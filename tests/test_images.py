import base64
import io
import json
from pathlib import Path

import pytest
from filelock import FileLock
from PIL import Image

from knowledge_mcp.images import decode_image, read_image
from knowledge_mcp.knowledge import Knowledge, SaveRequest
from knowledge_mcp.storage import digest
from knowledge_mcp.sync import ImageSync, SourceSync, worker


def picture(color="red", size=(32, 16), format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format=format)
    return output.getvalue()


@pytest.fixture
def image_remote(store, tmp_path, monkeypatch):
    store.settings.snapshot_dir = tmp_path / "snapshots"
    store.settings.source_sync = {"enabled": True, "remote": "notes:bucket/vault"}
    remote = {"note.md": b"![[_Attachments/nested/test.png]]", "_Attachments/nested/test.png": picture()}
    copies = []

    def command(self, *args):
        with FileLock(store.lock.lock_file, timeout=0):
            pass
        if args[0] == "lsjson":
            return json.dumps([{"Path": k, "Size": len(v), "ModTime": digest(v)} for k, v in remote.items()])
        assert args[0] == "copyto"
        path = args[1].removeprefix("notes:bucket/vault/")
        copies.append(path)
        Path(args[2]).write_bytes(remote[path])
        return ""

    monkeypatch.setattr(SourceSync, "command", command)
    return remote, copies


def test_image_mirror_incremental_change_delete_and_no_snapshot_copies(store, image_remote):
    remote, copies = image_remote
    path = "_Attachments/nested/test.png"
    remote.update({"outside.png": picture(), "_Attachments/.hidden.png": picture(), "_CustomJS/a.png": picture()})
    sync = ImageSync(store)
    assert sync.once()["files"] == 1
    file = store.settings.policy().resolve("source", path)
    initial_stat = file.stat()
    for _ in range(3):
        SourceSync(store).once()
        sync.once()
    assert copies.count(path) == 1
    assert file.stat().st_mtime_ns == initial_stat.st_mtime_ns
    assert file.stat().st_ino == initial_stat.st_ino
    assert not list((store.settings.snapshot_dir / "generations").rglob("*.png"))
    remote[path] = picture("blue")
    sync.once()
    assert file.read_bytes() == remote[path]
    assert copies.count(path) == 2
    del remote[path]
    sync.once()
    assert not file.exists()
    assert not store.settings.policy().image_manifest()["files"]


def test_failed_or_unstable_image_sync_retains_published_mirror(store, image_remote, monkeypatch):
    remote, _ = image_remote
    path = "_Attachments/nested/test.png"
    sync = ImageSync(store)
    sync.once()
    pointer = store.settings.snapshot_dir / "images.json"
    before = pointer.read_bytes()
    file = store.settings.policy().resolve("source", path)
    data = file.read_bytes()
    remote[path] = picture("blue")
    original = sync.download

    def failed(*args):
        raise RuntimeError("Download failed")

    monkeypatch.setattr(sync, "download", failed)
    with pytest.raises(RuntimeError):
        sync.once()
    monkeypatch.setattr(sync, "download", original)
    listing = sync.listing
    calls = 0

    def unstable():
        nonlocal calls
        calls += 1
        return listing() if calls == 1 else {}

    monkeypatch.setattr(sync, "listing", unstable)
    with pytest.raises(ValueError, match="changed during"):
        sync.once()
    assert pointer.read_bytes() == before
    assert file.read_bytes() == data
    assert not list(store.settings.snapshot_dir.glob("image-stage-*"))


def test_excluded_images_removed_without_following_symlinks(store, image_remote, tmp_path):
    ImageSync(store).once()
    root = store.settings.snapshot_dir / "images"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.png").write_bytes(picture())
    (root / "linked").symlink_to(outside, target_is_directory=True)
    config = store.settings.wiki_root / "wiki.config.json"
    raw = json.loads(config.read_text())
    raw["exclude"].append("_Attachments/nested")
    config.write_text(json.dumps(raw))
    assert ImageSync(store).once()["files"] == 0
    assert not (root / "_Attachments/nested/test.png").exists()
    assert (outside / "keep.png").exists()
    assert not (root / "linked").exists()


def test_invalid_image_does_not_publish(store, image_remote):
    remote, _ = image_remote
    remote["_Attachments/nested/test.png"] = b"not an image"
    with pytest.raises(ValueError, match="Invalid or damaged"):
        ImageSync(store).once()
    assert not (store.settings.snapshot_dir / "images.json").exists()


