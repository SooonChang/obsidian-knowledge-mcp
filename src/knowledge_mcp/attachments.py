from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from bs4 import BeautifulSoup
from pypdf import PdfReader

from .config import VaultPolicy
from .storage import Store, atomic_write, digest, now, split_frontmatter


def _filename_matches(store: Store, policy: VaultPolicy, name: str) -> set[str]:
    """Find allowed source files, including attachments not yet downloaded."""
    matches = set()
    for directory, dirs, files in os.walk(policy.source, followlinks=False):
        kept = []
        for child in dirs:
            relative = (Path(directory) / child).relative_to(policy.source).as_posix()
            try:
                policy.resolve("source", relative)
                kept.append(child)
            except (ValueError, OSError):
                pass
        dirs[:] = kept
        if name in files:
            relative = (Path(directory) / name).relative_to(policy.source).as_posix()
            try:
                policy.resolve("source", relative)
                matches.add(relative)
            except (ValueError, OSError):
                pass
    if store.settings.snapshot_dir:
        manifest = json.loads((store.settings.snapshot_dir / "current.json").read_text("utf-8"))
        if manifest["generation"] != policy.source.name:
            raise ValueError("Source snapshot changed; retry reading the attachment")
        for relative in manifest["files"]:
            if PurePosixPath(relative).name != name:
                continue
            try:
                policy.resolve("source", relative, exists=False)
                matches.add(relative)
            except (ValueError, OSError):
                pass
    return matches


def read_attachment(store: Store, path: str, linked_from: str, linked_vault="wiki", page=1):
    policy = store.settings.policy()
    referring = policy.resolve(linked_vault, linked_from).read_text("utf-8-sig")
    meta, _, _ = split_frontmatter(referring)
    sources = meta.get("source", [])
    sources = [sources] if isinstance(sources, str) else sources
    # Require a precise link, not arbitrary text containing an attachment name.
    wiki_links = {unquote(x.strip()) for x in re.findall(r"!?\[\[([^\]|#]+)(?:[^\]]*)\]\]", referring)}
    links = re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", referring)
    # Filename-only wikilinks need unique resolution, even if a root file matches.
    short_links = {x for x in wiki_links if "/" not in x and "\\" not in x and ":" not in x}
    references = {unquote(x.strip("<>")) for x in links} | set(sources) | (wiki_links - short_links)
    source_path = policy.resolve("source", path, exists=False)
    exact = path in references
    if linked_vault == "source":
        for link in references:
            if "://" not in link:
                candidate = (policy.source / PurePosixPath(linked_from).parent / link).resolve()
                exact |= candidate == source_path
    if not exact and source_path.name in short_links:
        matches = _filename_matches(store, policy, source_path.name)
        if path in matches and len(matches) > 1:
            raise ValueError(
                "Ambiguous attachment filename; use a vault-relative path in the referring wikilink"
            )
        exact = matches == {path}
    if not exact:
        raise ValueError("Attachment must be explicitly linked by the referring note")
    if source_path.suffix.lower() not in (".pdf", ".html", ".htm"):
        raise ValueError("Only linked HTML and text PDF are supported; no OCR")
    if not source_path.exists():
        if not store.settings.source_sync.get("enabled"):
            raise FileNotFoundError("Attachment unavailable")
        queue = store.settings.data_dir / "attachment-requests"
        request_file = queue / (digest(path) + ".json")
        atomic_write(request_file, json.dumps({"path": path}).encode())
        return {
            "path": path,
            "pending": True,
            "reason": "Download queued; call again after worker processes it",
        }
    if source_path.stat().st_size > 25_000_000:
        raise ValueError("Attachment exceeds 25 MB")
    data = source_path.read_bytes()
    if source_path.suffix.lower() == ".pdf":
        reader = PdfReader(source_path)
        if page < 1 or page > len(reader.pages):
            raise ValueError("Invalid PDF page")
        selected = reader.pages[page - 1 : page + 3]
        text = "\n\n".join(p.extract_text() or "" for p in selected)
        start, end = page, min(page + 3, len(reader.pages))
        unit = "pages"
        if not text.strip():
            return {"path": path, "available": False, "reason": "No text layer; OCR is not supported"}
    else:
        soup = BeautifulSoup(data, "html.parser")
        for element in soup(["script", "style", "iframe", "object", "noscript"]):
            element.decompose()
        text = soup.get_text("\n", strip=True)
        start, end, unit = 1, min(len(text), 16000), "characters"
    ident = uuid.uuid4().hex
    with store.db() as db:
        db.execute(
            "INSERT INTO receipts VALUES (?,?,?,?,?,?,?)",
            (ident, "source", path, digest(data), now(), start, end),
        )
    return {
        "path": path,
        "vault": "source",
        "hash": digest(data),
        "receipt": ident,
        "checked_at": now(),
        "range_unit": unit,
        "start": start,
        "end": end,
        "content": text[:16000],
        "truncated": len(text) > 16000,
    }
