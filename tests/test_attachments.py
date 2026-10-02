"""Synthetic binary fixtures only: no personal attachments are checked in."""
import base64
import json

import pytest
from mcp.types import CallToolResult

from bb_mcp.attachments import attachment_result
from bb_mcp import server


def test_heic_bytes_override_converted_jpeg_metadata():
    data = b"\0\0\0\x18ftypheic" + b"synthetic"
    result = attachment_result("test-id", data, {
        "transferName": "photo.heic.jpeg", "mimeType": "image/jpeg"})
    meta, binary = result.content
    assert json.loads(meta.text) == {
        "filename": "photo.heic", "mime_type": "image/heic", "size_bytes": len(data)}
    assert binary.type == "resource"
    assert binary.resource.mimeType == "image/heic"
    assert base64.b64decode(binary.resource.blob) == data
    assert result.structuredContent is None


@pytest.mark.parametrize("data,mime,extension", [
    (b"\xff\xd8\xffjpeg", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\npng", "image/png", "png"),
    (b"RIFF1234WEBPtest", "image/webp", "webp"),
    (b"GIF89atest", "image/gif", "gif"),
])
def test_inline_formats_use_actual_signature(data, mime, extension):
    result = attachment_result("test", data, {"transferName": "photo.heic"})
    assert result.content[1].type == "image"
    assert result.content[1].mimeType == mime
    assert json.loads(result.content[0].text)["filename"] == f"photo.{extension}"


@pytest.mark.parametrize("name", ["../../foo.jpg", "..\\..\\foo.jpg", "", None])
def test_unknown_bytes_are_honest_and_filename_is_safe(name):
    result = attachment_result("id/with?special#chars", b"unknown", {
        "transferName": name, "mimeType": "image/jpeg"})
    meta = json.loads(result.content[0].text)
    assert meta["mime_type"] == "application/octet-stream"
    assert meta["filename"].endswith(".bin")
    assert "/" not in meta["filename"] and "\\" not in meta["filename"]
    assert result.content[1].type == "resource"


@pytest.mark.parametrize("original", [False, True])
async def test_fastmcp_does_not_duplicate_large_binary_in_text(monkeypatch, original):
    data = b"\xff\xd8\xff" + b"x" * 2_659_489
    calls = []
    class FakeClient:
        async def download_attachment(self, guid, *, original=False):
            calls.append((guid, original))
            return data
        async def get_attachment(self, guid):
            return {"transferName": "photo.heic", "mimeType": "image/heic"}
    monkeypatch.setattr(server, "_bb", lambda ctx: FakeClient())
    result = await server.mcp.call_tool("download_attachment", {
        "attachment_guid": "synthetic", "original": original})
    assert isinstance(result, CallToolResult)
    assert result.structuredContent is None
    assert len(result.content) == 2
    assert len(result.content[0].text) < 250
    assert len(result.content[1].data) > 2_000_000
    assert base64.b64decode(result.content[1].data) == data
    assert calls == [("synthetic", original)]
