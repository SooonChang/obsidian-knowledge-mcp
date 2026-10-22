import json
import unicodedata
from pathlib import Path

import pytest

from knowledge_mcp.drafts import DraftRequest, Drafts, DraftUpload
from knowledge_mcp.storage import split_frontmatter
from knowledge_mcp.sync import CommandFailure, SourceSync

REMOTE = "drafts:bucket/vault"


@pytest.fixture
def drafts(store):
    store.settings.drafts = {"enabled": True, "remote": REMOTE}
    return store


def request(path="회의/요약.md", request_id="draft_0001", body="# 요약\n\n내용"):
    return DraftRequest(request_id=request_id, path=path, body=body, reason="사용자 검토용 초안")


def fake_remote(monkeypatch, remote):
    calls = []

    def command(self, *args):
        calls.append(args)
        if args[0] == "lsjson":
            assert args[1] == REMOTE + "/AI_Drafts"
            if not remote:
                raise CommandFailure("rclone failed (exit 3)", 3)
            return json.dumps([{"Path": k, "Size": len(v)} for k, v in remote.items()])
        assert args[0] == "copyto" and "--ignore-existing" in args
        assert args[2].startswith(REMOTE + "/AI_Drafts/")
        key = args[2].removeprefix(REMOTE + "/AI_Drafts/")
        remote.setdefault(key, Path(args[1]).read_bytes())
        return ""

    monkeypatch.setattr(DraftUpload, "command", command)
    return calls


def test_upload_creates_new_draft_once(drafts, monkeypatch):
    remote = {}
    calls = fake_remote(monkeypatch, remote)
    queued = Drafts(drafts.settings).queue_draft(request())
    assert queued == {
        "pending": True,
        "request_id": "draft_0001",
        "path": "AI_Drafts/회의/요약.md",
        "reason": queued["reason"],
    }
    DraftUpload(drafts).process()
    meta, body, _ = split_frontmatter(remote["회의/요약.md"].decode())
    assert meta["generated_by"] == "knowledge-mcp" and meta["request_id"] == "draft_0001"
    assert body.strip() == "# 요약\n\n내용"
    result = Drafts(drafts.settings).queue_draft(request())
    assert result["uploaded"] and result["path"] == "AI_Drafts/회의/요약.md"
    assert [c[0] for c in calls].count("copyto") == 1
    with pytest.raises(ValueError, match="reused"):
        Drafts(drafts.settings).queue_draft(request(body="# 다른 내용"))


@pytest.mark.parametrize(
    "existing",
    ["회의/요약.md", "회의/요약.MD", "회의/" + unicodedata.normalize("NFD", "요약") + ".md"],
)
def test_existing_name_is_never_overwritten(drafts, monkeypatch, existing):
    remote = {existing: b"user edit"}
    calls = fake_remote(monkeypatch, remote)
    Drafts(drafts.settings).queue_draft(request())
    DraftUpload(drafts).process()
    result = Drafts(drafts.settings).queue_draft(request())
    assert result == {"uploaded": False, "code": "already_exists", "message": result["message"]}
    assert remote == {existing: b"user edit"}
    assert all(c[0] == "lsjson" for c in calls)


def test_concurrent_file_is_reported_unverified(drafts, monkeypatch):
    remote = {}
    fake_remote(monkeypatch, remote)
    original = DraftUpload.command

    def racing(self, *args):
        if args[0] == "copyto":
            remote["회의/요약.md"] = b"written by Obsidian first"
        return original(self, *args)

    monkeypatch.setattr(DraftUpload, "command", racing)
    Drafts(drafts.settings).queue_draft(request())
    DraftUpload(drafts).process()
    assert Drafts(drafts.settings).queue_draft(request())["code"] == "upload_unverified"
    assert remote["회의/요약.md"] == b"written by Obsidian first"


@pytest.mark.parametrize(
    "path",
    [
        "../x.md",
        ".hidden.md",
        "/abs.md",
        "a/../b.md",
        "report.pdf",
        "a/b/c/d.md",
        "x?.md",
        "dir./x.md",
        "c:x.md",
    ],
)
def test_invalid_paths_rejected(drafts, path):
    with pytest.raises(ValueError):
        Drafts(drafts.settings).queue_draft(request(path=path))


def test_path_without_extension_becomes_markdown(drafts):
    assert Drafts(drafts.settings).queue_draft(request(path="메모"))["path"] == "AI_Drafts/메모.md"


def test_credentials_rejected(drafts):
    with pytest.raises(ValueError, match="credential"):
        Drafts(drafts.settings).queue_draft(request(body="api_key = abcdefghijklmnop"))


def test_pending_queue_is_bounded(drafts):
    for i in range(20):
        Drafts(drafts.settings).queue_draft(request(path=f"n{i}.md", request_id=f"draft_{i:04d}"))
    with pytest.raises(ValueError, match="Too many"):
        Drafts(drafts.settings).queue_draft(request(path="extra.md", request_id="draft_extra"))


def test_disabled_or_shared_credentials_refused(drafts):
    drafts.settings.drafts["enabled"] = False
    with pytest.raises(ValueError, match="disabled"):
        Drafts(drafts.settings).queue_draft(request())
    drafts.settings.drafts = {"enabled": True, "remote": "notes:bucket/vault/AI_Drafts"}
    drafts.settings.source_sync = {"enabled": False, "remote": "notes:bucket/vault"}
    with pytest.raises(ValueError, match="separate"):
        Drafts(drafts.settings).queue_draft(request())


def test_drafts_are_never_source(store, tmp_path, monkeypatch):
    # Excluded regardless of wiki.config.json, including case variants Obsidian treats as equal.
    policy = store.settings.policy()
    assert not policy.allowed("source", "AI_Drafts/x.md")
    assert not policy.allowed("source", "ai_drafts/x.md")
    assert policy.allowed("source", "AI_Drafts_old/x.md")
    store.settings.snapshot_dir = tmp_path / "snapshots"
    store.settings.source_sync = {"enabled": True, "remote": "notes:bucket/vault"}
    remote = {"a.md": b"# source", "AI_Drafts/d.md": "# 초안 전용 문구".encode()}

    def command(self, *args):
        if args[0] == "lsjson":
            return json.dumps([{"Path": k, "Size": len(v)} for k, v in remote.items()])
        Path(args[2]).write_bytes(remote[args[1].removeprefix("notes:bucket/vault/")])
        return ""

    monkeypatch.setattr(SourceSync, "command", command)
    SourceSync(store).once()
    assert (store.settings.policy().source / "a.md").exists()
    assert not (store.settings.policy().source / "AI_Drafts").exists()
    assert not store.keyword("초안", "source")


def test_worker_uploads_drafts_during_long_sync(drafts, monkeypatch):
    import threading
    import time

    from knowledge_mcp import sync

    remote = {}
    fake_remote(monkeypatch, remote)
    release = threading.Event()

    class Stop(BaseException):
        pass

    def slow_sync(self):
        release.wait(10)
        raise Stop  # End the worker loop; the draft thread is a daemon.

    def run_worker():
        try:
            sync.worker(drafts.settings)
        except Stop:
            pass

    monkeypatch.setattr(SourceSync, "once", slow_sync)
    worker = threading.Thread(target=run_worker, daemon=True)
    worker.start()
    Drafts(drafts.settings).queue_draft(request())
    try:
        for _ in range(50):
            if "회의/요약.md" in remote:
                break
            time.sleep(0.1)
        assert "회의/요약.md" in remote
    finally:
        release.set()
        worker.join(10)
