"""Exercise the real rclone binary against a local S3-compatible fixture."""

import hashlib
import io
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
from xml.sax.saxutils import escape

import pytest
from PIL import Image

from knowledge_mcp.drafts import DraftRequest, Drafts, DraftUpload
from knowledge_mcp.sync import ImageSync, SourceSync


def decoded_body(handler):
    """Read a PUT body, undoing HTTP and aws-chunked framing if the client used them."""
    if handler.headers.get("Transfer-Encoding") == "chunked":
        raw = b""
        while size := int(handler.rfile.readline().split(b";")[0], 16):
            raw += handler.rfile.read(size)
            handler.rfile.readline()
        handler.rfile.readline()
    else:
        raw = handler.rfile.read(int(handler.headers["Content-Length"]))
    if "x-amz-decoded-content-length" not in handler.headers:
        return raw
    data, rest = b"", raw
    while rest:
        header, rest = rest.split(b"\r\n", 1)
        size = int(header.split(b";")[0], 16)
        if not size:
            break
        data, rest = data + rest[:size], rest[size + 2 :]
    return data


@pytest.fixture
def fake_s3(tmp_path, monkeypatch):
    """Serve objects read-only; PUT succeeds only under notes/AI_Drafts/."""
    objects, writes = {}, []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_PUT(self):
            key = unquote(urlparse(self.path).path).removeprefix("/bucket/")
            writes.append(("PUT", key))
            if not key.startswith("notes/AI_Drafts/"):
                self.send_error(403)
                return
            objects[key] = decoded_body(self)
            self.send_response(200)
            self.send_header("ETag", '"' + hashlib.md5(objects[key]).hexdigest() + '"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            writes.append(("POST", self.path))
            self.send_error(405)

        def do_DELETE(self):
            writes.append(("DELETE", self.path))
            self.send_error(405)

        def do_HEAD(self):
            self.do_GET(head=True)

        def do_GET(self, head=False):
            url = urlparse(self.path)
            key = unquote(url.path).removeprefix("/bucket/")
            if url.path.rstrip("/") == "/bucket":
                query = parse_qs(url.query)
                prefix = query.get("prefix", [""])[0]
                contents = "".join(
                    "<Contents><Key>"
                    + escape(k)
                    + "</Key><Size>"
                    + str(len(v))
                    + "</Size><LastModified>2026-09-15T00:00:00.000Z</LastModified><ETag>&quot;"
                    + hashlib.md5(v).hexdigest()
                    + "&quot;</ETag><StorageClass>STANDARD</StorageClass></Contents>"
                    for k, v in objects.items()
                    if k.startswith(prefix)
                )
                body = (
                    '<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                    "<Name>bucket</Name><Prefix>"
                    + escape(prefix)
                    + "</Prefix><IsTruncated>false</IsTruncated>"
                    + contents
                    + "</ListBucketResult>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/xml")
            elif key in objects:
                body = objects[key]
                self.send_response(200)
                self.send_header("ETag", '"' + hashlib.md5(body).hexdigest() + '"')
                self.send_header("Last-Modified", "Tue, 15 Sep 2026 00:00:00 GMT")
                self.send_header("Content-Type", "text/markdown")
            else:
                self.send_error(404)
                return
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg = tmp_path / "rclone.conf"
    remote = (
        "type = s3\nprovider = Other\nregion = us-east-1\n"
        f"endpoint = http://127.0.0.1:{server.server_port}\n"
        "access_key_id = test-only\nsecret_access_key = test-only\nforce_path_style = true\n"
    )
    cfg.write_text("[notes]\n" + remote + "\n[drafts]\n" + remote)
    monkeypatch.setenv("RCLONE_CONFIG", str(cfg))
    try:
        yield objects, writes
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.skipif(not shutil.which("rclone"), reason="rclone is installed in the Linux test image")
def test_real_rclone_s3_download_never_writes(store, tmp_path, fake_s3):
    objects, writes = fake_s3
    objects.update({"notes/a.md": "# 실제 S3 다운로드\n검증 노트".encode(), "notes/_Template/excluded.md": b"skip"})
    output = io.BytesIO()
    Image.new("RGB", (20, 10), "blue").save(output, format="PNG")
    objects["notes/_Attachments/image.png"] = output.getvalue()
    store.settings.snapshot_dir = tmp_path / "snapshots"
    store.settings.source_sync = {"enabled": True, "remote": "notes:bucket/notes"}
    SourceSync(store).once()
    assert store.read("source", "a.md")["content"].startswith("# 실제 S3")
    assert not (store.settings.policy().source / "_Template/excluded.md").exists()
    assert not (store.settings.policy().source / "_Attachments/image.png").exists()
    ImageSync(store).once()
    image_file = store.settings.policy().resolve("source", "_Attachments/image.png")
    assert image_file.read_bytes() == output.getvalue()
    del objects["notes/_Attachments/image.png"]
    ImageSync(store).once()
    assert not image_file.exists()
    assert not writes


@pytest.mark.skipif(not shutil.which("rclone"), reason="rclone is installed in the Linux test image")
def test_real_rclone_draft_upload_writes_only_new_draft(store, fake_s3):
    objects, writes = fake_s3
    objects["notes/a.md"] = b"# source"
    store.settings.source_sync = {"enabled": False, "remote": "notes:bucket/notes"}
    store.settings.drafts = {"enabled": True, "remote": "drafts:bucket/notes"}
    drafts = Drafts(store.settings)
    draft = DraftRequest(request_id="draft_s3_01", path="회의/요약", body="# 초안 본문", reason="검토")
    drafts.queue_draft(draft)
    DraftUpload(store).process()
    assert drafts.queue_draft(draft)["uploaded"]
    assert "# 초안 본문" in objects["notes/AI_Drafts/회의/요약.md"].decode()
    assert writes == [("PUT", "notes/AI_Drafts/회의/요약.md")]
    again = DraftRequest(request_id="draft_s3_02", path="회의/요약", body="# 덮어쓰기", reason="검토")
    drafts.queue_draft(again)
    DraftUpload(store).process()
    assert drafts.queue_draft(again)["code"] == "already_exists"
    assert len(writes) == 1 and objects["notes/a.md"] == b"# source"
