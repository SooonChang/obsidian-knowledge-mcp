import copy
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from knowledge_mcp.attachments import read_attachment
from knowledge_mcp.knowledge import Knowledge, SaveRequest, find_note, note_path
from knowledge_mcp.semantic import hybrid_search
from knowledge_mcp.storage import Store, chunks, split_frontmatter


def test_service_store_can_start_during_writer_job(store):
    code = (
        "from pathlib import Path; from knowledge_mcp.config import Settings; "
        "from knowledge_mcp.storage import Store; "
        f"s=Settings(wiki_root=Path({str(store.settings.wiki_root)!r}), "
        f"data_dir=Path({str(store.settings.data_dir)!r})); "
        "store=Store(s); print('ready')"
    )
    # Use a separate process so a thread-local/reentrant lock cannot hide the
    # startup contention that occurs between Compose services.
    with store.lock:
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=5
        )
    assert result.stdout.strip() == "ready"


@pytest.mark.parametrize("target", ["wiki", "source"])
@pytest.mark.parametrize("setting", ["data_dir", "snapshot_dir"])
def test_runtime_paths_cannot_write_into_vault(store, target, setting):
    root = store.settings.wiki_root if target == "wiki" else store.settings.policy().source
    nested = root / "runtime-must-not-exist"
    setattr(store.settings, setting, nested)
    with pytest.raises(ValueError, match="outside both vaults"):
        Store(store.settings)
    assert not nested.exists()


def test_scoped_search_filters_before_candidate_limit(store):
    source = store.settings.policy().source
    for i in range(160):
        (source / f"bulk-{i}.md").write_text("# 검색어\n검색어", encoding="utf-8")
    note = store.settings.wiki_root / "AI-Sessions/wiki/concepts/target.md"
    note.write_text("# 위키 근거\n검색어 " + "다른 내용 " * 100, encoding="utf-8")
    store.reindex()
    assert store.keyword("검색어", "wiki")[0]["path"].endswith("target.md")


@pytest.mark.parametrize(
    "path",
    [
        "../escape.md",
        "/etc/passwd",
        "C:/file.md",
        "a/../x.md",
        ".secret.md",
        "x/.hidden/y.md",
        "a\\b.md",
        "_Template/test.md",
        "_CustomJS/test.md",
    ],
)
def test_source_paths_forbidden(store, path):
    with pytest.raises((ValueError, FileNotFoundError)):
        store.settings.policy().resolve("source", path, exists=False)


def test_settings_source_inside_wiki_rejected(store):
    file = store.settings.wiki_root / "wiki.config.json"
    raw = json.loads(file.read_text())
    raw["source_root"] = "."
    file.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        store.settings.policy()


def test_symlink_escape(store, tmp_path):
    source = store.settings.policy().source
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    try:
        (source / "linked.md").symlink_to(outside)
    except OSError:
        pytest.skip("Windows symlink creation privilege not available")
    with pytest.raises(ValueError):
        store.settings.policy().resolve("source", "linked.md")


def test_search_korean_short_and_deleted(store):
    store.reindex()
    for query in ("프리퀀시", "전환", "앱", "US"):
        assert store.keyword(query, "source")[0]["path"] == "실험.md"
    source = store.settings.policy().source / "실험.md"
    source.write_text("# 변경\n다른 내용", encoding="utf-8")
    assert not store.keyword("프리퀀시", "source")  # stale snippets never returned
    store.reindex()
    assert store.keyword("변경", "source")
    source.unlink()
    store.reindex()
    assert not store.keyword("변경", "source")


def test_no_hidden_or_attachments_indexed(store):
    source = store.settings.policy().source
    for folder in (".hidden", "_Template", "_Attachments"):
        (source / folder).mkdir()
        (source / folder / "a.md").write_text("검색불가텍스트", encoding="utf-8")
    store.reindex()
    assert not store.keyword("검색불가텍스트", "source")


def test_frontmatter_and_code_boundaries():
    text = "---\ntags: [test]\n---\n# 제목\n\n```python\n# not a heading\nprint(1)\n```\n\n## 다음\n본문"
    result = list(chunks(text))
    assert all("not a heading" not in row[0] for row in result)
    assert any("```python" in row[3] and "print(1)" in row[3] for row in result)
    assert result[-1][0] == "제목/다음"
    assert split_frontmatter(text)[0]["tags"] == ["test"]


