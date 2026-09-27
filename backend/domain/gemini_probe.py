"""テナントの Gemini が生成アプリから使えるかを確かめる、1回だけの問い合わせ。

生成アプリと同じ経路で確かめるため、preview-controller がテナントの身元で動く使い捨ての
Podの中で、このファイルを `python -c` でそのまま実行する。プレビュー実行環境には
google-genai が無いので、標準ライブラリだけで書く（backend からは何も import しない）。

入力は生成アプリと同じ環境変数（GOOGLE_GENAI_USE_VERTEXAI など）。
出力は1行のJSON。どの段階で止まったか（step）と、Googleが返した理由を返す。
トークン・APIキー・資格情報は決して出力しない。
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

PROMPT = "接続テストです。「OK」とだけ答えてください。"
TIMEOUT = 30


class ProbeFailed(Exception):
    def __init__(self, step, message, status=None):
        super().__init__(message)
        self.step, self.message, self.status = step, message, status


def _request(step, url, *, data=None, form=None, headers=None):
    body = (urllib.parse.urlencode(form).encode() if form is not None
            else json.dumps(data).encode() if data is not None else None)
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded" if form is not None else "application/json",
        **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise ProbeFailed(step, _reason(exc), exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProbeFailed(step, f"接続できませんでした（{getattr(exc, 'reason', exc)}）") from None


def _reason(exc):
    """Googleのエラー応答から理由だけを取り出す。応答にトークンは含まれない。"""
    try:
        payload = json.loads(exc.read().decode())
    except (ValueError, OSError):
        return f"HTTP {exc.code}"
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("status") or f"HTTP {exc.code}")[:500]
    return str(payload.get("error_description") or error or f"HTTP {exc.code}")[:500]


def _vertex_token(env):
    """Workload Identity連携：Kubernetesのトークンを、GCPのアクセストークンに交換する。"""
    try:
        with open(env["GOOGLE_APPLICATION_CREDENTIALS"], encoding="utf-8") as stream:
            config = json.load(stream)
        with open(config["credential_source"]["file"], encoding="utf-8") as stream:
            subject = stream.read().strip()
    except (KeyError, OSError, ValueError):
        raise ProbeFailed("credentials", "資格情報のファイルを読めませんでした。") from None
    exchanged = _request("sts", config["token_url"], form={
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "audience": config["audience"],
        "scope": "https://www.googleapis.com/auth/cloud-platform",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "subject_token_type": config["subject_token_type"],
        "subject_token": subject})
    token = exchanged.get("access_token")
    if not token:
        raise ProbeFailed("sts", "STSがアクセストークンを返しませんでした。")
    impersonation = config.get("service_account_impersonation_url")
    if impersonation:
        issued = _request("impersonation", impersonation,
                          data={"scope": ["https://www.googleapis.com/auth/cloud-platform"]},
                          headers={"Authorization": "Bearer " + token})
        token = issued.get("accessToken")
        if not token:
            raise ProbeFailed("impersonation", "サービスアカウントのトークンを取得できませんでした。")
    return token


def probe(env):
    started = time.monotonic()
    model = env.get("GEMINI_MODEL", "")
    if not model:
        raise ProbeFailed("settings", "GEMINI_MODEL が設定されていません。")
    body = {"contents": [{"role": "user", "parts": [{"text": PROMPT}]}]}
    if env.get("GEMINI_THINKING_LEVEL"):
        body["generationConfig"] = {"thinkingConfig": {"thinkingLevel": env["GEMINI_THINKING_LEVEL"]}}
    if env.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true":
        token = _vertex_token(env)
        location, project = env.get("GOOGLE_CLOUD_LOCATION", "global"), env.get("GOOGLE_CLOUD_PROJECT", "")
        host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
        url = (f"https://{host}/v1/projects/{project}/locations/{location}"
               f"/publishers/google/models/{model}:generateContent")
        headers = {"Authorization": "Bearer " + token}
    else:
        key = env.get("GEMINI_API_KEY", "")
        if not key:
            raise ProbeFailed("settings", "GEMINI_API_KEY が設定されていません。")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        headers = {"x-goog-api-key": key}
    answer = _request("generate", url, data=body, headers=headers)
    parts = ((answer.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()
    return {"ok": True, "step": "generate", "model": model, "text": text[:200],
            "elapsed_ms": int((time.monotonic() - started) * 1000)}


def run(env):
    try:
        return probe(env)
    except ProbeFailed as exc:
        return {"ok": False, "step": exc.step, "status": exc.status, "message": exc.message}


def source():
    """Podで `python -c` に渡す中身。このファイルそのもの。"""
    with open(__file__, encoding="utf-8") as stream:
        return stream.read()


if __name__ == "__main__":
    print("GEMINI_PROBE " + json.dumps(run(dict(os.environ)), ensure_ascii=False))
