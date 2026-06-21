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

### Private API vs AppleScript

BlueBubbles can drive Messages two ways. **AppleScript** works on any install
with no extra setup but only sends plain texts/attachments. The **[Private
API](https://docs.bluebubbles.app/private-api/installation)** (requires the
BlueBubbles Helper + partially disabled SIP) additionally unlocks tapbacks,
edit/unsend, typing indicators, read receipts, group management, threaded
replies, and SMS sending.

On startup the server reads `/server/info` and adapts itself to what's actually
available:

- **Sends** use the Private API when present, and fall back to AppleScript when
  not — so `send_message` works on a bare install instead of erroring.
- **Tools that require the Private API** (reactions, edit/unsend, typing,
  read receipts, group management, iMessage/FaceTime availability checks) are
  **hidden from the tool list** when it's unavailable, so a client never sees a
  tool that could only fail.

Override the auto-detection with `BLUEBUBBLES_PRIVATE_API`:

```jsonc
"BLUEBUBBLES_PRIVATE_API": "auto"   // default: introspect /server/info
// "true"  — force-enable (assume the Private API is set up)
// "false" — force-disable (AppleScript only; hide Private API tools)
```

If `/server/info` can't be read at startup, detection fails open (assumes the
Private API is available) so a transient blip doesn't hide half the toolset.

The same `/server/info` read also discovers the user's own iMessage address,
surfaced by the `get_my_address` tool. Override it if detection is wrong or you
run multiple handles:

```jsonc
"BLUEBUBBLES_MY_ADDRESS": "+15551234567"  // or you@icloud.com
```

### Contact names

Message and chat data from BlueBubbles carries raw phone numbers and emails, not
names. To save the model from constantly cross-referencing numbers, read
responses are **enriched with a `contactName`** beside each handle, and
`find_contact` / `find_chats` let it reach people by name instead of number.

Resolution is lazy and cached per session: a response's addresses are looked up
in a single batched `/contact/query` the first time they're seen, then reused.
Disable it (for leaner responses or if your contact DB is slow) with:

```jsonc
"BLUEBUBBLES_RESOLVE_NAMES": "false"  // default: enabled
```

`find_contact` / `find_chats` still work when this is off — they're explicit.

### Freshness guard

An agent can reply to a conversation against a stale snapshot — it read the thread
a while ago, a new message arrived in the gap, and it answers without seeing it.
The freshness guard enforces, server-side and per agent, that a send into a chat
only goes through if that agent **read the chat within the last hour and nothing
new has arrived since**. (A consequence: you must read an existing chat before
sending into it — good hygiene for replies. Starting a brand-new conversation via
`create_chat` is unaffected; it's for first contact only and, with the guard on,
refuses to reach an existing chat — use `send_message` for those.)

**On by default.** Disable with `BLUEBUBBLES_FRESHNESS=off`. Tuning:

```jsonc
"BLUEBUBBLES_FRESHNESS": "off",                  // default: on
"BLUEBUBBLES_FRESHNESS_IDENTITY": "session",     // how agents are told apart (default)
"BLUEBUBBLES_WATERMARK_TTL_SECONDS": "3600",     // how long a read stays "fresh" (default 1h)
"BLUEBUBBLES_WATERMARK_MAX_AGENTS": "10000"      // memory backstop (default 10000)
```

The freshness check is *per agent*, so it needs to tell agents apart.
`BLUEBUBBLES_FRESHNESS_IDENTITY` selects how:

- **`session`** (default): use the MCP transport session — automatic and
  zero-config for **stdio** (one agent) and **direct HTTP** (one session per
  connecting client). Over HTTP this relies on stateful sessions (FastMCP's
  default); the server **refuses to start** if you combine session-identity
  freshness with stateless HTTP, since every send would be blocked.