def test_save_idempotent_and_source_unchanged(store, request_data):
    source = store.settings.policy().source / "실험.md"
    before = source.read_bytes()
    knowledge = Knowledge(store)
    request = SaveRequest(**request_data)
    result = knowledge.save(request)
    assert result["saved"]
    assert knowledge.save(request) == result
    assert source.read_bytes() == before
    log = (store.settings.wiki_root / "AI-Sessions/log.md").read_text("utf-8")
    assert log.count("request_001") == 1
    assert store.keyword("프리퀀시", "wiki")
    assert "frequency" in (store.settings.wiki_root / "AI-Sessions/index.md").read_text("utf-8")


@pytest.mark.parametrize("related", ["AI-Sessions/wiki/projects/app", "AI-Sessions/wiki/projects/app.md"])
def test_related_accepts_wikilink_target_without_suffix(store, request_data, related):
    project = store.settings.wiki_root / "AI-Sessions/wiki/projects/app.md"
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text("# 앱", encoding="utf-8")
    request_data["related"] = [related]
    assert Knowledge(store).save(SaveRequest(**request_data))["saved"]
    saved = store.settings.wiki_root / request_data["changes"][0]["path"]
    assert "- [[AI-Sessions/wiki/projects/app]]" in saved.read_text("utf-8")


def test_missing_related_note_reports_wiki_path(store, request_data):
    request_data["related"] = ["AI-Sessions/wiki/projects/missing"]
    with pytest.raises(ValueError, match="Related wiki note not found: AI-Sessions/wiki/projects/missing.md"):
        Knowledge(store).save(SaveRequest(**request_data))


def test_filter_rejected_without_log(store, request_data):
    request_data["save_filter"] = []
    before = (store.settings.wiki_root / "AI-Sessions/log.md").read_bytes()
    assert not Knowledge(store).save(SaveRequest(**request_data))["saved"]
    assert before == (store.settings.wiki_root / "AI-Sessions/log.md").read_bytes()


def test_version_conflict_and_request_id_collision(store, request_data):
    k = Knowledge(store)
    k.save(SaveRequest(**request_data))
    request_data["changes"][0]["body"] += "\n추가"
    with pytest.raises(ValueError, match="reused"):
        k.save(SaveRequest(**request_data))
    request_data["request_id"] = "request_002"
    with pytest.raises(ValueError, match="Version conflict"):
        k.save(SaveRequest(**request_data))


def test_two_concurrent_updates_preserve_first(store, request_data):
    k = Knowledge(store)
    k.save(SaveRequest(**request_data))
    path = request_data["changes"][0]["path"]
    version = store.read("wiki", path)["hash"]
    requests = []
    for number in (2, 3):
        data = copy.deepcopy(request_data)
        data["request_id"] = f"request_00{number}"
        data["changes"][0]["expected_hash"] = version
        data["changes"][0]["body"] = f"# 변경 {number}"
        requests.append(SaveRequest(**data))

    def save(req):
        try:
            return k.save(req)["saved"]
        except ValueError:
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(save, requests)) == [False, True]


def test_crash_recovery(store, request_data, monkeypatch):
    import knowledge_mcp.knowledge as module

    original = module.atomic_write
    counter = 0

    def failing(path, data):
        nonlocal counter
        counter += 1
        if counter == 3:
            raise OSError("simulated crash")
        original(path, data)

    monkeypatch.setattr(module, "atomic_write", failing)
    with pytest.raises(OSError):
        Knowledge(store).save(SaveRequest(**request_data))
    monkeypatch.setattr(module, "atomic_write", original)
    Knowledge(store).recover()
    result = Knowledge(store).save(SaveRequest(**request_data))
    assert result["saved"]
    assert store.keyword("프리퀀시", "wiki")


def test_recovery_does_not_overwrite_external_change(store, request_data, monkeypatch):
    import knowledge_mcp.knowledge as module

    original = module.atomic_write
    counter = 0

    def failing(path, data):
        nonlocal counter
        counter += 1
        if counter == 3:
            raise OSError("crash")
        original(path, data)

    monkeypatch.setattr(module, "atomic_write", failing)
    with pytest.raises(OSError):
        Knowledge(store).save(SaveRequest(**request_data))
    monkeypatch.setattr(module, "atomic_write", original)
    target = store.settings.wiki_root / request_data["changes"][0]["path"]
    target.write_text("동료 수정", encoding="utf-8")
    with pytest.raises(ValueError, match="Recovery conflict"):
        Knowledge(store).recover()
    assert target.read_text("utf-8") == "동료 수정"


