from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import numpy as np

from .storage import Store, atomic_write, digest, now


class Encoder:
    def __init__(self, directory: Path, threads=2):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.directory = directory
        self.manifest = json.loads((directory / "manifest.json").read_text())
        for name, expected in self.manifest["files"].items():
            if Path(name).name != name or digest((directory / name).read_bytes()) != expected:
                raise ValueError("Model artifact checksum mismatch")
        self.model_id = digest(json.dumps(self.manifest, sort_keys=True))
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=256)
        self.tokenizer.enable_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(directory / "model.int8.onnx"), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.lock = threading.Lock()
        self.query_waiting = threading.Event()

    def encode(self, text: str, query=False):
        prefix = "query: " if query else "passage: "
        if query:
            self.query_waiting.set()
        try:
            with self.lock:
                # Tokenization shares mutable truncation/padding configuration.
                encoded = self.tokenizer.encode(prefix + text)
                ids = np.asarray([encoded.ids], dtype=np.int64)
                mask = np.asarray([encoded.attention_mask], dtype=np.int64)
                output = self.session.run(None, {"input_ids": ids, "attention_mask": mask})[0]
                if output.ndim == 3:
                    vec = (output * mask[..., None]).sum(axis=1) / mask.sum(axis=1, keepdims=True)
                else:
                    vec = output
                vector = np.asarray(vec[0], dtype=np.float32)
                return vector / max(float(np.linalg.norm(vector)), 1e-12)
        finally:
            if query:
                self.query_waiting.clear()

    def windows(self, title, heading, body):
        # Windows are embedding-only; returned excerpts retain the original Markdown block.
        with self.lock:
            self.tokenizer.no_truncation()
            try:
                prefix = (title + " / " + heading)[:200] + "\n"
                prefix_ids = self.tokenizer.encode("passage: " + prefix, add_special_tokens=False).ids[:64]
                body_ids = self.tokenizer.encode(body, add_special_tokens=False).ids
                size = 256 - len(prefix_ids) - 4
                prefix_text = self.tokenizer.decode(prefix_ids, skip_special_tokens=True)
                # encode() adds passage:, so remove this prefix from the decoded title.
                if prefix_text.startswith("passage:"):
                    prefix_text = prefix_text[len("passage:") :].strip()
                return [
                    prefix_text
                    + "\n"
                    + self.tokenizer.decode(body_ids[i : i + size], skip_special_tokens=True)
                    for i in range(0, max(len(body_ids), 1), max(size - 32, 1))
                ]
            finally:
                self.tokenizer.enable_truncation(max_length=256)


def embed_once(store: Store, encoder: Encoder):
    with store.db() as db:
        rows = db.execute(
            """SELECT c.* FROM chunks c
           LEFT JOIN vectors v ON v.id=c.id AND v.model=? AND v.chunk_hash=c.hash
           WHERE v.id IS NULL""",
            (encoder.model_id,),
        ).fetchall()
    completed, started = 0, time.monotonic()
    for row in rows:
        while encoder.query_waiting.is_set():
            time.sleep(0.05)
        try:
            file = store.settings.policy().resolve(row["vault"], row["path"])
            if digest(file.read_bytes()) != row["hash"]:
                continue
        except (ValueError, OSError):
            continue
        vectors = []
        for text in encoder.windows(row["title"], row["heading"], row["body"]):
            while encoder.query_waiting.is_set():
                time.sleep(0.05)
            vectors.append(encoder.encode(text))
        array = np.asarray(vectors, dtype=np.float32)
        # Store all windows; search takes the best score per parent chunk.
        with store.db() as db:
            current = db.execute("SELECT hash FROM chunks WHERE id=?", (row["id"],)).fetchone()
            if current and current[0] == row["hash"]:
                db.execute(
                    "INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)",
                    (row["id"], encoder.model_id, row["hash"], array.tobytes()),
                )
        completed += 1
        elapsed = time.monotonic() - started
        store.state(
            "semantic",
            {
                "model": encoder.model_id,
                "completed_this_run": completed,
                "pending_this_run": len(rows) - completed,
                "at": now(),
                "chunks_per_second": completed / max(elapsed, 0.001),
            },
        )
        time.sleep(0.02)
    return {"processed": completed, "seconds": time.monotonic() - started}


