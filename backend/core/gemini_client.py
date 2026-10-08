"""Gemini APIの接続先を1か所で切り替える。

Vertex AIはADC、Gemini Developer APIはGoogle AI StudioのAPIキーを使う。
呼び出し側が個別に判定すると、PDFだけVertex、生成だけDeveloper APIのように
設定が分かれるため、可用性判定とClient生成をここへ集約する。

画面（システム設定）で指定した値は、環境の設定より優先する（domain/system_gemini、
Antigravity と OpenAI 互換 API は domain/system_llm）。
main.py が DB を読む関数を渡し、ここで30秒だけ覚えておく。Vertex AI で Workload Identity
連携を選んでいれば、資格情報もここで作り、audience が変わらない限り使い回す。
"""
import threading
import time

from google import genai
from google.genai import types

_loader = None
_lock = threading.Lock()
_cache = {"at": 0.0, "value": None, "key": None, "credentials": None}
REFRESH_SECONDS = 30


def use_system_settings(loader) -> None:
    """loader() は保存済みのシステム設定を {"gemini": …, "antigravity": …, "openai_compatible": …}
    の形で返す（保存していない種類は None）。"""
    global _loader
    _loader = loader
    _cache.update(at=0.0, value=None, key=None, credentials=None)


def refresh_system_settings() -> None:
    """設定を保存した直後に呼ぶ。30秒待たずに次の呼び出しから新しい設定を読む。"""
    with _lock:
        _cache.update(at=0.0)


def _system_value():
    if _loader is None:
        return None
    with _lock:
        if time.monotonic() - _cache["at"] >= REFRESH_SECONDS:
            try:
                _cache["value"] = _loader()
            except Exception:
                _cache["value"] = None  # 読めなければ環境の設定のまま
            _cache["at"] = time.monotonic()
        return _cache["value"]


def _gemini_value():
    return (_system_value() or {}).get("gemini")


def _secret_value(secret) -> str:
    """SecretStrとテスト用の文字列を、ログへ出さずに同じ形で扱う。"""
    return secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret or "")


def effective(settings):
    """環境の設定に、システム設定を重ねたもの。モデル名や、どの生成AIを使えるかはここから読む。"""
    from backend.domain import system_gemini, system_llm
    key = _secret_value(getattr(settings, "tenant_secret_key", ""))
    update = {**system_gemini.overrides(_gemini_value(), key),
              **system_llm.overrides(_system_value() or {}, key)}
    return settings.model_copy(update=update) if update and hasattr(settings, "model_copy") else settings


def model(settings) -> str:
    return effective(settings).vertex_model


def thinking_config(settings):
    """思考レベルを指定していれば ThinkingConfig、無ければ None（モデルの既定）。"""
    level = getattr(effective(settings), "gemini_thinking_level", "")
    return types.ThinkingConfig(thinking_level=level) if level else None


def available(settings) -> bool:
    return available_in(effective(settings))


def available_in(settings) -> bool:
    """重ね済みの設定（テナントの値まで重ねたもの）で判定する。effective をもう一度かけない。"""
    backend = getattr(settings, "gemini_api_backend", "vertex")
    if backend == "developer":
        return bool(_secret_value(getattr(settings, "gemini_api_key", "")))
    return bool(settings.vertex_project and settings.vertex_location)


def _vertex_credentials():
    """WIFを選んでいなければ None（ADC＝環境の鍵ファイル）。"""
    from backend.core import k8s_token
    from backend.domain import system_gemini
    chosen = system_gemini.wif(_gemini_value())
    key = None if chosen is None else (system_gemini.audience(chosen), chosen.wif_service_account)
    with _lock:
        if key != _cache["key"]:
            _cache["credentials"] = None if chosen is None else k8s_token.credentials(
                system_gemini.credential_config(chosen),
                k8s_token.KubernetesTokenSupplier(system_gemini.audience(chosen)))
            _cache["key"] = key
        return _cache["credentials"]


def client(settings, *, timeout: int = 60_000, attempts: int = 1,
           initial_delay: float = 1, max_delay: float = 4):
    settings = effective(settings)
    options = types.HttpOptions(
        # Vertexは安定版、Developer APIはGoogleが既定としているbeta endpointを使う。
        api_version="v1" if settings.gemini_api_backend == "vertex" else "v1beta",
        timeout=timeout,
        retry_options=types.HttpRetryOptions(
            attempts=attempts, initial_delay=initial_delay, max_delay=max_delay,
            exp_base=2, jitter=0.2),
    )
    if settings.gemini_api_backend == "developer":
        return genai.Client(api_key=_secret_value(settings.gemini_api_key),
                            http_options=options)
    return genai.Client(vertexai=True, project=settings.vertex_project,
                        location=settings.vertex_location, http_options=options,
                        credentials=_vertex_credentials())


def generation_candidates(settings=None, *, api_key: str | None = None) -> list[dict]:
    """接続先のモデル一覧から、生成の選択肢にできる候補を返す（管理者が選ぶための材料）。

    api_key を渡すとそのキーで問い合わせる（テナントの Gemini API）。client() はシステムの
    設定を重ね直すので、テナントのキーにはそちらを使わない。
    """
    import itertools
    from backend.domain import system_gemini
    if api_key is not None:
        api = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta", timeout=30_000))
        vertex = False
    else:
        api = client(settings, timeout=30_000)
        vertex = effective(settings).gemini_api_backend == "vertex"
    try:
        # Vertex AI は query_base で Google が提供する基本モデルを並べる（無いとチューニング済みだけ）。
        pager = api.models.list(config={"page_size": 100, **({"query_base": True} if vertex else {})})
        models = list(itertools.islice(pager, 500))
    finally:
        api.close()
    return system_gemini.generation_candidates(models)
