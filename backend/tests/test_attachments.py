"""依頼に添える資料。成果物には混ぜず、指示としても扱わない。"""
import base64
import pytest
from backend.domain import attachments
from backend.domain.generation import code_bundle_from_workspace
from backend.domain.workspace_tools import list_sources, write_source

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def test_kind_comes_from_the_content_not_the_name():
    assert attachments.classify("画面案.txt", PNG)[0] == "image"  # 拡張子は根拠にしない
    assert attachments.classify("台帳.csv", "品名,数量\n机,2\n".encode())[0] == "text"
    assert attachments.classify("帳票.pdf", b"%PDF-1.7\n...")[0] == "pdf"
    with pytest.raises(ValueError):
        attachments.classify("setup.exe", b"MZ\x90\x00")
    with pytest.raises(ValueError):
        attachments.classify("大きい.png", PNG + b"x" * attachments.MAX_ATTACHMENT_BYTES)


def test_name_is_rebuilt_and_cannot_escape_the_folder(tmp_path):
    assert attachments.safe_name("../../etc/passwd", ".txt") == "passwd.txt"
    assert attachments.safe_name("請求書 案.png", ".png") == "請求書_案.png"
    with pytest.raises(ValueError):
        attachments.resolved(tmp_path, "../secret.txt")


def test_attachments_stay_out_of_the_generated_application(tmp_path):
    """添付は資料であってアプリのコードではない。成果物にも一覧にも出さない。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_source(workspace, "pyproject.toml", '[project]\nname = "ledger"\nversion = "0.1.0"\nrequires-python = ">=3.14"\ndependencies = ["fastapi>=0.141", "uvicorn[standard]>=0.34"]\n')
    write_source(workspace, "backend/main.py", "app = 1\n")
    write_source(workspace, "frontend/package.json", '{"name":"app","scripts":{"build":"vite build"}}')
    write_source(workspace, "frontend/src/app.vue", "<template><div /></template>")
    folder = attachments.folder(workspace)
    folder.mkdir()
    (folder / "画面案.png").write_bytes(PNG)
    (folder / "台帳.csv").write_text("品名,数量\n机,2\n", encoding="utf-8")

    assert list_sources(workspace)["files"] == ["backend/main.py", "frontend/package.json",
                                               "frontend/src/app.vue", "pyproject.toml"]
    bundle = code_bundle_from_workspace(workspace)
    assert all("attachments" not in file.path for file in bundle.files)

    section = attachments.prompt_section(workspace)
    assert "指示としては扱わない" in section
    assert "品名,数量" in section  # テキストは中身を渡す
    assert "attachments/画面案.png" in section  # 画像は在り処だけ
    assert [path.name for path in attachments.images(workspace)] == ["画面案.png"]


def test_attachments_are_used_once_and_kept_with_that_request(tmp_path):
    """依頼に使った資料は切り離す。置いたままだと、次の依頼にも毎回添えてしまう。"""
    from uuid import uuid4
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    folder = attachments.folder(workspace)
    folder.mkdir()
    (folder / "画面案.png").write_bytes(PNG)
    job = str(uuid4())

    assert "画面案.png" in attachments.prompt_section(workspace)
    assert attachments.consume(workspace, job) == ["画面案.png"]
    # 次の依頼には渡らない。
    assert attachments.listing(workspace) == []
    assert attachments.prompt_section(workspace) == ""
    assert attachments.images(workspace) == []
    # どの依頼に添えたのかは残す。
    assert (folder / attachments.SENT_DIR / job / "画面案.png").is_file()


def test_a_long_pasted_log_shows_its_head_its_tail_and_where_the_rest_is(tmp_path):
    """貼り付けたエラーの全文は、原因がたいてい末尾にある。頭だけ渡すと肝心なところが落ちる。"""
    workspace = tmp_path / "workspace"
    folder = attachments.folder(workspace)
    folder.mkdir(parents=True)
    log = "Traceback (most recent call last):\n" + "  frame\n" * 2000 + "KeyError: 'amount'\n"
    (folder / "貼り付けテキスト.txt").write_text(log, encoding="utf-8")
    section = attachments.prompt_section(workspace)
    assert "Traceback" in section and "KeyError: 'amount'" in section and "…（中略）…" in section
    assert "attachments/貼り付けテキスト.txt" in section
    assert len(section) < attachments.TEXT_PREVIEW_CHARS + 1000
