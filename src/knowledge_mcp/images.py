from __future__ import annotations

import base64
import io
import json
import uuid
import warnings

from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image, ImageOps

from .attachments import linked_attachment
from .storage import Store, digest, now

MAX_IMAGE_BYTES = 25_000_000
MAX_IMAGE_PIXELS = 40_000_000
MAX_DISPLAY_SIDE = 2048
FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP", ".gif": "GIF"}


class ImageValidationError(ValueError):
    """An image validation failure with a public, path-free diagnostic."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def decode_image(data: bytes, suffix: str):
    """Validate and decode only the displayed frame, without modifying the original."""
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageValidationError("image_too_large", "Image exceeds 25 MB")
    if suffix.lower() not in FORMATS:
        raise ImageValidationError("unsupported_image", "Only PNG, JPEG, WebP and GIF images are supported")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            # Obsidian attachments can retain an old extension after a format
            # conversion. Trust the decoded format, restricted to our allowlist.
            with Image.open(io.BytesIO(data), formats=list(set(FORMATS.values()))) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ImageValidationError("image_pixel_limit", "Image exceeds 40 million pixels")
                animated = getattr(image, "n_frames", 1) > 1
                image.seek(0)
                image.load()
                return ImageOps.exif_transpose(image), animated
    except ImageValidationError:
        raise
    except (OSError, SyntaxError, ValueError, EOFError) as exc:
        raise ImageValidationError("invalid_image", "Invalid or damaged image") from exc
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ImageValidationError("image_pixel_limit", "Image exceeds 40 million pixels") from exc


def read_image(
    store: Store,
    path: str,
    linked_from: str,
    linked_vault: str = "wiki",
    expected_hash: str | None = None,
) -> CallToolResult:
    # Publication, deletion and receipt validation share the wiki writer lock.
    with store.lock:
        policy = store.settings.policy()
        if not policy.is_image(path):
            raise ValueError("Image must be a supported file under attachments_dir")
        file = linked_attachment(store, policy, path, linked_from, linked_vault)
        metadata = policy.image_manifest().get("files", {}).get(path) if policy.snapshot_dir else None
        if policy.snapshot_dir and (not metadata or not file.exists()):
            return _result({"path": path, "pending": True, "reason": "Image awaits periodic synchronization"})
        if file.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds 25 MB")
        data = file.read_bytes()
        file_hash = digest(data)
        if metadata and metadata["hash"] != file_hash:
            raise ValueError("Image publication incomplete; retry after synchronization")
        if expected_hash is not None and expected_hash != file_hash:
            raise ValueError("Image changed since read; read it again")
        image, animated = decode_image(data, file.suffix)
        try:
            original_size = list(image.size)
            image.thumbnail((MAX_DISPLAY_SIDE, MAX_DISPLAY_SIDE), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.convert("RGBA").save(output, format="PNG")
            display_size = list(image.size)
        finally:
            image.close()
        ident, checked_at = uuid.uuid4().hex, now()
        with store.db() as db:
            db.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?,?,?)",
                (ident, "source", path, file_hash, checked_at, 1, 1),
            )
        info = {
            "path": path, "vault": "source", "hash": file_hash, "receipt": ident,
            "checked_at": checked_at, "original_size": original_size, "display_size": display_size,
            "range_unit": "frames", "start": 1, "end": 1, "first_frame_only": animated,
            "scope": "Displayed preview only; resized to at most 2048 px; no OCR verification",
        }
        return _result(info, output.getvalue())


def _result(info: dict, data: bytes | None = None) -> CallToolResult:
    content = [TextContent(type="text", text=json.dumps(info, ensure_ascii=False))]
    if data is not None:
        content.append(ImageContent(type="image", mimeType="image/png", data=base64.b64encode(data).decode()))
    return CallToolResult(content=content, structuredContent=info)