@pytest.mark.parametrize("format,suffix", [("PNG", ".png"), ("JPEG", ".jpg"), ("WEBP", ".webp")])
def test_read_local_image_returns_preview_and_original_hash(store, format, suffix):
    source = store.settings.policy().source
    path = "_Attachments/a" + suffix
    file = source / path
    file.parent.mkdir()
    original = picture(size=(3000, 1500), format=format)
    file.write_bytes(original)
    (source / "notes").mkdir()
    (source / "notes/ref.md").write_text(f"![](../{path})")
    result = read_image(store, path, "notes/ref.md", "source")
    info = result.structuredContent
    assert info["hash"] == digest(original)
    assert info["display_size"] == [2048, 1024]
    assert info["original_size"] == [3000, 1500]
    image = result.content[1]
    assert image.type == "image" and image.mimeType == "image/png"
    with Image.open(io.BytesIO(base64.b64decode(image.data))) as preview:
        assert preview.size == (2048, 1024)
    assert file.read_bytes() == original
    with pytest.raises(ValueError, match="changed since read"):
        read_image(store, path, "notes/ref.md", "source", "stale")


def test_gif_first_frame_and_limits(store, monkeypatch):
    source = store.settings.policy().source
    (source / "_Attachments").mkdir()
    output = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(
        output, format="GIF", save_all=True, append_images=[Image.new("RGB", (10, 10), "blue")],
    )
    (source / "_Attachments/animated.gif").write_bytes(output.getvalue())
    (source / "ref.md").write_text("![[animated.gif]]")
    result = read_image(store, "_Attachments/animated.gif", "ref.md", "source")
    assert result.structuredContent["first_frame_only"] is True
    assert result.structuredContent["range_unit"] == "frames"
    with Image.open(io.BytesIO(base64.b64decode(result.content[1].data))) as preview:
        assert preview.getpixel((0, 0))[:3] == (255, 0, 0)
    monkeypatch.setattr("knowledge_mcp.images.MAX_IMAGE_BYTES", 10)
    with pytest.raises(ValueError, match="25 MB"):
        decode_image(output.getvalue(), ".gif")
    monkeypatch.setattr("knowledge_mcp.images.MAX_IMAGE_BYTES", 25_000_000)
    monkeypatch.setattr("knowledge_mcp.images.MAX_IMAGE_PIXELS", 10)
    with pytest.raises(ValueError, match="40 million"):
        decode_image(output.getvalue(), ".gif")


@pytest.mark.parametrize("link", ["![[_Attachments/missing.png]]", "![[missing.png]]"])
def test_pending_before_first_sync_and_link_validation(store, tmp_path, link):
    store.settings.snapshot_dir = tmp_path / "snapshots"
    note = store.settings.wiki_root / "ref.md"
    note.write_text(link)
    result = read_image(store, "_Attachments/missing.png", "ref.md")
    assert result.structuredContent["pending"]
    assert not list(store.settings.wiki_root.rglob("*.png"))
    note.write_text("No link")
    with pytest.raises(ValueError, match="explicitly linked"):
        read_image(store, "_Attachments/missing.png", "ref.md")


def test_remote_image_receipt_save_and_stale_receipt(store, image_remote, request_data):
    remote, _ = image_remote
    path = "_Attachments/nested/test.png"
    SourceSync(store).once()
    ImageSync(store).once()
    result = read_image(store, path, "note.md", "source")
    request_data["source_receipts"] = [result.structuredContent["receipt"]]
    request_data["changes"][0]["metadata"]["source"] = [path]
    knowledge = Knowledge(store)
    assert knowledge.save(SaveRequest(**request_data))["saved"]
    saved = store.settings.wiki_root / request_data["changes"][0]["path"]
    assert "이미지 첫 프레임" in saved.read_text()
    assert knowledge.sources(request_data["changes"][0]["path"])[0]["available"]
    assert not list(store.settings.wiki_root.rglob("*.png"))
    remote[path] = picture("blue")
    ImageSync(store).once()
    request_data["request_id"] = "stale_receipt_request"
    with pytest.raises(ValueError, match="Source changed"):
        knowledge.save(SaveRequest(**request_data))


def test_remote_short_link_duplicates_and_symlink_rejected(store, image_remote, tmp_path):
    remote, _ = image_remote
    remote["note.md"] = b"![[test.png]]"
    remote["_Attachments/duplicate/test.png"] = picture("blue")
    SourceSync(store).once()
    ImageSync(store).once()
    with pytest.raises(ValueError, match="Ambiguous"):
        read_image(store, "_Attachments/nested/test.png", "note.md", "source")
    del remote["_Attachments/duplicate/test.png"]
    ImageSync(store).once()
    assert read_image(store, "_Attachments/nested/test.png", "note.md", "source").content[1].type == "image"
    file = store.settings.policy().resolve("source", "_Attachments/nested/test.png")
    file.unlink()
    outside = tmp_path / "outside.png"
    outside.write_bytes(picture())
    file.symlink_to(outside)
    with pytest.raises(ValueError, match="Linked"):
        read_image(store, "_Attachments/nested/test.png", "note.md", "source")


def test_incomplete_publication_fails_closed(store, image_remote):
    ImageSync(store).once()
    note = store.settings.wiki_root / "ref.md"
    note.write_text("![[_Attachments/nested/test.png]]")
    file = store.settings.policy().resolve("source", "_Attachments/nested/test.png")
    file.write_bytes(picture("blue"))
    with pytest.raises(ValueError, match="publication incomplete"):
        read_image(store, "_Attachments/nested/test.png", "ref.md")