def test_source_requires_current_receipt(store, request_data):
    k = Knowledge(store)
    request_data["changes"][0]["metadata"]["source"] = ["실험.md"]
    with pytest.raises(ValueError, match="receipt"):
        k.save(SaveRequest(**request_data))
    receipt = store.read("source", "실험.md")
    request_data["source_receipts"] = [receipt["receipt"]]
    (store.settings.policy().source / "실험.md").write_text("changed")
    with pytest.raises(ValueError, match="changed since"):
        k.save(SaveRequest(**request_data))
    request_data["source_receipts"] = [store.read("source", "실험.md")["receipt"]]
    k.save(SaveRequest(**request_data))
    meta, _, _ = split_frontmatter(
        (store.settings.wiki_root / request_data["changes"][0]["path"]).read_text("utf-8")
    )
    assert meta["source_checked"]


def test_unconfirmed_decision_draft(store, request_data):
    request_data["changes"][0]["path"] = "AI-Sessions/wiki/decisions/test.md"
    request_data["changes"][0]["metadata"] = {"type": "decision", "status": "active"}
    Knowledge(store).save(SaveRequest(**request_data))
    text = (store.settings.wiki_root / request_data["changes"][0]["path"]).read_text("utf-8")
    assert split_frontmatter(text)[0]["status"] == "draft"


@pytest.mark.parametrize("path", ["AGENTS.md", "wiki.config.json", "../zettelkasten/실험.md"])
def test_save_cannot_write_source_or_rules(store, request_data, path):
    request_data["changes"][0]["path"] = path
    with pytest.raises(ValueError):
        Knowledge(store).save(SaveRequest(**request_data))


def test_secret_rejected(store, request_data):
    request_data["changes"][0]["body"] += "\npassword: supersecretvalue"
    with pytest.raises(ValueError, match="credential"):
        Knowledge(store).save(SaveRequest(**request_data))


def test_html_link_and_script_removal(store):
    source = store.settings.policy().source
    (source / "report.html").write_text("<h1>결과</h1><script>evil()</script><p>전환율</p>", encoding="utf-8")
    (source / "실험.md").write_text("# 실험\n[결과](report.html)", encoding="utf-8")
    result = read_attachment(store, "report.html", "실험.md", "source")
    assert "전환율" in result["content"] and "evil" not in result["content"]
    assert result["receipt"]
    (source / "other.html").write_text("unlinked")
    with pytest.raises(ValueError, match="explicitly linked"):
        read_attachment(store, "other.html", "실험.md", "source")


def test_semantic_failure_keeps_keyword(store, monkeypatch):
    store.reindex()
    monkeypatch.setattr(store.settings, "token", lambda: "t")
    store.settings.semantic = {"enabled": True, "url": "http://127.0.0.1:1", "timeout_seconds": 0.05}
    result = hybrid_search(store, "프리퀀시")
    assert result["mode"] == "keyword"
    assert result["semantic_error"] == "ConnectError"
    assert result["results"]


def test_hybrid_reports_hit_origin_and_usage(store, monkeypatch):
    import httpx
    import numpy as np

    from knowledge_mcp import semantic

    store.reindex()
    store.settings.semantic = {"enabled": True}
    monkeypatch.setattr(store.settings, "token", lambda: "t")
    query = np.asarray([1, 0, 0, 0], dtype=np.float32)
    with store.db() as db:
        for row in db.execute("SELECT id, hash, path FROM chunks").fetchall():
            # Only the operations note is semantically close; keyword finds the source note.
            vector = query if row["path"].endswith("wiki-operations.md") else np.asarray([0, 1, 0, 0])
            db.execute(
                "INSERT INTO vectors VALUES (?,?,?,?)",
                (row["id"], "m", row["hash"], np.asarray(vector, dtype=np.float32).tobytes()),
            )
    response = httpx.Response(
        200, json={"vector": query.tolist(), "model": "m"}, request=httpx.Request("POST", "http://x")
    )
    monkeypatch.setattr(semantic.httpx, "post", lambda *a, **k: response)
    result = hybrid_search(store, "프리퀀시", "both", 5)
    assert result["mode"] == "hybrid"
    assert result["matched"]["semantic"] >= 1 and result["matched"]["both"] == 1
    store.record_search("auto", "both", result, 12.5)
    store.record_search(
        "auto",
        "auto",
        {"mode": "keyword", "semantic": "unavailable", "semantic_error": "ConnectError", "results": []},
        3.0,
    )
    usage = store.search_usage()
    assert usage["searches"] == 2
    assert usage["semantic"] == {"ready": 1, "unavailable": 1}
    assert usage["semantic_errors"] == {"ConnectError": 1}
    assert usage["results"]["semantic_only"] == result["matched"]["semantic"]
    with store.db() as db:
        assert "프리퀀시" not in json.dumps([list(r) for r in db.execute("SELECT * FROM searches")])


