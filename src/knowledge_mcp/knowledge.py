from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .storage import Store, atomic_write, digest, local_date, now, split_frontmatter

PREFIXES = {
    "source": "sources",
    "concept": "concepts",
    "decision": "decisions",
    "error": "errors",
    "project": "projects",
    "design": "design",
    "dev-task": "dev-tasks",
    "handoff": None,
}
SECRET = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bgh[pousr]_[A-Za-z0-9]{30,}|"
    r"(?im:^\s*(?:password|api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*\S{8,})"
)
LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


class Change(BaseModel):
    path: str
    expected_hash: str | None = None
    body: str = Field(min_length=1, max_length=120_000)
    metadata: dict = Field(default_factory=dict)


class Evidence(BaseModel):
    kind: Literal["conversation", "url", "wiki"]
    reference: str = Field(min_length=1, max_length=2000)
    summary: str = Field(min_length=1, max_length=4000)


class SaveRequest(BaseModel):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    changes: list[Change] = Field(min_length=1, max_length=10)
    save_filter: list[int] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list)
    source_receipts: list[str] = Field(default_factory=list, max_length=100)
    related: list[str] = Field(default_factory=list, max_length=30)


class Knowledge:
    def __init__(self, store: Store):
        self.store = store
        self.journal = store.settings.data_dir / "transactions"
        self.journal.mkdir(exist_ok=True)

    def recover(self):
        with self.store.lock:
            for file in sorted(self.journal.glob("*.json")):
                job = json.loads(file.read_text("utf-8"))
                if job.get("state") != "done":
                    self._apply(job, file)

    def _apply(self, job, journal):
        policy = self.store.settings.policy()
        # Preflight the entire transaction before changing any file.
        for item in job["files"]:
            target = policy.resolve("wiki", item["path"], exists=False)
            current = digest(target.read_bytes()) if target.exists() else None
            if current not in (item["before"], item["after"]):
                raise ValueError(
                    "Recovery conflict: " + item["path"] + "; preserve files and resolve manually"
                )
        for item in job["files"]:
            target = policy.resolve("wiki", item["path"], exists=False)
            current = digest(target.read_bytes()) if target.exists() else None
            if current == item["after"]:
                continue
            if current != item["before"]:
                raise ValueError("Concurrent edit; transaction requires recovery")
            atomic_write(target, base64.b64decode(item["data"]))
        with self.store.db() as db:
            for item in job["files"]:
                self.store.index_file(db, policy, "wiki", item["path"])
            db.execute(
                "INSERT OR REPLACE INTO requests VALUES (?,?,?)",
                (job["id"], job["fingerprint"], json.dumps(job["result"])),
            )
        job["state"] = "done"
        atomic_write(journal, json.dumps(job).encode())

    def save(self, request: SaveRequest):
        fingerprint = digest(request.model_dump_json())
        with self.store.lock:
            self.recover()
            with self.store.db() as db:
                prior = db.execute("SELECT * FROM requests WHERE id=?", (request.request_id,)).fetchone()
                if prior:
                    if prior["fingerprint"] != fingerprint:
                        raise ValueError("Request ID reused with different content")
                    return json.loads(prior["result"])
            if not request.save_filter:
                return {"saved": False, "reason": "저장 필터를 통과하지 않아 저장하지 않았습니다."}
            if not set(request.save_filter) <= {1, 2, 3, 4, 5}:
                raise ValueError("save_filter must contain numbers 1..5")
            if not request.evidence and not request.source_receipts:
                raise ValueError("Evidence or a source read receipt is required")
            if SECRET.search(request.model_dump_json()) or any(
                SECRET.search(c.body) for c in request.changes
            ):
                raise ValueError("Potential credential detected; remove or mask it")
            policy = self.store.settings.policy()
            receipts = {}
            with self.store.db() as db:
                for ident in request.source_receipts:
                    r = db.execute("SELECT * FROM receipts WHERE id=?", (ident,)).fetchone()
                    if not r or r["vault"] != "source":
                        raise ValueError("Unknown source read receipt")
                    file = policy.resolve("source", r["path"])
                    if digest(file.read_bytes()) != r["hash"]:
                        raise ValueError("Source changed since read; read it again")
                    receipts[r["path"]] = dict(r)
            changes = {}
            requested_paths = {c.path for c in request.changes}
            if len(requested_paths) != len(request.changes):
                raise ValueError("Duplicate change path")
            for path in request.related:
                if path not in requested_paths:
                    policy.resolve("wiki", path)
            for evidence in request.evidence:
                if evidence.kind == "wiki":
                    policy.resolve("wiki", evidence.reference)
                if evidence.kind == "url" and not evidence.reference.startswith(("https://", "http://")):
                    raise ValueError("Invalid URL evidence")
            for change in request.changes:
                target = policy.resolve("wiki", change.path, exists=False)
                if not change.path.endswith(".md"):
                    raise ValueError("Only Markdown knowledge files may be saved")
                before = target.read_bytes() if target.exists() else None
                current_hash = digest(before) if before is not None else None
                if current_hash != change.expected_hash:
                    raise ValueError("Version conflict; read_note again: " + change.path)
                previous = split_frontmatter(before.decode("utf-8-sig"))[0] if before else {}
                meta = previous | change.metadata
                typ = meta.get("type")
                if typ not in PREFIXES:
                    raise ValueError("Unknown knowledge type")
                prefix = (
                    "AI-Sessions/conversations/" if typ == "handoff" else f"AI-Sessions/wiki/{PREFIXES[typ]}/"
                )
                if not change.path.startswith(prefix):
                    raise ValueError("Knowledge type/path mismatch")
                status = meta.get("status", "draft")
                if status not in ("draft", "active", "superseded", "archived"):
                    raise ValueError("Unknown status")
                if typ == "decision" and status == "active":
                    if not meta.get("owner") or not meta.get("decision_evidence"):
                        status = "draft"
                    if not request.evidence:
                        status = "draft"
                if status == "superseded" and not meta.get("superseded_by"):
                    raise ValueError("superseded_by is required")
                if previous.get("type") == "decision" and previous.get("status") == "active":
                    old_body = split_frontmatter(before.decode("utf-8-sig"))[1].strip()
                    if old_body not in change.body:
                        raise ValueError(
                            "Preserve the previous active decision text; append a revision or supersede it"
                        )
                sources = meta.get("source", [])
                sources = [sources] if isinstance(sources, str) else sources
                if not isinstance(sources, list):
                    raise ValueError("source must be a path or list")
                previous_sources = previous.get("source", [])
                previous_sources = (
                    [previous_sources] if isinstance(previous_sources, str) else previous_sources
                )
                for path in sources:
                    policy.resolve("source", path)
                    if path not in previous_sources and path not in receipts:
                        raise ValueError("New source requires read_note/read_attachment receipt")
                if sources and all(p in receipts for p in sources):
                    # A receipt may cover only a section: preserve its scope explicitly in the body.
                    meta["source_checked"] = min(
                        datetime.fromisoformat(receipts[p]["checked_at"])
                        .astimezone(timezone(timedelta(hours=9)))
                        .date()
                        .isoformat()
                        for p in sources
                    )
                elif sources:
                    if not previous.get("source_checked") or meta.get("source_checked") != previous.get(
                        "source_checked"
                    ):
                        raise ValueError("Cannot advance source_checked without reading every source")
                elif "source_checked" in meta:
                    raise ValueError("source_checked without source")
                for linked in LINK.findall(change.body):
                    linked = linked if linked.endswith(".md") else linked + ".md"
                    if linked not in requested_paths:
                        policy.resolve("wiki", linked)
                meta.update(
                    type=typ,
                    status=status,
                    date=previous.get("date", local_date()),
                    updated=local_date(),
                    save_filter=sorted(set(request.save_filter)),
                )
                body = change.body.strip()
                body += "\n\n저장 이유: " + request.reason + "\n"
                body += "\n## 이번 갱신 근거\n"
                for ev in request.evidence:
                    body += f"\n- {ev.kind}: {ev.reference} — {ev.summary}\n"
                for path, r in receipts.items():
                    if path in sources:
                        body += f"\n- 원문 {path}, 확인 {r['checked_at']}, 범위 {r['start']}–{r['end']}; 전체 검증을 뜻하지 않음.\n"
                if request.related:
                    body += (
                        "\n## 관련 문서\n"
                        + "\n".join(f"- [[{p.removesuffix('.md')}]]" for p in request.related)
                        + "\n"
                    )
                text = "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n\n" + body
                changes[change.path] = (current_hash, text.encode())
            index_path, log_path = "AI-Sessions/index.md", "AI-Sessions/log.md"
            index = policy.resolve("wiki", index_path).read_bytes()
            log = policy.resolve("wiki", log_path).read_bytes()
            index_text = index.decode("utf-8-sig")
            for path in sorted(requested_paths):
                stem = path.removesuffix(".md")
                new_meta = split_frontmatter(changes[path][1].decode())[0]
                status_tag = f"[status:{new_meta['status']}]"
                if f"[[{stem}" not in index_text:
                    index_text += f"\n- [[{stem}]]: {request.reason.replace(chr(10), ' ')} {status_tag}\n"
                else:
                    lines = index_text.splitlines()
                    for i, line in enumerate(lines):
                        if f"[[{stem}]]" in line or f"[[{stem}|" in line:
                            lines[i] = re.sub(r"\s*\[status:[^\]]+\]", "", line) + " " + status_tag
                    index_text = "\n".join(lines) + "\n"
            changes[index_path] = (digest(index), index_text.encode())
            summary = request.reason.replace("\n", " ")
            log_text = (
                log.decode("utf-8-sig").rstrip()
                + f"\n{datetime.now(timezone(timedelta(hours=9))):%Y-%m-%d %H:%M} | save | {summary} | request:{request.request_id}\n"
            )
            changes[log_path] = (digest(log), log_text.encode())
            result = {
                "saved": True,
                "request_id": request.request_id,
                "paths": sorted(changes),
                "git": "pending" if self.store.settings.git.get("enabled") else "disabled",
                "semantic_validation": "agent-provided evidence; structural checks only",
            }
            job = {
                "id": request.request_id,
                "fingerprint": fingerprint,
                "state": "prepared",
                "result": result,
                "files": [
                    {"path": p, "before": h, "after": digest(data), "data": base64.b64encode(data).decode()}
                    for p, (h, data) in changes.items()
                ],
            }
            journal = self.journal / (request.request_id + ".json")
            atomic_write(journal, json.dumps(job).encode())
            self._apply(job, journal)
            return result

    def sources(self, wiki_path):
        policy = self.store.settings.policy()
        meta, _, _ = split_frontmatter(policy.resolve("wiki", wiki_path).read_text("utf-8-sig"))
        paths = meta.get("source", [])
        paths = [paths] if isinstance(paths, str) else paths
        result = []
        for p in paths:
            try:
                file = policy.resolve("source", p)
                item = {"path": p, "available": True, "mtime": file.stat().st_mtime}
            except (ValueError, OSError):
                item = {"path": p, "available": False}
            result.append(item | {"source_checked": str(meta.get("source_checked", ""))})
        return result

    def lint(self):
        policy, issues = self.store.settings.policy(), []
        index = policy.resolve("wiki", "AI-Sessions/index.md").read_text("utf-8-sig")
        for path, file in policy.notes("wiki"):
            if not path.startswith(("AI-Sessions/wiki/", "AI-Sessions/conversations/")):
                continue
            try:
                text = file.read_text("utf-8-sig")
                meta, _, _ = split_frontmatter(text)
                for key in ("type", "date", "updated", "status", "save_filter"):
                    if key not in meta:
                        issues.append({"path": path, "problem": "missing " + key})
                if not meta.get("save_filter") or not set(meta["save_filter"]) <= {1, 2, 3, 4, 5}:
                    issues.append({"path": path, "problem": "invalid save_filter"})
                if meta.get("source") and not meta.get("source_checked"):
                    issues.append({"path": path, "problem": "missing source_checked"})
                for linked in LINK.findall(text):
                    try:
                        policy.resolve("wiki", linked if linked.endswith(".md") else linked + ".md")
                    except (ValueError, OSError):
                        issues.append({"path": path, "problem": "unresolved link", "target": linked})
                if path.removesuffix(".md") not in index:
                    issues.append({"path": path, "problem": "not directly indexed; check topic map"})
                if SECRET.search(text):
                    issues.append({"path": path, "problem": "possible credential; value omitted"})
                for source in self.sources(path):
                    if not source["available"]:
                        issues.append(
                            {"path": path, "problem": "source unavailable", "target": source["path"]}
                        )
            except (ValueError, yaml.YAMLError, OSError) as exc:
                issues.append({"path": path, "problem": type(exc).__name__})
        return {
            "at": now(),
            "issues": issues,
            "scope": "structural; truth, semantic conflicts and privacy require review",
        }
