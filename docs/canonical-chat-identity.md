# Canonical chat identity (the `iMessageLite` duality)

Status: **implemented** in `src/bb_mcp/chats.py`, wired in `src/bb_mcp/server.py`.

## What `iMessageLite` is

`iMessageLite` is a **real Apple Messages transport** — a first-class `.imservice`
plugin (`/System/Library/Messages/PlugIns/iMessageLite.imservice`), sibling to
`iMessage`, `RCS`, `SMS`, and `SatelliteSMS`, registered in Identity Services as
`com.apple.imservice.ids.iMessageLite`. It surfaced on the macOS/iOS **26.x** line
(alongside RCS-E2EE and the `any;-;` GUID-prefix change). Apple doesn't document what
it *does*; its existence is firmware-confirmed.

BlueBubbles reads `chat.guid` / `service_name` **verbatim** from the Messages
`chat.db`, with no normalization. And in `chat.db`, **each `(service, address)` pair
is its own chat row** — own ROWID, own GUID, own `chat_message_join` set. Apple
*displays* a person's iMessage/SMS/RCS/iMessageLite rows as one merged conversation,
but the rows are physically separate.

So one person can be addressable under several GUIDs that point at **different message
views**, e.g.:

- `iMessage;-;+1XXXXXXXXXX` — the live thread, full recent history (and where sends
  deliver).
- `iMessageLite;-;+1XXXXXXXXXX` — a stale shadow row holding a single months-old
  message from some past registration/handoff.

This is the same class as BlueBubbles issue #777 (macOS 26's `any;-;` prefix): **never
trust or reconstruct the service prefix; resolve the canonical chat by participant +
recency.**

## The bug it caused

The freshness guard keyed each agent's watermark on the **raw** `chat_guid`. With alias
rows that have divergent message views:

- **Key fragmentation** — a read under `iMessageLite;-;X` and a send under `iMessage;-;X`
  landed on different watermark keys, so the read didn't count for the send.
- **Stale-view false reject** — reading the stale shadow recorded an old watermark; a
  live-check of the canonical row saw newer messages and **falsely rejected a legitimate
  send** as "conversation moved."
- **Mirror under-protection** — symmetrically, a read under one alias could make a send
  under another look fresh when it wasn't.

## The fix

We treat a conversation as **service-agnostic: one identity per participant (1:1) or
group id.** (Decision: iMessage / SMS / RCS / `any` / `iMessageLite` to one number are
**one** conversation — matches Apple's merged-thread UX, and is the conservative
direction for freshness since a read under any service counts as having seen that
person's latest.)

Two pieces, in `bb_mcp/chats.py`:

1. **`canonical_chat_key(guid, normalize)`** — the service-agnostic key the watermark is
   stored under:
   - 1:1 → `1:1:<normalized address>`
   - group → `group:<id>` (service stripped)
   - unparseable → `raw:<guid>` (**fail-safe**: distinct, never merges → never
     manufactures a false "fresh")
2. **`ChatResolver`** — resolves an alias GUID to the **live canonical chat**: among the
   participant's rows, the most recent, breaking ties by service preference
   (`iMessage`/`any` > `iMessageLite` > `RCS` > `SMS`). Built from one `/chat/query`
   enumeration, cached ~60s (chat topology is global and rarely changes). Group and
   unknown GUIDs resolve to themselves.

Wired into `server.py`:

- **`get_chat_messages`** resolves the GUID first, so the agent reads the **live** thread,
  not the stale shadow — and records the watermark from what it actually saw.
- **`send_message` / `send_multipart` / `send_attachment`** resolve before the freshness
  check and the send, so they compare and deliver against the canonical chat.
- **Freshness** keys on `canonical_chat_key`, so a read under any alias clears a send under
  any alias (record and check normalize identically).
- **`create_chat`** checks existence via `ChatResolver.find_for_address` (participant
  match), so it refuses an existing chat under *any* service alias, not just a constructed
  `iMessage` GUID.
- **`list_chats` / `find_chats`** dedupe alias rows (`dedupe_chats`) so the agent is handed
  one stable GUID per conversation and never sees the shadow.

### Fail-closed guarantees (preserved)

- An unparseable/unknown GUID keys on its **raw self** → distinct → forces a re-read,
  never merges with another conversation.
- Resolver errors fall back to the **input GUID** (never invents a target).
- Aliasing can only ever cause a *re-read* (safe), never a false "fresh" send.

## Residual edges (documented, not bugs)

- **Resolution is best-effort & TTL-cached (~60s).** A brand-new alias appearing
  mid-window resolves to itself until the cache refreshes — worst case a re-read, never a
  bypass.
- **Group aliasing** across services (rare) isn't resolved; groups key on their opaque id.
- **SMS vs iMessage to one number share a watermark** by design. If someone genuinely has
  independent active SMS *and* iMessage threads with unseen messages in one, a read of the
  other counts — mild under-protection on a cooperative guardrail.