- **`meta`**: identify agents by a per-agent `agentId` the caller stamps into
  request `_meta`. Required only behind a **pooling proxy/airlock**, where every
  agent shares one transport session so `session` can't distinguish them. See
  [`docs/freshness-guard.md`](docs/freshness-guard.md) for the airlock contract.

### Compact responses & sender filtering

Message reads return a **compact projection by default** — the fields an
assistant needs (`guid`, `text`, `handle` + `contactName`, `isFromMe`,
timestamps, attachment names, reaction/reply linkage) instead of the full raw
BlueBubbles objects, which cuts token usage substantially. Pass `extended=true`
on any read tool to get the complete raw fields.

Message reads (`get_chat_messages`, `search_messages`, `get_recent_messages`)
also accept **`from_address`** to filter by who sent each message — pass a phone
number / email, or `"me"` for the user's own messages. Filtering is by true
sender (the user when `isFromMe`, otherwise the message's handle), so it's
correct in both 1:1 and group chats.

## Tools

| Tool | Description | Annotations |
|------|-------------|-------------|
| `ping` | Check server connectivity | read-only |
| `get_server_info` | Server info and health | read-only |
| `get_my_address` | The user's own iMessage address (to identify their own messages) | read-only |
| `list_chats` | List conversations by recent activity | read-only |
| `get_chat` | Chat details with participants | read-only |
| `get_chat_messages` | Messages from a chat | read-only |
| `search_messages` | Search by text, chat, time range | read-only |
| `get_message` | Single message by GUID | read-only |
| `get_contacts` | All contacts | read-only |
| `lookup_contact` | Look up name by phone/email | read-only |
| `find_contact` | Find contacts by name (phone/email unknown) | read-only |
| `find_chats` | Find chats involving a contact by name | read-only |
| `check_imessage` | Check iMessage registration | read-only |
| `check_facetime` | Check FaceTime registration | read-only |
| `query_handles` | List/search known handles | read-only |
| `get_handle` | Get a handle by address | read-only |
| `get_focus_status` | Contact's Focus / Do Not Disturb status | read-only |
| `find_my_devices` | Find My — your devices' locations | read-only |
| `find_my_friends` | Find My — friends' locations | read-only |
| `list_scheduled_messages` | List future messages | read-only |
| `get_scheduled_message` | Get one scheduled message by ID | read-only |
| `get_recent_messages` | Messages from last N minutes across all chats | read-only |
| `get_unread_chats` | Chats with unread messages + their latest messages | read-only |
| `get_attachment_info` | Attachment metadata | read-only |
| `download_attachment` | Download attachment as base64 | read-only |
| `get_group_icon` | Download a group chat's icon | read-only |
| `mark_chat_read` | Send read receipt | idempotent, open-world |
| `mark_chat_unread` | Mark chat unread (local) | idempotent |
| `rename_group` | Rename a group chat | idempotent |
| `set_group_icon` | Set a group chat's icon | idempotent |
| `start_typing` | Show typing indicator | open-world |
| `stop_typing` | Stop typing indicator | open-world |
| `send_message` | Send to an existing chat (1:1 or group) | open-world |
| `create_chat` | Start a new 1:1 conversation by phone/email | open-world |
| `create_group_chat` | Create a group chat + first message | open-world |
| `send_attachment` | Send a file attachment | open-world |
| `send_multipart` | Send text + attachments as one message | open-world |
| `send_reaction` | Tapback reaction | open-world |
| `edit_message` | Edit a sent message | open-world |
| `schedule_message` | Schedule a future message | open-world |
| `update_scheduled_message` | Update a scheduled message | open-world |
| `add_participant` | Add to group chat | open-world |
| `unsend_message` | Retract a message | destructive, open-world |
| `remove_participant` | Remove from group chat | destructive, open-world |
| `leave_chat` | Leave a group chat | destructive, open-world |
| `remove_group_icon` | Remove a group chat's icon | destructive, open-world |
| `delete_message` | Delete a single message | destructive, open-world |
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
