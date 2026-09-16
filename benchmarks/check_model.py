"""Compare FP32 and INT8 embeddings using public/synthetic text, never real vault content."""

import json
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

from knowledge_mcp.semantic import Encoder

directory = Path(sys.argv[1])
started = time.perf_counter()
encoder = Encoder(directory)
load_seconds = time.perf_counter() - started
options = ort.SessionOptions()
options.intra_op_num_threads = 2
options.inter_op_num_threads = 1
fp32 = ort.InferenceSession(
    str(directory / "model.onnx"), sess_options=options, providers=["CPUExecutionProvider"]
)
questions = json.loads(Path(sys.argv[2]).read_text("utf-8"))
cosines, latencies = [], []
for item in questions:
    text = item["query"]
    encoded = encoder.tokenizer.encode("query: " + text)
    ids = np.asarray([encoded.ids], dtype=np.int64)
    mask = np.asarray([encoded.attention_mask], dtype=np.int64)
    hidden = fp32.run(None, {"input_ids": ids, "attention_mask": mask})[0]
    vector = (hidden * mask[..., None]).sum(axis=1) / mask.sum(axis=1, keepdims=True)
    vector = vector[0] / np.linalg.norm(vector[0])
    started = time.perf_counter()
    quantized = encoder.encode(text, query=True)
    latencies.append(time.perf_counter() - started)
    cosines.append(float(vector @ quantized))
print(
    json.dumps(
        {
            "questions": len(questions),
            "int8_load_seconds": load_seconds,
            "fp32_int8_cosine_min": min(cosines),
            "fp32_int8_cosine_mean": float(np.mean(cosines)),
            "int8_median_seconds": float(np.median(latencies)),
            "int8_p95_seconds": float(np.percentile(latencies, 95)),
        },
        indent=2,
    )
)
