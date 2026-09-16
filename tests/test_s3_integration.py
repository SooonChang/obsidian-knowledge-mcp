"""Exercise the real rclone binary against a local, read-only S3-compatible fixture."""

import hashlib
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
from xml.sax.saxutils import escape

import pytest

from knowledge_mcp.sync import SourceSync


@pytest.mark.skipif(not shutil.which("rclone"), reason="rclone is installed in the Linux test image")
def test_real_rclone_s3_download_never_writes(store, tmp_path, monkeypatch):
    objects = {"notes/a.md": "# 실제 S3 다운로드\n검증 노트".encode(), "notes/_Template/excluded.md": b"skip"}
    writes = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_PUT(self):
            writes.append("PUT")
            self.send_error(405)

        def do_POST(self):
            writes.append("POST")
            self.send_error(405)

        def do_DELETE(self):
            writes.append("DELETE")
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
    cfg.write_text(
        "[notes]\ntype = s3\nprovider = Other\nregion = us-east-1\n"
        f"endpoint = http://127.0.0.1:{server.server_port}\n"
        "access_key_id = test-only\nsecret_access_key = test-only\nforce_path_style = true\n"
    )
    monkeypatch.setenv("RCLONE_CONFIG", str(cfg))
    store.settings.snapshot_dir = tmp_path / "snapshots"
    store.settings.source_sync = {"enabled": True, "remote": "notes:bucket/notes"}
    try:
        SourceSync(store).once()
        assert store.read("source", "a.md")["content"].startswith("# 실제 S3")
        assert not (store.settings.policy().source / "_Template/excluded.md").exists()
        assert not writes
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
