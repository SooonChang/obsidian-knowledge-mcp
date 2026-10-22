from __future__ import annotations

import json
import time
import unicodedata
import uuid

import yaml
from filelock import FileLock
from pydantic import BaseModel, Field

from .config import Settings, fold, relative_path
from .knowledge import SECRET, note_path
from .storage import Store, atomic_write, digest, now, split_frontmatter
from .sync import CommandFailure, SyncValidationError, named_remote, rclone

MAX_PENDING = 20
RESULT_DAYS = 7
WINDOWS_FORBIDDEN = set('<>"|?*')


class DraftRequest(BaseModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    path: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=200_000)
    reason: str = Field(min_length=1, max_length=1000)
    metadata: dict = Field(default_factory=dict)


def drafts_remote(settings: Settings) -> str:
    remote = settings.drafts.get("remote", "")
    if not named_remote(remote):
        raise ValueError("drafts requires a named rclone remote")
    source = settings.source_sync.get("remote", "")
    # The source remote holds a read-only key; never reuse it for writing.
    if source and remote.split(":", 1)[0] == source.split(":", 1)[0]:
        raise ValueError("drafts remote must use credentials separate from source_sync")
    return remote.rstrip("/")


def draft_path(path: str) -> str:
    """A path below the drafts directory; the server adds the directory itself."""
    path = relative_path(note_path(unicodedata.normalize("NFC", path)))
    parts = path.split("/")
    if len(parts) > 3:
        raise ValueError("Draft path may have at most two folders")
    if any(set(p) & WINDOWS_FORBIDDEN or p != p.rstrip(" .") for p in parts):
        raise ValueError("Draft path contains characters Obsidian cannot sync on Windows")
    return path


def render(request: DraftRequest) -> str:
    meta, body, _ = split_frontmatter(request.body)
    meta = meta | request.metadata
    meta.update(
        generated_by="knowledge-mcp", request_id=request.request_id, created_at=now(), reason=request.reason
    )
    return "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n\n" + body.lstrip("\n")


class Drafts:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.queue = settings.data_dir / "draft-uploads"
        self.results = settings.data_dir / "draft-results"
        self.lock = FileLock(str(settings.data_dir / "drafts.lock"), timeout=30)

    def enabled(self) -> bool:
        return bool(self.settings.drafts.get("enabled"))

    def pending(self) -> int:
        return len(list(self.queue.glob("*.json"))) if self.queue.exists() else 0

    def queue_draft(self, request: DraftRequest):
        if not self.enabled():
            raise ValueError("Draft upload is disabled")
        drafts_remote(self.settings)
        path = draft_path(request.path)
        if SECRET.search(request.model_dump_json()) or SECRET.search(request.body):
            raise ValueError("Potential credential detected; remove or mask it")
        fingerprint = digest(request.model_dump_json())
        target = self.settings.drafts_dir() + "/" + path
        with self.lock:
            for directory in (self.results, self.queue):
                file = directory / (request.request_id + ".json")
                if file.exists():
                    job = json.loads(file.read_text("utf-8"))
                    if job["fingerprint"] != fingerprint:
                        raise ValueError("Request ID reused with different content")
                    return job.get("result") or {
                        "pending": True,
                        "request_id": request.request_id,
                        "path": target,
                    }
            if self.pending() >= MAX_PENDING:
                raise ValueError("Too many drafts awaiting upload; retry later")
            job = {"fingerprint": fingerprint, "path": path, "text": render(request), "queued_at": now()}
            atomic_write(
                self.queue / (request.request_id + ".json"), json.dumps(job, ensure_ascii=False).encode()
            )
        return {
            "pending": True,
            "request_id": request.request_id,
            "path": target,
            "reason": "Upload queued; call again with the same request to see the result",
        }


class DraftUpload(Drafts):
    """Create new Markdown files under the drafts directory only; never overwrite or delete."""

    def __init__(self, store: Store):
        super().__init__(store.settings)
        self.store = store

    def command(self, *args):
        return rclone(*args)

    def existing(self, root: str) -> dict[str, int]:
        try:
            records = json.loads(self.command("lsjson", root, "--recursive", "--files-only"))
        except CommandFailure as exc:
            if exc.returncode == 3:  # Directory not found: no drafts yet.
                return {}
            raise
        return {fold(r["Path"]): r["Size"] for r in records}

    def upload(self, path: str, text: str):
        root = drafts_remote(self.settings) + "/" + self.settings.drafts_dir()
        if fold(path) in self.existing(root):
            raise SyncValidationError(
                "already_exists", "A draft with this name already exists; choose another path"
            )
        data = text.encode()
        staging = self.settings.data_dir / "draft-staging" / (uuid.uuid4().hex + ".md")
        atomic_write(staging, data)
        try:
            self.command(
                "copyto", str(staging), root + "/" + path, "--ignore-existing", "--s3-no-check-bucket"
            )
        finally:
            staging.unlink(missing_ok=True)
        # S3 has no create-if-absent; confirm that our bytes landed rather than a concurrent file.
        if self.existing(root).get(fold(path)) != len(data):
            raise SyncValidationError(
                "upload_unverified", "Uploaded draft could not be verified; check the vault"
            )
        return {"uploaded": True, "path": self.settings.drafts_dir() + "/" + path, "at": now()}

    def process(self):
        if not self.enabled() or not self.queue.exists():
            return
        for file in sorted(self.queue.glob("*.json"), key=lambda p: p.stat().st_mtime):
            job = json.loads(file.read_text("utf-8"))
            try:
                result = self.upload(job["path"], job["text"])
            except SyncValidationError as exc:
                result = {"uploaded": False, "code": exc.code, "message": str(exc)}
            except CommandFailure:
                # Never expose rclone errors or remote paths; retry with a new request ID.
                result = {
                    "uploaded": False,
                    "code": "upload_failed",
                    "message": "Upload failed; inspect operator logs",
                }
            with self.lock:
                atomic_write(
                    self.results / file.name,
                    json.dumps(
                        {"fingerprint": job["fingerprint"], "result": result}, ensure_ascii=False
                    ).encode(),
                )
                file.unlink()
            self.store.state("drafts", {"ok": result["uploaded"], "at": now(), "code": result.get("code")})
        self.prune()

    def prune(self):
        if not self.results.exists():
            return
        for file in self.results.glob("*.json"):
            if time.time() - file.stat().st_mtime > RESULT_DAYS * 86400:
                file.unlink(missing_ok=True)