def test_worker_records_image_failure_independently(store, image_remote, monkeypatch):
    def failed(*args):
        raise ValueError("Invalid or damaged image")

    monkeypatch.setattr(ImageSync, "download", failed)
    worker(store.settings, once=True)
    assert store.state("source_sync")["ok"]
    assert store.state("image_sync")["ok"] is False
    assert store.settings.policy().resolve("source", "note.md").exists()


def test_sync_rejects_oversized_image_before_downloading(store, image_remote, monkeypatch):
    sync = ImageSync(store)
    path = "_Attachments/huge.png"
    monkeypatch.setattr(sync, "listing", lambda: {path: {"size": 25_000_001}})
    with pytest.raises(ValueError, match="size limit"):
        sync.once()
    assert not image_remote[1]
    assert not store.settings.policy().image_manifest()


def test_shared_image_root_symlink_rejected(store, image_remote, tmp_path):
    root = store.settings.snapshot_dir
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "images").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Linked"):
        ImageSync(store).once()
    note = store.settings.wiki_root / "ref.md"
    note.write_text("![[_Attachments/nested/test.png]]")
    with pytest.raises(ValueError, match="Linked"):
        read_image(store, "_Attachments/nested/test.png", "ref.md")
    assert not list(outside.iterdir())


def test_worker_preserves_safe_image_error_and_progress(store, image_remote, capsys):
    remote, _ = image_remote
    remote["_Attachments/nested/test.png"] = b"broken image"
    worker(store.settings, once=True)
    state = store.state("image_sync")
    assert state["status"] == "failed"
    assert state["code"] == "invalid_image"
    assert state["message"] == "Invalid or damaged image"
    assert state["stage"] == "validating"
    assert state["total"] == 1
    assert state["processed"] == 0
    assert state["file_id"] == digest("_Attachments/nested/test.png")
    assert "invalid_image" in capsys.readouterr().out
    assert "_Attachments" not in json.dumps(state)
    assert not store.settings.policy().image_manifest()


def test_image_progress_visible_during_download_and_completed(store, image_remote, monkeypatch):
    sync = ImageSync(store)
    original = sync.download
    states = []

    def download(*args):
        states.append(store.state("image_sync"))
        return original(*args)

    monkeypatch.setattr(sync, "download", download)
    result = sync.once()
    assert states[0]["status"] == "running"
    assert states[0]["stage"] == "downloading"
    assert states[0]["total"] == 1
    assert result["status"] == "complete"
    assert result["downloaded"] == result["processed"] == 1
    assert result["last_success_at"] == result["at"]
    assert "file_id" not in result
    again = sync.once()
    assert again["reused"] == 1
    assert again["downloaded"] == 0


def test_unknown_image_error_does_not_expose_exception_message(store, image_remote, monkeypatch):
    sync = ImageSync(store)

    def download(*args):
        raise ValueError("private://credentials@bucket/secret-file.png")

    monkeypatch.setattr(sync, "download", download)
    with pytest.raises(ValueError):
        sync.once()
    state = store.state("image_sync")
    assert state["status"] == "failed"
    assert state["code"] == "image_sync_failed"
    assert "credentials" not in json.dumps(state)


@pytest.mark.parametrize("format,suffix", [("PNG", ".jpg"), ("JPEG", ".png"), ("WEBP", ".jpeg"), ("GIF", ".png")])
def test_mislabeled_images_sync_and_read_without_modifying_original(store, image_remote, format, suffix):
    remote, copies = image_remote
    path = "_Attachments/nested/mislabeled" + suffix
    original = picture(format=format)
    remote.clear()
    remote.update({"note.md": f"![[{path}]]".encode(), path: original})
    SourceSync(store).once()
    sync = ImageSync(store)
    assert sync.once()["status"] == "complete"
    file = store.settings.policy().resolve("source", path)
    assert file.read_bytes() == original
    result = read_image(store, path, "note.md", "source")
    assert result.structuredContent["hash"] == digest(original)
    assert result.content[1].mimeType == "image/png"
    with Image.open(io.BytesIO(base64.b64decode(result.content[1].data))) as preview:
        assert preview.size == (32, 16)
    assert file.read_bytes() == original
    assert sync.once()["reused"] == 1
    assert copies.count(path) == 1
    assert not list(store.settings.wiki_root.rglob("*" + suffix))


def test_unsupported_actual_format_still_rejected(store, image_remote):
    remote, _ = image_remote
    remote["_Attachments/nested/test.png"] = picture(format="TIFF")
    with pytest.raises(ValueError, match="Invalid or damaged image"):
        ImageSync(store).once()
    assert store.state("image_sync")["code"] == "invalid_image"
    assert not store.settings.policy().image_manifest()
