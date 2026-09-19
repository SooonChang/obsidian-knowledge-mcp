from urllib.parse import quote

import pytest

from knowledge_mcp.attachments import read_attachment
from knowledge_mcp.sync import SourceSync


@pytest.fixture
def html_attachment(store):
    source = store.settings.policy().source
    (source / "referring.md").write_text("[[report.html]]", encoding="utf-8")
    file = source / "report.html"
    file.write_text("<p>분석 결과</p>", encoding="utf-8")
    return file


@pytest.mark.parametrize("length", [0, 1, 15999, 16000, 16001, 32000, 32001])
def test_html_continuation_covers_every_character_and_records_ranges(store, html_attachment, length):
    text = ("가나다🙂xyz" * (length // 7 + 1))[:length]
    html_attachment.write_text(f"<script>excluded</script><p>{text}</p>", encoding="utf-8")
    start, file_hash, pieces = 1, None, []
    while True:
        result = read_attachment(
            store, "report.html", "referring.md", "source", start=start, expected_hash=file_hash
        )
        assert result["total_characters"] == length
        assert result["range_unit"] == "characters"
        assert result["start"] == start
        assert result["end"] == min(start - 1 + 16000, length)
        assert result["content"] == text[start - 1 : result["end"]]
        assert result["truncated"] == (result["end"] < length)
        with store.db() as db:
            receipt = db.execute("SELECT * FROM receipts WHERE id=?", (result["receipt"],)).fetchone()
        assert (receipt["start"], receipt["end"]) == (result["start"], result["end"])
        assert receipt["hash"] == result["hash"]
        assert receipt["checked_at"] == result["checked_at"]
        pieces.append(result["content"])
        file_hash = result["hash"]
        if result["next_start"] is None:
            break
        assert result["next_start"] == result["end"] + 1
        start = result["next_start"]
    assert "".join(pieces) == text


def test_html_custom_character_window(store, html_attachment):
    html_attachment.write_text("<h1>제목</h1><style>excluded</style><p>가나다라마바사</p>", encoding="utf-8")
    result = read_attachment(store, "report.html", "referring.md", "source", start=3, max_chars=4)
    assert result["content"] == "\n가나다"
    assert result["total_characters"] == 10
    assert result["end"] == 6
    assert result["next_start"] == 7


@pytest.mark.parametrize(
    "arguments,message",
    [
        ({"start": 0}, "1-based"),
        ({"start": -1}, "1-based"),
        ({"start": 100}, "beyond"),
        ({"max_chars": 0}, "max_chars"),
        ({"max_chars": 16001}, "max_chars"),
    ],
)
def test_invalid_html_window_does_not_issue_receipt(store, html_attachment, arguments, message):
    with pytest.raises(ValueError, match=message):
        read_attachment(store, "report.html", "referring.md", "source", **arguments)
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_html_continuation_rejects_changed_file(store, html_attachment):
    first = read_attachment(store, "report.html", "referring.md", "source", max_chars=2)
    html_attachment.write_text("<p>바뀐 분석 결과</p>", encoding="utf-8")
    with pytest.raises(ValueError, match="changed since read"):
        read_attachment(
            store,
            "report.html",
            "referring.md",
            "source",
            start=first["next_start"],
            expected_hash=first["hash"],
        )
    with store.db() as db:
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_pdf_page_behavior_is_preserved(store, monkeypatch):
    from types import SimpleNamespace

    source = store.settings.policy().source
    (source / "referring.md").write_text("[[report.pdf]]", encoding="utf-8")
    (source / "report.pdf").write_bytes(b"test PDF")
    pages = [SimpleNamespace(extract_text=lambda n=n: f"Page {n}") for n in range(1, 7)]
    monkeypatch.setattr("knowledge_mcp.attachments.PdfReader", lambda path: SimpleNamespace(pages=pages))
    result = read_attachment(store, "report.pdf", "referring.md", "source", page=2)
    assert result["content"] == "Page 2\n\nPage 3\n\nPage 4\n\nPage 5"
    assert (result["start"], result["end"], result["range_unit"]) == (2, 5, "pages")
    assert "next_start" not in result
    with pytest.raises(ValueError, match="HTML-only"):
        read_attachment(store, "report.pdf", "referring.md", "source", start=16001)


@pytest.mark.parametrize("vault", ["source", "wiki"])
@pytest.mark.parametrize(
    "link",
    [
        "[[보고서.html]]",
        "![[보고서.html]]",
        "[[보고서.html|결과]]",
        "[[보고서.html#결과]]",
        "[[ 보고서.html ]]",
        f"[[{quote('보고서.html')}]]",
    ],
)
def test_filename_wikilink_reads_unique_attachment(store, vault, link):
    policy = store.settings.policy()
    attachment = policy.source / "_Attachments/보고서.html"
    attachment.parent.mkdir()
    attachment.write_text("<p>분석 결과</p><script>hidden()</script>", encoding="utf-8")
    root = policy.source if vault == "source" else policy.wiki
    note = root / "notes/referring.md"
    note.parent.mkdir()
    note.write_text(link, encoding="utf-8")
    result = read_attachment(store, "_Attachments/보고서.html", "notes/referring.md", vault)
    assert result["content"] == "분석 결과"
    assert result["path"] == "_Attachments/보고서.html"
    with store.db() as db:
        receipt = db.execute("SELECT * FROM receipts WHERE id=?", (result["receipt"],)).fetchone()
    assert receipt["path"] == result["path"]


@pytest.mark.parametrize("requested", ["report.html", "_Attachments/report.html"])
def test_duplicate_filename_requires_explicit_link(store, requested):
    source = store.settings.policy().source
    (source / "_Attachments").mkdir()
    for path in ("report.html", "_Attachments/report.html"):
        (source / path).write_text("<p>report</p>", encoding="utf-8")
    note = source / "referring.md"
    note.write_text("[[report.html]]", encoding="utf-8")
    with pytest.raises(ValueError, match="Ambiguous attachment filename"):
        read_attachment(store, requested, "referring.md", "source")
    note.write_text("[[_Attachments/report.html]]", encoding="utf-8")
    assert read_attachment(store, "_Attachments/report.html", "referring.md", "source")["receipt"]


@pytest.mark.parametrize("link", ["report.html", "[report](report.html)", "[[different.html]]"])
def test_plain_text_and_markdown_do_not_authorize_filename_search(store, link):
    source = store.settings.policy().source
    (source / "_Attachments").mkdir()
    (source / "_Attachments/report.html").write_text("report", encoding="utf-8")
    (source / "referring.md").write_text(link, encoding="utf-8")
    with pytest.raises(ValueError, match="explicitly linked"):
        read_attachment(store, "_Attachments/report.html", "referring.md", "source")


def test_excluded_duplicates_are_not_candidates(store):
    source = store.settings.policy().source
    for directory in ("_Attachments", "_Template", ".hidden"):
        (source / directory).mkdir()
        (source / directory / "report.html").write_text("report", encoding="utf-8")
    (source / "referring.md").write_text("[[report.html]]", encoding="utf-8")
    assert read_attachment(store, "_Attachments/report.html", "referring.md", "source")["receipt"]
    with pytest.raises(ValueError, match="allowed vault scope"):
        read_attachment(store, "_Template/report.html", "referring.md", "source")


@pytest.mark.parametrize("duplicate", [False, True])
def test_filename_link_uses_remote_manifest_before_download(store, tmp_path, monkeypatch, duplicate):
    store.settings.snapshot_dir = tmp_path / "snapshots"
    store.settings.source_sync = {"enabled": True, "remote": "notes:bucket/vault"}
    remote = {
        "referring.md": b"[[report.html]]",
        "_Attachments/report.html": b"<p>remote report</p>",
    }
    if duplicate:
        remote["other/report.html"] = b"<p>different report</p>"
    metadata = {path: {"size": len(data), "mtime": "unchanged"} for path, data in remote.items()}

    monkeypatch.setattr(SourceSync, "listing", lambda self: metadata)

    def download(self, path, dest, expected):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(remote[path])

    monkeypatch.setattr(SourceSync, "download", download)
    sync = SourceSync(store)
    sync.once()
    path = "_Attachments/report.html"
    assert not (store.settings.policy().source / path).exists()
    if duplicate:
        with pytest.raises(ValueError, match="Ambiguous attachment filename"):
            read_attachment(store, path, "referring.md", "source")
        assert not (store.settings.data_dir / "attachment-requests").exists()
    else:
        assert read_attachment(store, path, "referring.md", "source")["pending"]
        sync.attachment(path)
        assert read_attachment(store, path, "referring.md", "source")["content"] == "remote report"
