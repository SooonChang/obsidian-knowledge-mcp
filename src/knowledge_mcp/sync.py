from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from .config import Settings, relative_path
from .storage import Store, atomic_write, digest, now


class CommandFailure(RuntimeError):
    pass


def run(args, *, cwd=None, timeout=300):
    result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        # Never expose rclone errors, remote URLs or credentials in tool outputs.
        raise CommandFailure(f"{Path(args[0]).name} failed (exit {result.returncode})")
    return result.stdout.decode("utf-8", errors="replace").strip()


class SourceSync:
    def __init__(self, store: Store):
        self.store, self.settings = store, store.settings
        self.root = self.settings.snapshot_dir
        self.remote = self.settings.source_sync.get("remote", "")
        # Require a configured rclone remote, never a local filesystem source or ad-hoc backend.
        if self.settings.source_sync.get("enabled") and (
            not self.root
            or not self.remote
            or self.remote.startswith(("/", ":", "-"))
            or ":" not in self.remote
            or "\\" in self.remote
        ):
            raise ValueError("source_sync requires snapshot_dir and a named rclone remote")

    def command(self, *args):
        cfg = os.environ.get("RCLONE_CONFIG")
        command = ["rclone"]
        if cfg:
            command += ["--config", cfg]
        return run(command + list(args))

    def listing(self):
        records = json.loads(self.command("lsjson", self.remote, "--recursive", "--files-only"))
        policy, files = self.settings.policy(), {}
        for record in records:
            path = record["Path"]
            if not policy.allowed("source", path):
                continue
            if record.get("IsLink") or record.get("Size", 0) < 0:
                continue
            if path.lower().endswith((".md", ".html", ".htm", ".pdf")):
                files[path] = {"size": record["Size"], "mtime": record.get("ModTime"), "id": record.get("ID")}
        return files

    def download(self, path: str, dest: Path, expected):
        relative_path(path)
        limit = 4_000_000 if path.lower().endswith(".md") else 25_000_000
        if expected["size"] > limit:
            raise ValueError("Remote document exceeds configured size limit")
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.command("copyto", self.remote.rstrip("/") + "/" + path, str(dest), "--retries", "2")
        if dest.is_symlink() or dest.stat().st_size != expected["size"]:
            raise ValueError("Downloaded size differs from remote listing")
        if path.lower().endswith(".md"):
            dest.read_text("utf-8-sig")

    def once(self):
        if not self.settings.source_sync.get("enabled"):
            return {"enabled": False}
        with self.store.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            before = self.listing()
            policy = self.settings.policy()
            old_pointer = self.root / "current.json"
            old = json.loads(old_pointer.read_text()) if old_pointer.exists() else {}
            generation = uuid.uuid4().hex
            dest = self.root / "generations" / generation
            dest.mkdir(parents=True)
            manifest = {"generation": generation, "at": now(), "files": before, "hashes": {}}
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
                    # Retain on-demand attachments when their remote metadata is unchanged.
                    target.write_bytes(previous.read_bytes())
                elif markdown:
                    self.download(path, target, metadata)
                else:
                    continue
                manifest["hashes"][path] = digest(target.read_bytes())
            after = self.listing()
            if before != after:
                raise ValueError("Remote changed during sync; previous generation retained")
            # A stable object listing is not a transaction across an Obsidian upload.
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
        with self.store.lock:
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
            if self.listing().get(path) != metadata:
                raise ValueError("Attachment changed; synchronize source first")
            temp = file.with_name(file.name + "." + uuid.uuid4().hex + ".download")
            self.download(path, temp, metadata)
            if self.listing().get(path) != metadata:
                temp.unlink(missing_ok=True)
                raise ValueError("Attachment changed during download")
            os.replace(temp, file)
            return file


class GitSync:
    def __init__(self, store: Store):
        self.store = store
        self.settings = store.settings
        self.root = self.settings.wiki_root
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
        with self.store.lock:
            if self.git("status", "--porcelain"):
                raise ValueError("Refuse initialization in a dirty checkout")
            self.git("fetch", "origin")
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
            self.git("fetch", "origin")
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
            self.git("push", "origin", "HEAD:refs/heads/" + self.branch)
            self.store.reindex("wiki")
            return self.store.state(
                "git", {"ok": True, "at": now(), "head": self.git("rev-parse", "HEAD"), "branch": self.branch}
            )


def worker(settings: Settings, *, once=False):
    store = Store(settings)
    from .knowledge import Knowledge

    Knowledge(store).recover()
    next_sync = 0
    while True:
        jobs = []
        if time.monotonic() >= next_sync:
            jobs = [("source_sync", SourceSync(store).once), ("git", GitSync(store).once)]
            next_sync = time.monotonic() + 300
        for key, job in jobs:
            try:
                job()
            except Exception as exc:
                prior = store.state(key) or {}
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
                # Paths may be sensitive; operational stdout carries only error class.
                print(f"{key}: {type(exc).__name__}", flush=True)
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
            return
        time.sleep(2)
