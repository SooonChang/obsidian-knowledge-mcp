from urllib.parse import quote

import pytest

from knowledge_mcp.attachments import read_attachment
from knowledge_mcp.sync import SourceSync


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
