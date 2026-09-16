from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


def relative_path(value: str) -> str:
    if not value or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("Invalid relative vault path")
    parts = value.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        raise ValueError("Hidden or traversing path is forbidden")
    if PurePosixPath(value).is_absolute():
        raise ValueError("Absolute path is forbidden")
    return value


def glob_regex(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        if pattern[i : i + 3] == "**/":
            out += "(?:.*/)?"
            i += 3
        elif pattern[i : i + 2] == "**":
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile("^" + out.rstrip("/") + "$")


@dataclass
class Settings:
    wiki_root: Path
    data_dir: Path
    # A snapshot-store pointer is resolved per operation, never a vault-provided symlink.
    snapshot_dir: Path | None = None
    host: str = "127.0.0.1"
    port: int = 8765
    allowed_hosts: list[str] = field(default_factory=lambda: ["127.0.0.1:*", "localhost:*", "[::1]:*"])
    allowed_origins: list[str] = field(default_factory=list)
    semantic: dict = field(default_factory=dict)
    source_sync: dict = field(default_factory=dict)
    git: dict = field(default_factory=dict)

    @classmethod
    def load(cls, file: str | Path) -> Settings:
        file = Path(file).resolve()
        raw = tomllib.loads(file.read_text("utf-8"))

        def path(value):
            p = Path(value)
            return (file.parent / p).resolve() if not p.is_absolute() else p.resolve()

        if raw.get("semantic", {}).get("model_dir"):
            raw["semantic"]["model_dir"] = str(path(raw["semantic"]["model_dir"]))
        return cls(
            wiki_root=path(raw["wiki_root"]),
            data_dir=path(raw["data_dir"]),
            snapshot_dir=path(raw["snapshot_dir"]) if raw.get("snapshot_dir") else None,
            **{k: v for k, v in raw.items() if k not in ("wiki_root", "data_dir", "snapshot_dir")},
        )

    def token(self) -> str:
        file = os.environ.get("KNOWLEDGE_TOKEN_FILE")
        token = Path(file).read_text("utf-8").strip() if file else os.environ.get("KNOWLEDGE_TOKEN", "")
        if len(token) < 32:
            raise ValueError("KNOWLEDGE_TOKEN_FILE or KNOWLEDGE_TOKEN must contain at least 32 characters")
        return token

    def policy(self) -> VaultPolicy:
        return VaultPolicy(self)


class VaultPolicy:
    def __init__(self, settings: Settings):
        self.wiki = settings.wiki_root.resolve(strict=True)
        raw = json.loads((self.wiki / "wiki.config.json").read_text("utf-8-sig"))
        if not isinstance(raw.get("source_root"), str) or not raw["source_root"].strip():
            raise ValueError("Missing source_root")
        if "~" in raw["source_root"] or "$" in raw["source_root"] or "%" in raw["source_root"]:
            raise ValueError("source_root does not expand home or environment variables")
        self.source = (self.wiki / raw["source_root"]).resolve()
        self.declared_source = self.source
        if self.source == self.wiki or self.source.is_relative_to(self.wiki):
            raise ValueError("source_root cannot be inside wiki")
        self.attachments = relative_path(raw["attachments_dir"])
        if not isinstance(raw.get("exclude"), list) or any(not isinstance(x, str) for x in raw["exclude"]):
            raise ValueError("exclude must be a list of globs")
        self.patterns = [glob_regex(x) for x in raw["exclude"]]
        self.snapshot_dir = settings.snapshot_dir
        if settings.snapshot_dir:
            current = settings.snapshot_dir / "current.json"
            if current.exists():
                ident = json.loads(current.read_text("utf-8"))["generation"]
                if not re.fullmatch(r"[a-f0-9]{32}", ident):
                    raise ValueError("Invalid source generation")
                self.source = (settings.snapshot_dir / "generations" / ident).resolve(strict=True)
                if self.source.is_relative_to(self.wiki):
                    raise ValueError("Source snapshot cannot be inside wiki")
            else:
                self.source = settings.snapshot_dir / "unavailable"

    def allowed(self, vault: str, path: str) -> bool:
        try:
            relative_path(path)
        except ValueError:
            return False
        if vault not in ("wiki", "source"):
            return False
        if vault == "source":
            parts = path.split("/")
            for n in range(1, len(parts) + 1):
                prefix = "/".join(parts[:n])
                if any(p.fullmatch(prefix) for p in self.patterns):
                    return False
        return True

    def resolve(self, vault: str, path: str, *, exists=True) -> Path:
        if not self.allowed(vault, path):
            raise ValueError("Path is outside the allowed vault scope")
        root = self.wiki if vault == "wiki" else self.source
        if not root.is_dir():
            raise FileNotFoundError(f"{vault} vault unavailable")
        current = root
        for part in path.split("/"):
            current = current / part
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise ValueError("Linked filesystem paths are forbidden")
            if current.exists() and getattr(current.stat(), "st_file_attributes", 0) & 2:
                raise ValueError("Hidden filesystem paths are forbidden")
        resolved = current.resolve(strict=exists)
        if not resolved.is_relative_to(root.resolve()):
            raise ValueError("Path escapes vault")
        if vault == "source" and resolved.is_relative_to(self.wiki):
            raise ValueError("Wiki area is excluded from source")
        return resolved

    def notes(self, vault: str):
        root = self.wiki if vault == "wiki" else self.source
        if not root.is_dir():
            raise FileNotFoundError(f"{vault} vault unavailable")
        for directory, dirs, files in os.walk(root, followlinks=False):
            kept = []
            for name in dirs:
                rel = (Path(directory) / name).relative_to(root).as_posix()
                try:
                    self.resolve(vault, rel)
                    if vault != "source" or not (
                        rel == self.attachments or rel.startswith(self.attachments + "/")
                    ):
                        kept.append(name)
                except (ValueError, OSError):
                    pass
            dirs[:] = kept
            for name in files:
                if not name.lower().endswith(".md"):
                    continue
                rel = (Path(directory) / name).relative_to(root).as_posix()
                try:
                    yield rel, self.resolve(vault, rel)
                except (ValueError, OSError):
                    pass
