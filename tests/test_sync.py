import json
import subprocess

import pytest
from filelock import FileLock

from knowledge_mcp.knowledge import Knowledge, SaveRequest
from knowledge_mcp.storage import Store, digest
from knowledge_mcp.sync import GitSync, SourceSync


def test_source_snapshots_and_delete(store, tmp_path, monkeypatch):
    store.settings.snapshot_dir = tmp_path / "snapshots"
    store.settings.source_sync = {"enabled": True, "remote": "notes:bucket/vault"}
    remote = {
        "a.md": "# 내용\n새 지식".encode(),
        "report.pdf": b"not fetched automatically",
        ".secret.md": b"secret",
        "_Template/no.md": b"excluded",
    }
    calls = []

    def fake_command(self, *args):
        # Another process can acquire the writer lock during S3 I/O, including
        # both initial sync and on-demand attachments.
        with FileLock(store.lock.lock_file, timeout=0):
            pass
        calls.append(args[0])
        if args[0] == "lsjson":
            return json.dumps([{"Path": k, "Size": len(v), "ModTime": digest(v)} for k, v in remote.items()])
        assert args[0] == "copyto"
        assert args[1].startswith("notes:bucket/vault/")
        from pathlib import Path

        Path(args[2]).write_bytes(remote[args[1].removeprefix("notes:bucket/vault/")])
        return ""

    monkeypatch.setattr(SourceSync, "command", fake_command)
    sync = SourceSync(store)
    sync.once()
    first = store.settings.policy().source
    assert (first / "a.md").exists()
    assert not (first / "report.pdf").exists()
    assert not (first / ".secret.md").exists()
    sync.attachment("report.pdf")
    assert (first / "report.pdf").read_bytes() == remote["report.pdf"]
    remote["a.md"] = "# 바뀐 지식".encode()
    sync.once()
    assert (store.settings.policy().source / "report.pdf").read_bytes() == remote["report.pdf"]
    assert store.keyword("바뀐", "source")
    del remote["a.md"]
    sync.once()
    assert not store.keyword("바뀐", "source")
    assert set(calls) <= {"lsjson", "copyto"}


def test_unstable_remote_does_not_publish(store, tmp_path, monkeypatch):
    store.settings.snapshot_dir = tmp_path / "snapshots"
    store.settings.source_sync = {"enabled": True, "remote": "notes:bucket/vault"}
    sync = SourceSync(store)
    listed = iter([{}, {"new.md": {"size": 0, "mtime": "later"}}])
    monkeypatch.setattr(sync, "listing", lambda: next(listed))
    with pytest.raises(ValueError, match="changed during"):
        sync.once()
    assert not (store.settings.snapshot_dir / "current.json").exists()


def git(root, *args):
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@local", *args],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


@pytest.mark.parametrize("branch", ["codex/wiki-memory", "wiki-memory"])
def test_git_only_server_branch_and_unowned_change(store, tmp_path, request_data, branch, monkeypatch):
    root = store.settings.wiki_root
    git(root, "init", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-u", "origin", "main")
    original_main = git(root, "rev-parse", "main")
    store.settings.git = {"enabled": True, "branch": branch}
    sync = GitSync(store)
    real_git = sync.git
    saved_during_fetch = False

    def checked_git(*args):
        nonlocal saved_during_fetch
        if args[0] in ("fetch", "push"):
            with FileLock(store.lock.lock_file, timeout=0):
                pass
            if args[0] == "fetch" and git(root, "branch", "--show-current") == branch:
                other = Store(store.settings)
                other.lock.timeout = 0
                assert Knowledge(other).save(SaveRequest(**request_data))["saved"]
                saved_during_fetch = True
        return real_git(*args)

    monkeypatch.setattr(sync, "git", checked_git)
    sync.initialize()
    result = sync.once()
    assert result["ok"]
    assert saved_during_fetch
    assert git(root, "rev-parse", "origin/main") == original_main
    assert git(root, "rev-parse", "HEAD") == git(root, "rev-parse", "origin/" + branch)
    (root / "README.md").write_text("other person's file")
    with pytest.raises(ValueError, match="Unowned"):
        sync.once()


def test_git_dirty_initialization_refused(store):
    root = store.settings.wiki_root
    git(root, "init", "-b", "main")
    with pytest.raises(ValueError, match="dirty"):
        GitSync(store).initialize()


@pytest.mark.parametrize("branch", ["main", "master", "HEAD", "-bad", "bad..name", "@{-1}"])
def test_git_invalid_server_branch_refused(store, branch):
    store.settings.git = {"enabled": True, "branch": branch}
    with pytest.raises(ValueError, match="Invalid server branch"):
        GitSync(store)
