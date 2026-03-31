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

## License

MIT
