from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from filelock import FileLock

from .config import Settings, relative_path
from .storage import Store, atomic_write, digest, now


class CommandFailure(RuntimeError):
    def __init__(self, message: str, returncode: int | None = None):
        super().__init__(message)
        self.returncode = returncode


class SyncValidationError(ValueError):
    """A validation error whose code and message contain no remote data."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def run(args, *, cwd=None, timeout=300):
    result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        # Never expose rclone errors, remote URLs or credentials in tool outputs.
        raise CommandFailure(f"{Path(args[0]).name} failed (exit {result.returncode})", result.returncode)
    return result.stdout.decode("utf-8", errors="replace").strip()


def rclone(*args):
    cfg = os.environ.get("RCLONE_CONFIG")
    command = ["rclone"]
    if cfg:
        command += ["--config", cfg]
    return run(command + list(args))


# S3 bills per request. Without these flags rclone lists folder by folder and
# sends one HEAD per object to read its stored mtime and Content-Type.
# --fast-list lists 1,000 keys per request; the server LastModified and size
# from the listing are enough to detect changes. Avoid --hash: multipart
# objects would need a HEAD each.
LIST_FLAGS = ("--files-only", "--fast-list", "--no-mimetype", "--use-server-modtime")
SYNC_REQUEST = "sync-request"


def request_sync(settings: Settings):
    """Ask the worker for a source refresh; it rate-limits how often S3 is listed."""
    if not settings.source_sync.get("enabled"):
        return
    try:
        (settings.data_dir / SYNC_REQUEST).touch()
    except OSError:
        pass  # A refresh hint must never fail a tool call.


def named_remote(remote) -> bool:
    """A configured rclone remote, never a local filesystem path or ad-hoc backend."""
    return (
        isinstance(remote, str)
        and ":" in remote
        and not remote.startswith(("/", ":", "-"))
        and "\\" not in remote
    )


class SourceSync:
    def __init__(self, store: Store, cache: dict | None = None):
        self.store, self.settings = store, store.settings
        # Shared by the jobs of one worker cycle so the remote is listed once.
        self.cache = cache
        self.root = self.settings.snapshot_dir
        self.remote = self.settings.source_sync.get("remote", "")
        self.lock = FileLock(str(self.settings.data_dir / "source-sync.lock"), timeout=30)
        if self.settings.source_sync.get("enabled") and (not self.root or not named_remote(self.remote)):
            raise ValueError("source_sync requires snapshot_dir and a named rclone remote")

    def command(self, *args):
        return rclone(*args)

    def remote_files(self, fresh=False):
        if self.cache is not None and not fresh and "records" in self.cache:
            return self.cache["records"]
        records = json.loads(self.command("lsjson", self.remote, "--recursive", *LIST_FLAGS))
        if self.cache is not None:
            self.cache["records"] = records
        return records

    @staticmethod
    def metadata(record):
        if record.get("IsLink") or record.get("Size", 0) < 0:
            return None
        return {"size": record["Size"], "mtime": record.get("ModTime"), "id": record.get("ID")}

    def listing(self, fresh=False):
        policy, files = self.settings.policy(), {}
        for record in self.remote_files(fresh):
            path = record["Path"]
            if not policy.allowed("source", path):
                continue
            metadata = self.metadata(record)
            if metadata and self.accept_file(policy, path):
                files[path] = metadata
        return files

    def entry(self, path):
        """Metadata of one object, listing only its folder instead of the whole vault."""
        parent, _, name = relative_path(path).rpartition("/")
        root = self.remote.rstrip("/") + ("/" + parent if parent else "")
        try:
            records = json.loads(self.command("lsjson", root, *LIST_FLAGS))
        except CommandFailure as exc:
            if exc.returncode == 3:  # Directory not found.
                return None
            raise
        for record in records:
            if record["Path"] == name:
                return self.metadata(record)
        return None

    def accept_file(self, policy, path):
        return path.lower().endswith((".md", ".html", ".htm", ".pdf"))

    def download(self, path: str, dest: Path, expected):
        relative_path(path)
        limit = 4_000_000 if path.lower().endswith(".md") else 25_000_000
        if expected["size"] > limit:
            raise SyncValidationError("file_too_large", "Remote document exceeds configured size limit")
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Do not add --s3-no-head-object: copyto then cannot tell the source is a
        # file and silently copies nothing. Downloads are rare; listings are not.
        self.command("copyto", self.remote.rstrip("/") + "/" + path, str(dest), "--retries", "2")
        if dest.is_symlink() or dest.stat().st_size != expected["size"]:
            raise SyncValidationError("download_size_mismatch", "Downloaded size differs from remote listing")
        if path.lower().endswith(".md"):
            dest.read_text("utf-8-sig")

    def once(self):
        if not self.settings.source_sync.get("enabled"):
            return {"enabled": False}
        # Serialize snapshots and attachments without blocking wiki writers
        # during remote listing/downloads.
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            before = self.listing()
            policy = self.settings.policy()
            old_pointer = self.root / "current.json"
            old = json.loads(old_pointer.read_text()) if old_pointer.exists() else {}
            generation = uuid.uuid4().hex
            dest = self.root / "generations" / generation
            dest.mkdir(parents=True)
            manifest = {"generation": generation, "at": now(), "files": before, "hashes": {}}
            downloaded = False
            for path, metadata in before.items():
                markdown = path.lower().endswith(".md") and not (
                    path == policy.attachments or path.startswith(policy.attachments + "/")
                )
                target = dest / path
                previous = policy.source / path
                if (
                    old.get("files", {}).get(path) == metadata
                    and previous.is_file()
                    and not previous.is_symlink()
                ):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Retain unchanged notes and on-demand attachments. Files are never
                    # modified in place, so generations can share data via hard links.
                    try:
                        os.link(previous, target)
                    except OSError:
                        target.write_bytes(previous.read_bytes())
                    if path in old.get("hashes", {}):
                        manifest["hashes"][path] = old["hashes"][path]
                        continue
                elif markdown:
                    self.download(path, target, metadata)
                    downloaded = True
                else:
                    continue
                manifest["hashes"][path] = digest(target.read_bytes())
            # Only newly downloaded bytes can be torn by a concurrent upload; a
            # generation of unchanged files needs no second (billed) listing.
            if downloaded and self.listing(fresh=True) != before:
                raise ValueError("Remote changed during sync; previous generation retained")
            # A stable object listing is not a transaction across an Obsidian upload.
            with self.store.lock:
                atomic_write(old_pointer, json.dumps(manifest, ensure_ascii=False).encode())
                state = self.store.state(
                    "source_sync", {"ok": True, "at": now(), "generation": generation, "files": len(before)}
                )
                self.store.reindex("source")
            self.prune(generation)
            return state

    def prune(self, current):
        import shutil

        root = (self.root / "generations").resolve()
        # Keep the previous snapshots for in-flight readers and recovery; bound disk usage.
        directories = sorted(
            (p for p in root.iterdir() if p.is_dir() and not p.is_symlink()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for p in directories[3:]:
            if p.name != current and p.resolve().parent == root and time.time() - p.stat().st_mtime > 3600:
                shutil.rmtree(p)

    def attachment(self, path):
        if not self.settings.source_sync.get("enabled"):
            raise FileNotFoundError("Attachment unavailable")
        with self.lock:
            pointer = json.loads((self.root / "current.json").read_text())
            policy = self.settings.policy()
            if not policy.allowed("source", path):
                raise ValueError("Excluded attachment")
            metadata = pointer["files"].get(path)
            if not metadata:
                raise FileNotFoundError("Attachment not in published source listing")
            file = policy.resolve("source", path, exists=False)
            if file.exists():
                return file
            # Verify the remote entry again, including after download.
            if self.entry(path) != metadata:
                raise ValueError("Attachment changed; synchronize source first")
            temp = file.with_name(file.name + "." + uuid.uuid4().hex + ".download")
            self.download(path, temp, metadata)
            if self.entry(path) != metadata:
                temp.unlink(missing_ok=True)
                raise ValueError("Attachment changed during download")
            os.replace(temp, file)
            return file


class ImageSync(SourceSync):
    """Maintain one latest image mirror independently of Markdown generations."""

    def accept_file(self, policy, path):
        return policy.is_image(path)

    def once(self):
        if not self.settings.source_sync.get("enabled"):
            return self.store.state("image_sync", {"enabled": False, "status": "disabled"})
        from .images import ImageValidationError

        prior = self.store.state("image_sync") or {}
        progress = {
            "enabled": True, "ok": False, "status": "running", "stage": "preparing",
            "started_at": now(), "total": 0, "processed": 0, "downloaded": 0, "reused": 0,
            "last_success_at": prior.get("last_success_at") or prior.get("at"),
        }
        self.store.state("image_sync", progress)
        try:
            return self._once(progress)
        except Exception as exc:
            known = isinstance(exc, (SyncValidationError, ImageValidationError))
            progress.update(
                ok=False, status="failed", error=type(exc).__name__, attempt=now(),
                code=exc.code if known else "image_sync_failed",
                message=str(exc) if known else "Image sync failed; inspect private operator logs",
            )
            self.store.state("image_sync", progress)
            raise

    def _once(self, progress):
        from .images import decode_image

        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            root = self.root / "images"
            if root.is_symlink():
                raise SyncValidationError("unsafe_image_path", "Linked image directories are forbidden")
            root.mkdir(exist_ok=True)
            policy = self.settings.policy()
            progress.update(stage="listing")
            self.store.state("image_sync", progress)
            before = self.listing()
            progress.update(total=len(before), stage="downloading")
            self.store.state("image_sync", progress)
            old = policy.image_manifest().get("files", {})
            files, downloads = {}, {}
            with tempfile.TemporaryDirectory(prefix="image-stage-", dir=self.root) as staging:
                for path, metadata in before.items():
                    # An opaque reference lets an operator correlate a failure
                    # locally without revealing vault filenames in MCP or logs.
                    progress.update(file_id=digest(path))
                    self.store.state("image_sync", progress)
                    target = policy.resolve("source", path, exists=False)
                    previous = old.get(path, {})
                    if (
                        previous.get("remote") == metadata
                        and target.is_file()
                        and target.stat().st_size == metadata["size"]
                        and digest(target.read_bytes()) == previous.get("hash")
                    ):
                        files[path] = previous
                        progress.update(processed=progress["processed"] + 1, reused=progress["reused"] + 1)
                        self.store.state("image_sync", progress)
                        continue
                    temp = Path(staging) / path
                    self.download(path, temp, metadata)
                    data = temp.read_bytes()
                    progress.update(stage="validating")
                    self.store.state("image_sync", progress)
                    image, _ = decode_image(data, Path(path).suffix)
                    image.close()
                    files[path] = {"remote": metadata, "hash": digest(data)}
                    downloads[path] = temp
                    progress.update(
                        stage="downloading", processed=progress["processed"] + 1,
                        downloaded=progress["downloaded"] + 1,
                    )
                    self.store.state("image_sync", progress)
                progress.update(stage="verifying")
                self.store.state("image_sync", progress)
                if downloads and self.listing(fresh=True) != before:
                    raise SyncValidationError("remote_changed", "Remote images changed during sync; publication deferred")
                progress.update(stage="publishing")
                self.store.state("image_sync", progress)
                with self.store.lock:
                    for path, temp in downloads.items():
                        target = policy.resolve("source", path, exists=False)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(temp, target)
                    # Publish before cleanup. A partially failed publication is
                    # detected by hashes; an interrupted cleanup is retried below.
                    at = now()
                    atomic_write(self.root / "images.json", json.dumps({"at": at, "files": files}).encode())
                    self._cleanup(set(files))
                    progress.pop("file_id", None)
                    progress.update(ok=True, status="complete", stage="complete", at=at, last_success_at=at, files=len(files))
                    return self.store.state("image_sync", progress)

    def _cleanup(self, keep):
        root = self.root / "images"
        # Only this dedicated mirror is scanned. Never traverse linked directories.
        for directory, dirs, names in os.walk(root, topdown=False, followlinks=False):
            for name in names:
                file = Path(directory) / name
                path = file.relative_to(root).as_posix()
                if path not in keep:
                    file.unlink()
            for name in dirs:
                child = Path(directory) / name
                if child.is_symlink():
                    child.unlink()
                elif not any(child.iterdir()):
                    child.rmdir()


class GitSync:
    def __init__(self, store: Store):
        self.store = store
        self.settings = store.settings
        self.root = self.settings.wiki_root
        self.lock = FileLock(str(self.settings.data_dir / "git-sync.lock"), timeout=30)
        self.branch = self.settings.git.get("branch", "codex/wiki-memory")
        if not isinstance(self.branch, str) or self.branch in ("main", "master"):
            raise ValueError("Invalid server branch")
        try:
            checked = run(["git", "check-ref-format", "--branch", self.branch])
        except CommandFailure as exc:
            raise ValueError("Invalid server branch") from exc
        if checked != self.branch:
            raise ValueError("Invalid server branch")

    def git(self, *args):
        return run(["git", *args], cwd=self.root)

    def initialize(self):
        with self.lock:
            with self.store.lock:
                if self.git("status", "--porcelain"):
                    raise ValueError("Refuse initialization in a dirty checkout")
            self.git("fetch", "origin")
            with self.store.lock:
                if self.git("status", "--porcelain"):
                    raise ValueError("Refuse initialization in a dirty checkout")
                try:
                    self.git("show-ref", "--verify", "--quiet", "refs/heads/" + self.branch)
                    self.git("switch", self.branch)
                except CommandFailure:
                    try:
                        self.git("show-ref", "--verify", "--quiet", "refs/remotes/origin/" + self.branch)
                        self.git("switch", "-c", self.branch, "--track", "origin/" + self.branch)
                    except CommandFailure:
                        self.git("switch", "-c", self.branch, "origin/main")
            return {"branch": self.branch}

    def once(self):
        if not self.settings.git.get("enabled"):
            return {"enabled": False}
        with self.lock:
            self._commit_owned()
            self.git("fetch", "origin")
            with self.store.lock:
                # Saving can proceed during fetch. Commit any such writes before
                # merge so Git never auto-stashes or absorbs a dirty checkout.
                self._commit_owned()
                self._merge_remote()
            self.git("push", "origin", "HEAD:refs/heads/" + self.branch)
            self.store.reindex("wiki")
            return self.store.state(
                "git", {"ok": True, "at": now(), "head": self.git("rev-parse", "HEAD"), "branch": self.branch}
            )

    def _commit_owned(self):
        with self.store.lock:
            if self.git("branch", "--show-current") != self.branch:
                raise ValueError("Checkout must be initialized on server branch")
            # Only journal-owned files may be committed. Never capture arbitrary user edits.
            owned = {}
            transaction_dir = self.settings.data_dir / "transactions"
            for file in sorted(transaction_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
                job = json.loads(file.read_text())
                if job["state"] == "done":
                    for item in job["files"]:
                        owned[item["path"]] = item["after"]
            paths = []
            raw = subprocess.check_output(
                ["git", "status", "--porcelain", "-z", "--untracked-files=all"], cwd=self.root
            )
            for record in raw.decode().split("\x00"):
                if not record:
                    continue
                code, path = record[:2], record[3:]
                if "R" in code or "C" in code:
                    raise ValueError("Unexpected rename in server checkout")
                if (
                    path not in owned
                    or not (self.root / path).is_file()
                    or digest((self.root / path).read_bytes()) != owned[path]
                ):
                    raise ValueError("Unowned checkout change: " + path)
                paths.append(path)
            if paths:
                self.git("add", "--", *paths)
                self.git(
                    "-c",
                    "user.name=Knowledge MCP",
                    "-c",
                    "user.email=knowledge-mcp@localhost",
                    "commit",
                    "-m",
                    "Update evidence-based wiki memory",
                )

    def _merge_remote(self):
        for ref in ("origin/" + self.branch, "origin/main"):
            try:
                self.git("rev-parse", "--verify", ref)
            except CommandFailure:
                continue
            try:
                self.git(
                    "-c",
                    "user.name=Knowledge MCP",
                    "-c",
                    "user.email=knowledge-mcp@localhost",
                    "merge",
                    "--no-edit",
                    ref,
                )
            except CommandFailure:
                self.git("merge", "--abort")
                raise ValueError(
                    "Git merge conflict; server branch preserved for manual resolution"
                ) from None


def drafts(store: Store, forever=False):
    from .drafts import DraftUpload

    while True:
        try:
            DraftUpload(store).process()
        except Exception as exc:
            store.state("drafts", {"ok": False, "error": type(exc).__name__, "at": now()})
        if not forever:
            return
        time.sleep(2)


def seconds(section: dict, key: str, default: int) -> int:
    return max(60, int(section.get(key, default)))


class Schedule:
    """Decide when to list S3 (billed per request) and when to sync Git (free).

    Source and images sync every interval_seconds, and sooner when an MCP call
    requested it, but never more often than on_demand_min_seconds.
    """

    def __init__(self, settings: Settings):
        self.request = settings.data_dir / SYNC_REQUEST
        self.interval = seconds(settings.source_sync, "interval_seconds", 1800)
        self.min_gap = seconds(settings.source_sync, "on_demand_min_seconds", 300)
        self.git_interval = seconds(settings.git, "interval_seconds", 300)
        self.last_source = self.last_git = None

    def due(self, clock: float):
        source = (
            self.last_source is None
            or clock - self.last_source >= self.interval
            or (self.request.exists() and clock - self.last_source >= self.min_gap)
        )
        git = source or self.last_git is None or clock - self.last_git >= self.git_interval
        if source:
            # Calls arriving during this cycle request the next one.
            self.request.unlink(missing_ok=True)
            self.last_source = clock
        if git:
            self.last_git = clock
        return source, git


def worker(settings: Settings, *, once=False):
    store = Store(settings)
    from .knowledge import Knowledge

    Knowledge(store).recover()
    if not once:
        # Drafts must not wait for a full source/image/Git cycle, which can take minutes.
        threading.Thread(target=drafts, args=(store, True), daemon=True).start()
    schedule = Schedule(settings)
    while True:
        source, git = schedule.due(time.monotonic())
        jobs = []
        if source:
            cache = {}
            jobs += [
                ("source_sync", lambda: SourceSync(store, cache).once()),
                ("image_sync", lambda: ImageSync(store, cache).once()),
            ]
        if git:
            jobs.append(("git", lambda: GitSync(store).once()))
        for key, job in jobs:
            try:
                job()
            except Exception as exc:
                prior = store.state(key) or {}
                if key != "image_sync" or prior.get("status") != "failed":
                    store.state(
                        key,
                        prior
                        | {
                            "ok": False,
                            "error": type(exc).__name__,
                            "message": "Sync failed; inspect private operator logs",
                            "attempt": now(),
                        },
                    )
                # Include only static diagnostics, never remote paths or stderr.
                detail = ""
                if key == "image_sync" and prior.get("status") == "failed":
                    detail = f" (code={prior['code']}, stage={prior['stage']})"
                print(f"{key}: {type(exc).__name__}{detail}", flush=True)
        if jobs:
            store.reindex("wiki")
        queue = settings.data_dir / "attachment-requests"
        for request_file in queue.glob("*.json"):
            try:
                SourceSync(store).attachment(json.loads(request_file.read_text())["path"])
                request_file.unlink()
            except Exception as exc:
                store.state("attachment_error", {"error": type(exc).__name__, "at": now()})
        if once:
            drafts(store)
            return
        time.sleep(2)
