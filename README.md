# bluebubbles-mcp

MCP server for [BlueBubbles](https://bluebubbles.app) — access iMessage from any MCP client.

Built from scratch with no third-party MCP dependencies beyond the official [`mcp`](https://pypi.org/project/mcp/) SDK and [`httpx`](https://pypi.org/project/httpx/).

## Prerequisites

- Python 3.11+
- A running [BlueBubbles server](https://bluebubbles.app) with API access enabled

## Setup

```bash
git clone https://github.com/metaember/bluebubbles-mcp.git
cd bluebubbles-mcp
uv sync
```

## Configuration

Add to your MCP client config (e.g. Claude Code `~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "bluebubbles": {
      "command": "uv",
      "args": ["--directory", "/path/to/bluebubbles-mcp", "run", "python", "-m", "bb_mcp.server"],
      "env": {
        "BLUEBUBBLES_URL": "https://your-bluebubbles-server",
        "BLUEBUBBLES_PASSWORD": "your-server-password"
      }
    }
  }
}
```

### Restricting write recipients (allowlist)

By default the server can message anyone. Set `BLUEBUBBLES_WRITE_ALLOWLIST` to a
comma-separated list of phone numbers and/or emails to restrict **every write**
(send, reaction, attachment, typing, scheduling, group edits, delete/leave) to
those recipients. It is enforced server-side, so an MCP client — including one
reaching the HTTP transport — cannot bypass it.

```jsonc
"BLUEBUBBLES_WRITE_ALLOWLIST": "+15551234567, partner@example.com",
"BLUEBUBBLES_ALLOWLIST_REGION": "US"  // optional, default region for parsing local numbers
```

Behavior:

- **Unset** → unrestricted (backward compatible). **Set but empty** (`""`) → deny
  all, a safe failure mode.
- Numbers are matched in normalized E.164 form, so `(555) 123-4567` and
  `+15551234567` are equivalent; emails match case-insensitively.
- For a **group chat**, *every* participant must be on the list or the write is
  blocked. A chat whose participants can't be resolved is denied (fail-closed).
- Read tools are **not** restricted, and the message-GUID writes
  (`edit_message`, `unsend_message`) aren't covered — they only act on an
  already-sent message and can't reach a new recipient. A
  `BLUEBUBBLES_READ_ALLOWLIST` may be added later.

## Tools

| Tool | Description | Annotations |
|------|-------------|-------------|
| `ping` | Check server connectivity | read-only |
| `get_server_info` | Server info and health | read-only |
| `list_chats` | List conversations by recent activity | read-only |
| `get_chat` | Chat details with participants | read-only |
| `get_chat_messages` | Messages from a chat | read-only |
| `search_messages` | Search by text, chat, time range | read-only |
| `get_message` | Single message by GUID | read-only |
| `get_contacts` | All contacts | read-only |
| `lookup_contact` | Look up by phone/email | read-only |
| `check_imessage` | Check iMessage registration | read-only |
| `check_facetime` | Check FaceTime registration | read-only |
| `list_scheduled_messages` | List future messages | read-only |
| `get_recent_messages` | Messages from last N minutes across all chats | read-only |
| `get_unread_chats` | Chats with unread messages + their latest messages | read-only |
| `get_attachment_info` | Attachment metadata | read-only |
| `download_attachment` | Download attachment as base64 | read-only |
| `mark_chat_read` | Send read receipt | idempotent, open-world |
| `mark_chat_unread` | Mark chat unread (local) | idempotent |
| `rename_group` | Rename a group chat | idempotent |
| `start_typing` | Show typing indicator | open-world |
| `stop_typing` | Stop typing indicator | open-world |
| `send_message` | Send to existing chat | open-world |
| `send_message_to_address` | Send to phone/email | open-world |
| `send_attachment` | Send a file attachment | open-world |
| `send_reaction` | Tapback reaction | open-world |
| `edit_message` | Edit a sent message | open-world |
| `schedule_message` | Schedule a future message | open-world |
| `add_participant` | Add to group chat | open-world |
| `unsend_message` | Retract a message | destructive, open-world |
| `remove_participant` | Remove from group chat | destructive, open-world |
| `leave_chat` | Leave a group chat | destructive, open-world |
| `delete_chat` | Delete a conversation | destructive, open-world |
| `delete_scheduled_message` | Cancel scheduled message | destructive, open-world |

## Run over HTTP (Docker)

The server speaks stdio by default. Set `MCP_TRANSPORT=streamable-http` to serve
over [Streamable HTTP](https://modelcontextprotocol.io) instead, so any
HTTP-capable MCP client can connect at `http://<host>:8000/mcp`. The Docker image
sets this for you.

```sh
docker build -t bluebubbles-mcp .
docker run --rm -e BLUEBUBBLES_URL -e BLUEBUBBLES_PASSWORD -p 8000:8000 bluebubbles-mcp
```

As a Compose service — credentials live in this container's own environment (an
`env_file` or Docker secrets), so they stay isolated to this tool:

```yaml
services:
  bluebubbles-mcp:
    build: .
    environment:
      BLUEBUBBLES_URL: ${BLUEBUBBLES_URL}
      BLUEBUBBLES_PASSWORD: ${BLUEBUBBLES_PASSWORD}
    restart: unless-stopped
```

Notes:
- The HTTP endpoint is unauthenticated, so don't expose it publicly — keep it on a
  private/internal network (and drop the published `ports:` if a co-located client
  reaches it over the Compose network).
- `BLUEBUBBLES_URL` must be reachable **from inside the container** — use a
  hostname/IP the container can resolve (e.g. the BlueBubbles host's LAN address or
  `host.docker.internal`), not `localhost`.

## License

MIT
