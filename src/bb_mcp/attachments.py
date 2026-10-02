"""Binary MCP attachment presentation; provider metadata is not format evidence."""
from __future__ import annotations

import base64
import json
import re
from urllib.parse import quote

from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, ImageContent, TextContent


def attachment_format(data: bytes) -> tuple[str, str]:
    for signature, mime, extension in (
        (b"\xff\xd8\xff", "image/jpeg", "jpg"),
        (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
        (b"GIF87a", "image/gif", "gif"),
        (b"GIF89a", "image/gif", "gif"),
        (b"%PDF-", "application/pdf", "pdf"),
        (b"PK\x03\x04", "application/zip", "zip"),
        (b"ID3", "audio/mpeg", "mp3"),
        (b"OggS", "application/ogg", "ogg"),
    ):
        if data.startswith(signature):
            return mime, extension
    if data.startswith(b"RIFF"):
        if data[8:12] == b"WEBP":
            return "image/webp", "webp"
        if data[8:12] == b"WAVE":
            return "audio/wav", "wav"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"hevx"):
            return "image/heic", "heic"
        if brand in (b"mif1", b"msf1"):
            return "image/heif", "heif"
        if brand in (b"avif", b"avis"):
            return "image/avif", "avif"
        if brand == b"qt  ":
            return "video/quicktime", "mov"
        if brand in (b"isom", b"iso2", b"mp41", b"mp42", b"M4V "):
            return "video/mp4", "mp4"
        if brand in (b"M4A ", b"M4B "):
            return "audio/mp4", "m4a"
    # An honest unknown is preferable to labelling arbitrary bytes as JPEG.
    return "application/octet-stream", "bin"


def attachment_result(guid: str, data: bytes, metadata: dict) -> CallToolResult:
    mime, extension = attachment_format(data)
    raw_name = metadata.get("transferName")
    name = raw_name if isinstance(raw_name, str) else "attachment"
    name = re.split(r"[/\\]", name)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    stem = re.sub(r"[^\w.-]", "_", stem).strip(".")[:120] or "attachment"
    if stem.lower().endswith("." + extension):
        stem = stem[:-(len(extension) + 1)]
    filename = f"{stem}.{extension}"
    encoded = base64.b64encode(data).decode("ascii")
    info = TextContent(type="text", text=json.dumps({
        "filename": filename, "mime_type": mime, "size_bytes": len(data),
    }))
    # Only broadly supported visual formats go inline. Originals such as HEIC,
    # audio, PDFs and unknown files remain downloadable binary resources.
    if mime in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        binary = ImageContent(type="image", mimeType=mime, data=encoded)
    else:
        binary = EmbeddedResource(type="resource", resource=BlobResourceContents(
            uri=f"attachment:///{quote(guid, safe='')}/{quote(filename, safe='')}",
            mimeType=mime, blob=encoded,
        ))
    # CallToolResult bypasses FastMCP's automatic string/structured duplication.
    return CallToolResult(content=[info, binary])
