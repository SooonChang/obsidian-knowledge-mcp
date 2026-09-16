import base64
import json

import numpy as np

from knowledge_mcp.semantic import export_vectors, import_vectors
from knowledge_mcp.storage import digest


def test_portable_vectors_match_model_and_document(store, tmp_path):
    store.reindex()
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    manifest = {"model": "fixture-only", "dimensions": 384, "files": {}}
    (model_dir / "manifest.json").write_text(json.dumps(manifest))
    model = digest(json.dumps(manifest, sort_keys=True))
    vector = np.ones((2, 384), dtype=np.float32).tobytes()
    with store.db() as db:
        row = db.execute("SELECT * FROM chunks WHERE vault='source' LIMIT 1").fetchone()
        db.execute("INSERT INTO vectors VALUES (?,?,?,?)", (row["id"], model, row["hash"], vector))
    file = tmp_path / "vectors.json"
    assert export_vectors(store, file)["exported"] == 1
    with store.db() as db:
        db.execute("DELETE FROM vectors")
    assert import_vectors(store, file, model_dir)["imported"] == 1
    payload = json.loads(file.read_text())
    payload["entries"][0]["hash"] = "stale"
    file.write_text(json.dumps(payload))
    assert import_vectors(store, file, model_dir)["imported"] == 0


def test_invalid_vector_rejected(store, tmp_path):
    import pytest

    store.reindex()
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    manifest = {"files": {}}
    (model_dir / "manifest.json").write_text(json.dumps(manifest))
    model = digest(json.dumps(manifest, sort_keys=True))
    with store.db() as db:
        row = db.execute("SELECT * FROM chunks LIMIT 1").fetchone()
    file = tmp_path / "bad.json"
    file.write_text(
        json.dumps(
            {
                "format": 1,
                "entries": [
                    {
                        "id": row["id"],
                        "hash": row["hash"],
                        "model": model,
                        "vector": base64.b64encode(b"bad").decode(),
                    }
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="dimensions"):
        import_vectors(store, file, model_dir)