def test_reindex_missing_source_preserves_wiki(store):
    source = store.settings.policy().source
    source.rename(source.with_name("moved"))
    result = store.reindex()
    assert "source" in result["errors"]
    assert "wiki" in result["counts"]


def test_body_links_outside_wiki_warn_instead_of_failing(store, request_data):
    request_data["changes"][0]["body"] = (
        "# 프리퀀시\n\n[[2.Projects/app-ml 배치 명령어]]와 ![[화면.png]] 참고. `[[inline-example]]`\n\n"
        "```yaml\nlink:\n  - \"[[code-example]]\"\n```\n"
    )
    result = Knowledge(store).save(SaveRequest(**request_data))
    assert result["saved"]
    assert [w["target"] for w in result["warnings"]] == ["2.Projects/app-ml 배치 명령어", "화면.png"]
    issues = Knowledge(store).lint()["issues"]
    targets = {i["target"] for i in issues if i["problem"] == "unresolved link"}
    assert targets == {"2.Projects/app-ml 배치 명령어", "화면.png"}


@pytest.mark.parametrize("reference", ["AI-Sessions/index", "AI-Sessions/index.md", "wiki.config.json"])
def test_wiki_evidence_accepts_wikilink_target(store, request_data, reference):
    request_data["evidence"].append({"kind": "wiki", "reference": reference, "summary": "기존 문서"})
    assert Knowledge(store).save(SaveRequest(**request_data))["saved"]


def test_missing_wiki_evidence_reports_wiki_path(store, request_data):
    request_data["evidence"].append({"kind": "wiki", "reference": "AI-Sessions/missing", "summary": "없음"})
    with pytest.raises(ValueError, match="Wiki evidence not found: AI-Sessions/missing$"):
        Knowledge(store).save(SaveRequest(**request_data))


def test_note_path_and_missing_note_error_hide_server_paths(store):
    assert note_path(" AI-Sessions/index ") == "AI-Sessions/index.md"
    assert note_path("AI-Sessions/index.md") == "AI-Sessions/index.md"
    with pytest.raises(ValueError, match="read_attachment"):
        note_path("보고서.html")
    assert store.read("wiki", note_path("AI-Sessions/index"))["path"] == "AI-Sessions/index.md"
    with pytest.raises(FileNotFoundError) as error:
        store.read("wiki", "AI-Sessions/missing.md")
    assert str(error.value) == "Not found in wiki vault: AI-Sessions/missing.md"
    assert str(store.settings.wiki_root) not in str(error.value)


def test_find_note_resolves_unique_filename_link(store):
    policy = store.settings.policy()
    for path in ("projects/a/app-ml 배치 명령어.md", "projects/a/중복.md", "projects/b/중복.md", "루트.md"):
        (policy.source / path).parent.mkdir(parents=True, exist_ok=True)
        (policy.source / path).write_text("# 노트", encoding="utf-8")
    assert find_note(policy, "source", "app-ml 배치 명령어") == "projects/a/app-ml 배치 명령어.md"
    assert find_note(policy, "source", "루트") == "루트.md"
    assert find_note(policy, "source", "projects/b/중복") == "projects/b/중복.md"
    assert find_note(policy, "source", "없는노트") == "없는노트.md"
    with pytest.raises(ValueError, match="projects/a/중복.md, projects/b/중복.md"):
        find_note(policy, "source", "중복")
    note = store.read("source", find_note(policy, "source", "app-ml 배치 명령어"))
    assert note["path"] == "projects/a/app-ml 배치 명령어.md"


def test_index_entry_added_when_only_prefix_sibling_is_indexed(store, request_data):
    index = store.settings.wiki_root / "AI-Sessions/index.md"
    index.write_text("# 지도\n- [[AI-Sessions/wiki/concepts/frequency-v2]]: 다른 문서\n", encoding="utf-8")
    Knowledge(store).save(SaveRequest(**request_data))
    text = index.read_text("utf-8")
    assert "- [[AI-Sessions/wiki/concepts/frequency]]: " in text
    assert "frequency-v2]]: 다른 문서\n" in text
    issues = Knowledge(store).lint()["issues"]
    assert not [i for i in issues if i["path"] == request_data["changes"][0]["path"]]
