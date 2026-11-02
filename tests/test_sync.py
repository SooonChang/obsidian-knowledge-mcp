import json
import shutil
import subprocess

import pytest
from filelock import FileLock

from knowledge_mcp.knowledge import Knowledge, SaveRequest
from knowledge_mcp.storage import Store, digest
from knowledge_mcp.sync import GitSync, ImageSync, Schedule, SourceSync, request_sync


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
            # Cheap listing only: no per-object HEAD for mtime or MIME type.
            assert {"--fast-list", "--no-mimetype", "--use-server-modtime"} <= set(args)
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
    unchanged = {"note.md": b"# unchanged"}
    remote.update(unchanged)
    sync.once()
    shared = store.settings.policy().source
    remote["a.md"] = "# 바뀐 지식".encode()
    sync.once()
    current = store.settings.policy().source
    assert (current / "report.pdf").read_bytes() == remote["report.pdf"]
    # Unchanged files share data with earlier generations instead of being copied.
    assert (current / "report.pdf").stat().st_ino == (first / "report.pdf").stat().st_ino
    assert (current / "note.md").stat().st_ino == (shared / "note.md").stat().st_ino
    assert (current / "a.md").stat().st_ino != (shared / "a.md").stat().st_ino
    manifest = json.loads((store.settings.snapshot_dir / "current.json").read_text())
    assert manifest["hashes"]["note.md"] == digest(unchanged["note.md"])
    assert manifest["hashes"]["report.pdf"] == digest(remote["report.pdf"])
    # Pruning an earlier generation removes only its names, not shared data.
    shutil.rmtree(first)
    shutil.rmtree(shared)
    assert (current / "report.pdf").read_bytes() == remote["report.pdf"]
    assert (current / "note.md").read_bytes() == unchanged["note.md"]
    assert store.keyword("바뀐", "source")
    del remote["a.md"]
    sync.once()
    assert not store.keyword("바뀐", "source")
    assert set(calls) <= {"lsjson", "copyto"}


def test_unstable_remote_does_not_publish(store, tmp_path, monkeypatch):
    store.settings.snapshot_dir = tmp_path / "snapshots"
    store.settings.source_sync = {"enabled": True, "remote": "notes:bucket/vault"}
    sync = SourceSync(store)
    first = {"a.md": {"size": 1, "mtime": "now", "id": None}}
    listed = iter([first, first | {"new.md": {"size": 0, "mtime": "later", "id": None}}])
    monkeypatch.setattr(sync, "listing", lambda fresh=False: next(listed))
    monkeypatch.setattr(sync, "download", lambda path, dest, expected: dest.write_bytes(b"#"))
    with pytest.raises(ValueError, match="changed during"):
        sync.once()
    assert not (store.settings.snapshot_dir / "current.json").exists()


def fake_remote(monkeypatch, remote):
    calls = []

    def fake_command(self, *args):
        calls.append(args)
        if args[0] == "lsjson":
            return json.dumps([{"Path": k, "Size": len(v), "ModTime": digest(v)} for k, v in remote.items()])
        from pathlib import Path

        Path(args[2]).parent.mkdir(parents=True, exist_ok=True)
        Path(args[2]).write_bytes(remote[args[1].split("/", 2)[2]])
        return ""

    monkeypatch.setattr(SourceSync, "command", fake_command)
    return calls


def test_unchanged_cycle_lists_once_and_downloads_nothing(store, tmp_path, monkeypatch):
    store.settings.snapshot_dir = tmp_path / "snapshots"
    store.settings.source_sync = {"enabled": True, "remote": "notes:bucket/vault"}
    remote = {"a.md": b"# a", "b.md": b"# b"}
    calls = fake_remote(monkeypatch, remote)
    SourceSync(store).once()
    # Downloads were verified by a second listing.
    assert [c[0] for c in calls] == ["lsjson", "copyto", "copyto", "lsjson"]
    calls.clear()
    SourceSync(store).once()
    assert [c[0] for c in calls] == ["lsjson"]
    calls.clear()
    remote["a.md"] = b"# changed"
    SourceSync(store).once()
    assert [c[0] for c in calls] == ["lsjson", "copyto", "lsjson"]
    assert store.keyword("changed", "source")


def test_cycle_shares_one_listing_between_source_and_images(store, tmp_path, monkeypatch):
    store.settings.snapshot_dir = tmp_path / "snapshots"
    store.settings.source_sync = {"enabled": True, "remote": "notes:bucket/vault"}
    calls = fake_remote(monkeypatch, {"a.md": b"# a"})
    SourceSync(store).once()
    calls.clear()
    cache = {}
    SourceSync(store, cache).once()
    ImageSync(store, cache).once()
    assert [c[0] for c in calls] == ["lsjson"]


def test_attachment_lists_only_its_folder(store, tmp_path, monkeypatch):
    store.settings.snapshot_dir = tmp_path / "snapshots"
    store.settings.source_sync = {"enabled": True, "remote": "notes:bucket/vault"}
    remote = {"docs/report.pdf": b"%PDF"}
    calls = []

    def fake_command(self, *args):
        calls.append(args)
        if args[0] == "lsjson":
            if "--recursive" in args:
                return json.dumps([{"Path": k, "Size": len(v), "ModTime": "t"} for k, v in remote.items()])
            assert args[1] == "notes:bucket/vault/docs"
            return json.dumps([{"Path": "report.pdf", "Size": 4, "ModTime": "t"}])
        from pathlib import Path

        Path(args[2]).write_bytes(remote["docs/report.pdf"])
        return ""

    monkeypatch.setattr(SourceSync, "command", fake_command)
    sync = SourceSync(store)
    sync.once()
    calls.clear()
    assert sync.attachment("docs/report.pdf").read_bytes() == b"%PDF"
    assert not [c for c in calls if "--recursive" in c]


def test_schedule_periodic_and_rate_limited_on_demand(store):
    settings = store.settings
    settings.source_sync = {"enabled": True, "remote": "notes:bucket/vault"}
    schedule = Schedule(settings)
    assert schedule.due(0) == (True, True)
    assert schedule.due(10) == (False, False)
    request_sync(settings)
    # Requested too soon after the last sync: wait for the minimum gap.
    assert schedule.due(299)[0] is False
    assert schedule.due(300) == (True, True)
    assert not (settings.data_dir / "sync-request").exists()
    # Without requests, sources wait for the interval; Git keeps its own.
    assert schedule.due(600) == (False, True)
    assert schedule.due(2099)[0] is False
    assert schedule.due(2100) == (True, True)


def test_request_sync_ignored_when_source_disabled(store):
    settings = store.settings
    settings.source_sync = {"enabled": False}
    request_sync(settings)
    assert not (settings.data_dir / "sync-request").exists()


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
