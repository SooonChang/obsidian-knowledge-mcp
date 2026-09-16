import json

import numpy as np
import pytest

from knowledge_mcp.semantic import Encoder, embed_once
from knowledge_mcp.storage import digest


def tiny_model(directory):
    onnx = pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")
    from onnx import TensorProto, helper, numpy_helper
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from tokenizers.processors import TemplateProcessing

    directory.mkdir()
    vocabulary = {
        "[UNK]": 0,
        "[PAD]": 1,
        "[CLS]": 2,
        "[SEP]": 3,
        "query": 4,
        "passage": 5,
        ":": 6,
        "실험": 7,
        "검색": 8,
        "제목": 9,
    }
    tokenizer = Tokenizer(WordLevel(vocabulary, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.post_processor = TemplateProcessing(
        single="[CLS] $A [SEP]", special_tokens=[("[CLS]", 2), ("[SEP]", 3)]
    )
    tokenizer.save(str(directory / "tokenizer.json"))
    table = np.random.default_rng(5).normal(size=(10, 384)).astype(np.float32)
    node = helper.make_node("Gather", ["weights", "input_ids"], ["last_hidden_state"], axis=0)
    graph = helper.make_graph(
        [node],
        "synthetic-test-embedding",
        [
            helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["batch", "sequence"]),
            helper.make_tensor_value_info("attention_mask", TensorProto.INT64, ["batch", "sequence"]),
        ],
        [helper.make_tensor_value_info("last_hidden_state", TensorProto.FLOAT, ["batch", "sequence", 384])],
        [numpy_helper.from_array(table, "weights")],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], ir_version=9)
    onnx.save(model, directory / "model.int8.onnx")
    manifest = {
        "model": "synthetic-test-only",
        "files": {
            name: digest((directory / name).read_bytes()) for name in ("tokenizer.json", "model.int8.onnx")
        },
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


def test_real_onnx_runtime_pooling_windows_resume(store, tmp_path):
    directory = tiny_model(tmp_path / "tiny")
    encoder = Encoder(directory)
    vector = encoder.encode("실험 검색", query=True)
    assert vector.shape == (384,)
    assert np.isclose(np.linalg.norm(vector), 1.0)
    windows = encoder.windows("제목", "실험", "검색 " * 1000)
    assert len(windows) > 1
    store.reindex()
    first = embed_once(store, encoder)
    assert first["processed"] > 0
    assert embed_once(store, encoder)["processed"] == 0
    (store.settings.policy().source / "실험.md").write_text("# 실험\n변경된 내용", encoding="utf-8")
    store.reindex("source")
    assert embed_once(store, encoder)["processed"] > 0


def test_model_checksum_validation(tmp_path):
    directory = tiny_model(tmp_path / "tiny")
    (directory / "tokenizer.json").write_text("{}")
    with pytest.raises(ValueError, match="checksum"):
        Encoder(directory)
