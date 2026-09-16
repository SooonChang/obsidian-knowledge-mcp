from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .config import Settings
from .storage import Store


def main():
    parser = argparse.ArgumentParser(description="Obsidian knowledge MCP operator commands")
    parser.add_argument("--config", default="config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "worker", "sync", "index", "status", "lint", "git-init", "semantic-serve"):
        sub.add_parser(name)
    for name in ("prepare-model", "export-vectors", "import-vectors", "benchmark"):
        cmd = sub.add_parser(name)
        cmd.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.command == "prepare-model":
        from .semantic import prepare_model

        print(json.dumps(prepare_model(args.path), ensure_ascii=False, indent=2))
        return
    settings = Settings.load(args.config)
    store = Store(settings)
    if args.command == "serve":
        from .server import serve

        return serve(settings)
    if args.command == "semantic-serve":
        from .server import semantic_serve

        return semantic_serve(settings)
    if args.command in ("worker", "sync", "git-init"):
        from .sync import GitSync, worker

        result = (
            GitSync(store).initialize()
            if args.command == "git-init"
            else worker(settings, once=args.command == "sync")
        )
    elif args.command == "index":
        result = store.reindex()
    elif args.command == "status":
        result = {k: store.state(k) for k in ("index", "source_sync", "git", "semantic")}
    elif args.command == "lint":
        from .knowledge import Knowledge

        result = Knowledge(store).lint()
    elif args.command == "export-vectors":
        from .semantic import export_vectors

        result = export_vectors(store, args.path)
    elif args.command == "import-vectors":
        from .semantic import import_vectors

        result = import_vectors(store, args.path, Path(settings.semantic["model_dir"]))
    else:
        import numpy as np

        from .semantic import hybrid_search

        questions = json.loads(args.path.read_text("utf-8"))
        if not questions or any(not q.get("expected") for q in questions):
            raise ValueError("Benchmark needs questions with at least one expected reference")
        report = {}
        for mode in ("keyword", "hybrid"):
            times, hits, details = [], [], []
            for item in questions:
                start = time.perf_counter()
                found = hybrid_search(store, item["query"], "both", 10, mode)
                elapsed = time.perf_counter() - start
                paths = {x["vault"] + ":" + x["path"] for x in found["results"]}
                expected = set(item["expected"])
                recall = len(paths & expected) / len(expected)
                times.append(elapsed)
                hits.append(recall)
                details.append(
                    {
                        "query": item["query"],
                        "recall_at_10": recall,
                        "mode_used": found["mode"],
                        "seconds": elapsed,
                    }
                )
            report[mode] = {
                "recall_at_10": sum(hits) / max(len(hits), 1),
                "median_seconds": float(np.median(times)),
                "p95_seconds": float(np.percentile(times, 95)),
                "details": details,
            }
        try:
            import resource

            report["peak_rss_kib_linux"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except ImportError:
            report["peak_rss_kib_linux"] = None
        result = report
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
