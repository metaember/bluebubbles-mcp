# Attachment content contract

`download_attachment(attachment_guid, original=false)` returns a `CallToolResult`
with two content blocks: small JSON text (`filename`, `mime_type`, `size_bytes`)
and one binary block. There is no base64 JSON field or duplicate structured result.
Consumers that previously parsed `data_base64` must use MCP content blocks.

JPEG, PNG, WebP and GIF signatures produce `ImageContent`. Other files, including
HEIC originals, produce an embedded `BlobResourceContents` with a filename-bearing
URI. Unknown signatures are `application/octet-stream` / `.bin`, not a guess from
potentially stale provider metadata. Filenames are sanitized and extensions reflect
the detected representation. This is signature detection, not a full media decoder.

By default the download asks BlueBubbles for its converted representation. Set
`original=true` to retain original bytes. Both paths identify the returned bytes;
neither assumes the provider actually performed a conversion. Downloads are streamed
and rejected above 50 MiB, even without Content-Length. HTTP errors never include
the credential-bearing URL.

This avoids text-result truncation in MCP consumers. Consumers/gateways still need
binary block support and may impose their own media limits. Synthetic FastMCP tests
exercise a 2.66 MB payload (over 3.5 million base64 characters) and assert it appears
exactly once, outside text and structured output. No automatic offsite processing,
external upload service, or write permission is introduced by this read tool.