def hybrid_search(store: Store, query, scope="auto", limit=10, mode="auto"):
    if mode not in ("auto", "keyword", "hybrid"):
        raise ValueError("Invalid search mode")
    lexical = store.keyword(query, scope, max(limit, 30))
    cfg = store.settings.semantic
    if mode == "keyword" or not cfg.get("enabled"):
        return {"mode": "keyword", "semantic": "disabled", "results": lexical[:limit]}
    try:
        token = store.settings.token()
        response = httpx.post(
            cfg.get("url", "http://127.0.0.1:8766") + "/embed",
            json={"text": query},
            headers={"Authorization": "Bearer " + token},
            timeout=cfg.get("timeout_seconds", 5),
        )
        response.raise_for_status()
        payload = response.json()
        vector = np.asarray(payload["vector"], dtype=np.float32)
        with store.db() as db:
            rows = db.execute(
                """SELECT c.*, v.vector FROM vectors v JOIN chunks c ON c.id=v.id
                WHERE v.model=? AND v.chunk_hash=c.hash""",
                (payload["model"],),
            ).fetchall()
        scored = []
        for row in rows:
            if scope in ("wiki", "source") and row["vault"] != scope:
                continue
            matrix = np.frombuffer(row["vector"], dtype=np.float32).reshape(-1, len(vector))
            scored.append((float((matrix @ vector).max()), dict(row)))
        semantic = store.present([r for _, r in sorted(scored, key=lambda x: x[0], reverse=True)], scope, 30)
        merged = {}
        for hits in (lexical, semantic):
            for rank, hit in enumerate(hits):
                item = merged.setdefault((hit["vault"], hit["path"]), [0, hit])
                item[0] += 1 / (61 + rank)
        results = [x[1] for x in sorted(merged.values(), key=lambda x: x[0], reverse=True)]
        if scope == "auto":
            results.sort(key=lambda x: x["vault"] != "wiki")
        return {
            "mode": "hybrid" if semantic else "keyword",
            "semantic": "ready" if semantic else "indexing",
            "results": results[:limit],
            "embedding": store.state("semantic"),
        }
    except (httpx.HTTPError, ValueError, OSError, KeyError):
        return {"mode": "keyword", "semantic": "unavailable", "results": lexical[:limit]}


def export_vectors(store: Store, file: Path):
    import base64

    with store.db() as db:
        rows = db.execute("""SELECT c.id,c.vault,c.path,c.hash,v.model,v.vector
                             FROM vectors v JOIN chunks c ON v.id=c.id AND v.chunk_hash=c.hash""").fetchall()
    entries = [dict(r) | {"vector": base64.b64encode(r["vector"]).decode()} for r in rows]
    atomic_write(file, json.dumps({"format": 1, "entries": entries}).encode())
    return {"exported": len(entries)}


def import_vectors(store: Store, file: Path, model_dir: Path):
    import base64

    manifest = json.loads((model_dir / "manifest.json").read_text())
    model_id = digest(json.dumps(manifest, sort_keys=True))
    data = json.loads(file.read_text())
    if data.get("format") != 1:
        raise ValueError("Unknown embedding export format")
    count = 0
    with store.lock, store.db() as db:
        for item in data["entries"]:
            row = db.execute("SELECT hash FROM chunks WHERE id=?", (item["id"],)).fetchone()
            if not row or row[0] != item["hash"] or item["model"] != model_id:
                continue
            raw = base64.b64decode(item["vector"], validate=True)
            if len(raw) == 0 or len(raw) % (384 * 4):
                raise ValueError("Invalid vector dimensions")
            if not np.isfinite(np.frombuffer(raw, dtype=np.float32)).all():
                raise ValueError("Non-finite vector")
            db.execute(
                "INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)", (item["id"], model_id, item["hash"], raw)
            )
            count += 1
    return {"imported": count}


def prepare_model(directory: Path):
    # Operator-only command; never exposed through MCP.
    import torch
    from huggingface_hub import model_info
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from transformers import AutoModel, AutoTokenizer

    name = "intfloat/multilingual-e5-small"
    revision = model_info(name).sha
    tokenizer = AutoTokenizer.from_pretrained(name, revision=revision)
    model = AutoModel.from_pretrained(name, revision=revision).eval()

    class Wrapper(torch.nn.Module):
        def __init__(self, inner):
            super().__init__()
            self.inner = inner

        def forward(self, input_ids, attention_mask):
            return self.inner(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state

    directory.mkdir(parents=True, exist_ok=True)
    inputs = tokenizer("passage: 업무 지식 검색", return_tensors="pt")
    torch.onnx.export(
        Wrapper(model),
        (inputs["input_ids"], inputs["attention_mask"]),
        str(directory / "model.onnx"),
        input_names=["input_ids", "attention_mask"],
        output_names=["last_hidden_state"],
        dynamic_axes={
            k: {0: "batch", 1: "sequence"} for k in ("input_ids", "attention_mask", "last_hidden_state")
        },
        opset_version=17,
        dynamo=False,
    )
    quantize_dynamic(
        str(directory / "model.onnx"),
        str(directory / "model.int8.onnx"),
        weight_type=QuantType.QInt8,
        op_types_to_quantize=["MatMul"],
    )
    tokenizer.backend_tokenizer.save(str(directory / "tokenizer.json"))
    manifest = {
        "model": name,
        "revision": revision,
        "dimensions": 384,
        "window": 256,
        "overlap": 32,
        "pooling": "masked-mean-l2",
        "files": {
            p.name: digest(p.read_bytes())
            for p in directory.iterdir()
            if p.name in ("model.int8.onnx", "tokenizer.json")
        },
    }
    atomic_write(directory / "manifest.json", json.dumps(manifest, indent=2).encode())
    return manifest
