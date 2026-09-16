from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from filelock import FileLock

from .config import Settings


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def local_date() -> str:
    return datetime.now(timezone(timedelta(hours=9))).date().isoformat()


def digest(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def split_frontmatter(text: str) -> tuple[dict, str, int]:
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                meta = yaml.safe_load("\n".join(lines[1:i])) or {}
                if not isinstance(meta, dict):
                    raise ValueError("Frontmatter must be a mapping")
                return meta, "\n".join(lines[i + 1 :]), i + 1
        raise ValueError("Unclosed frontmatter")
    return {}, text, 0


def chunks(text: str):
    _, body, offset = split_frontmatter(text)
    lines, section, block, start, fence = body.splitlines(), [], [], offset + 1, False
    for idx, line in enumerate(lines, offset + 1):
        heading = re.match(r"^(#{1,6})\s+(.+)$", line) if not fence else None
        boundary = heading or (not fence and not line.strip() and sum(map(len, block)) >= 1800)
        if boundary and block:
            yield "/".join(section), start, idx - 1, "\n".join(block)
            block, start = [], idx
        if heading:
            level = len(heading[1])
            section = section[: level - 1] + [heading[2]]
        if line.startswith(("```", "~~~")):
            fence = not fence
        block.append(line)
    if block:
        yield "/".join(section), start, offset + len(lines), "\n".join(block)


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        policy = settings.policy()
        runtime = settings.data_dir.resolve()
        for root in (policy.wiki, policy.declared_source):
            if runtime == root or runtime.is_relative_to(root):
                raise ValueError("data_dir must be outside both vaults")
            if settings.snapshot_dir and settings.snapshot_dir.resolve().is_relative_to(root):
                raise ValueError("snapshot_dir must be outside both vaults")
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(settings.data_dir / "writer.lock"), timeout=30)
        self.db_path = settings.data_dir / "knowledge.db"
        with self.lock, self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                  vault TEXT, path TEXT, hash TEXT, title TEXT, metadata TEXT,
                  mtime REAL, indexed_at TEXT, PRIMARY KEY(vault,path));
                CREATE TABLE IF NOT EXISTS chunks (
                  id TEXT PRIMARY KEY, vault TEXT, path TEXT, hash TEXT,
                  heading TEXT, start INTEGER, end INTEGER, body TEXT, title TEXT);
                CREATE VIRTUAL TABLE IF NOT EXISTS words USING fts5(
                  id UNINDEXED, title, heading, body, tokenize='unicode61');
                CREATE VIRTUAL TABLE IF NOT EXISTS grams USING fts5(
                  id UNINDEXED, title, heading, body, tokenize='trigram');
                CREATE TABLE IF NOT EXISTS receipts (
                  id TEXT PRIMARY KEY, vault TEXT, path TEXT, hash TEXT, checked_at TEXT,
                  start INTEGER, end INTEGER);
                CREATE TABLE IF NOT EXISTS requests (
                  id TEXT PRIMARY KEY, fingerprint TEXT, result TEXT);
                CREATE TABLE IF NOT EXISTS states (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS vectors (
                  id TEXT, model TEXT, chunk_hash TEXT, vector BLOB, PRIMARY KEY(id,model));
            """)

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def state(self, key: str, value=None):
        with self.db() as db:
            if value is not None:
                db.execute("INSERT OR REPLACE INTO states VALUES (?,?)", (key, json.dumps(value)))
                return value
            row = db.execute("SELECT value FROM states WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def remove(self, db, vault, path):
        ids = [r[0] for r in db.execute("SELECT id FROM chunks WHERE vault=? AND path=?", (vault, path))]
        for ident in ids:
            db.execute("DELETE FROM words WHERE id=?", (ident,))
            db.execute("DELETE FROM grams WHERE id=?", (ident,))
            db.execute("DELETE FROM vectors WHERE id=?", (ident,))
        db.execute("DELETE FROM chunks WHERE vault=? AND path=?", (vault, path))
        db.execute("DELETE FROM documents WHERE vault=? AND path=?", (vault, path))

    def index_file(self, db, policy, vault, path):
        file = policy.resolve(vault, path)
        data = file.read_bytes()
        if len(data) > 4_000_000:
            raise ValueError("Note exceeds 4 MB indexing limit")
        hashed = digest(data)
        row = db.execute("SELECT hash FROM documents WHERE vault=? AND path=?", (vault, path)).fetchone()
        if row and row[0] == hashed:
            return False
        text = data.decode("utf-8-sig")
        meta, body, _ = split_frontmatter(text)
        title = str(
            meta.get("title")
            or next((x[2:] for x in body.splitlines() if x.startswith("# ")), Path(path).stem)
        )
        self.remove(db, vault, path)
        db.execute(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,?)",
            (vault, path, hashed, title, json.dumps(meta, default=str), file.stat().st_mtime, now()),
        )
        labels = " ".join(str(meta.get(x, "")) for x in ("tags", "aliases"))
        for heading, start, end, content in chunks(text):
            ident = digest(f"{vault}:{path}:{start}:{hashed}")
            db.execute(
                "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?)",
                (ident, vault, path, hashed, heading, start, end, content, title),
            )
            for table in ("words", "grams"):
                db.execute(
                    f"INSERT INTO {table}(id,title,heading,body) VALUES (?,?,?,?)",
                    (ident, title + " " + labels + " " + path, heading, content),
                )
        return True

    def reindex(self, scope="both"):
        if scope not in ("wiki", "source", "both"):
            raise ValueError("Invalid indexing scope")
        policy, counts, errors = self.settings.policy(), {}, {}
        with self.lock:
            for vault in ("wiki", "source") if scope == "both" else (scope,):
                try:
                    files = list(policy.notes(vault))
                except OSError:
                    errors[vault] = "vault unavailable; previous index retained"
                    continue
                seen, changed = set(), 0
                with self.db() as db:
                    for path, _ in files:
                        seen.add(path)
                        try:
                            changed += self.index_file(db, policy, vault, path)
                        except (ValueError, UnicodeError, OSError, yaml.YAMLError) as exc:
                            self.remove(db, vault, path)
                            errors[f"{vault}:{path}"] = type(exc).__name__
                    old = db.execute("SELECT path FROM documents WHERE vault=?", (vault,)).fetchall()
                    for row in old:
                        if row[0] not in seen:
                            self.remove(db, vault, row[0])
                counts[vault] = {"notes": len(seen), "changed": changed}
        return self.state("index", {"at": now(), "counts": counts, "errors": errors})

    def read(self, vault, path, start=1, end=None, section=None):
        if start < 1 or (end is not None and end < start):
            raise ValueError("Invalid line range")
        file = self.settings.policy().resolve(vault, path)
        if file.stat().st_size > 4_000_000:
            raise ValueError("Note too large")
        data = file.read_bytes()
        text = data.decode("utf-8-sig")
        lines = text.splitlines()
        if start > max(len(lines), 1):
            raise ValueError("start is beyond the note")
        if section:
            found = [(s, e) for h, s, e, _ in chunks(text) if h == section or h.endswith("/" + section)]
            if not found:
                raise ValueError("Section not found; use a heading from search results")
            start, end = found[0]
        end = min(end if end is not None else len(lines), start + 399, len(lines))
        receipt = uuid.uuid4().hex
        with self.db() as db:
            db.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?,?,?)",
                (receipt, vault, path, digest(data), now(), start, end),
            )
        return {
            "vault": vault,
            "path": path,
            "hash": digest(data),
            "receipt": receipt,
            "checked_at": now(),
            "start_line": start,
            "end_line": end,
            "total_lines": len(lines),
            "content": "\n".join(lines[start - 1 : end]),
            "truncated": end < len(lines),
        }

    def keyword(self, query: str, scope="both", limit=10):
        if scope not in ("auto", "wiki", "source", "both") or not 1 <= limit <= 50:
            raise ValueError("Invalid scope or limit")
        terms = re.findall(r"\w+", query, flags=re.UNICODE)[:12]
        if not terms:
            return []
        candidates = {}
        with self.db() as db:
            for table, usable in (("words", terms), ("grams", [x for x in terms if len(x) >= 3])):
                if not usable:
                    continue
                match = " OR ".join('"' + x.replace('"', '""') + '"' for x in usable)
                scope_clause = " AND c.vault=?" if scope in ("wiki", "source") else ""
                params = (match, scope) if scope_clause else (match,)
                rows = db.execute(
                    f"""SELECT c.*, bm25({table},0,5,3,1) AS score FROM {table}
                    JOIN chunks c ON c.id={table}.id WHERE {table} MATCH ?{scope_clause}
                    ORDER BY score LIMIT 150""",
                    params,
                ).fetchall()
                for rank, row in enumerate(rows):
                    if scope in ("wiki", "source") and row["vault"] != scope:
                        continue
                    item = candidates.setdefault(row["id"], [dict(row), 0.0])
                    item[1] += 1 / (60 + rank + 1)
            short = [x.lower() for x in terms if len(x) < 3]
            if short:
                for row in db.execute("SELECT * FROM chunks"):
                    if scope in ("wiki", "source") and row["vault"] != scope:
                        continue
                    hay = (row["title"] + " " + row["heading"] + " " + row["body"]).lower()
                    if any(t in hay for t in short):
                        item = candidates.setdefault(row["id"], [dict(row), 0.0])
                        item[1] += 1 / 61
        ranked = sorted(candidates.values(), key=lambda x: x[1], reverse=True)
        return self.present([row for row, _ in ranked], scope, limit)

    def present(self, rows, scope, limit):
        policy, result, seen = self.settings.policy(), [], set()
        if scope == "auto":
            rows = sorted(rows, key=lambda r: r["vault"] != "wiki")
        for row in rows:
            key = (row["vault"], row["path"])
            if key in seen:
                continue
            try:
                # Never serve stale excerpts, excluded paths or retired snapshots.
                file = policy.resolve(*key)
                if digest(file.read_bytes()) != row["hash"]:
                    continue
            except (ValueError, OSError):
                continue
            seen.add(key)
            meta = {}
            with self.db() as db:
                doc = db.execute("SELECT metadata FROM documents WHERE vault=? AND path=?", key).fetchone()
                if doc:
                    meta = json.loads(doc[0])
            result.append(
                {k: row[k] for k in ("id", "vault", "path", "hash", "heading", "start", "end", "title")}
                | {
                    "excerpt": row["body"][:1000],
                    "sources": meta.get("source", []),
                    "source_checked": meta.get("source_checked"),
                }
            )
            if len(result) >= limit:
                break
        return result
