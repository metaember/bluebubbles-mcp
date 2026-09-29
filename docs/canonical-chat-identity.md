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

We group chat listings by participant (1:1) or group id, then resolve known aliases
to the most recently active row before a read or send. A freshness watermark belongs
to the **resolved row's GUID**, so an unresolved alias cannot borrow another row's
watermark.

Two pieces, in `bb_mcp/chats.py`:

1. **`canonical_chat_key(guid, normalize)`** — the service-agnostic key used to
   deduplicate chat listings:
   - 1:1 → `1:1:<normalized address>`
   - group → `group:<id>` (service stripped)
   - unparseable → `raw:<guid>` (distinct, never merges)
2. **`ChatResolver`** — resolves an alias GUID to the **live canonical chat**: among the
   participant's rows, the most recent, breaking ties by service preference
   (`iMessage`/`any` > `iMessageLite` > `RCS` > `SMS`). Built from one `/chat/query`
   enumeration, cached ~60s for reads. A guarded send refreshes that enumeration so
   newly active aliases are considered. A GUID outside the first page can still use
   the known mapping for its embedded address; otherwise it resolves to itself.

Wired into `server.py`:

- **`get_chat_messages`** resolves the GUID first, so the agent reads the **live** thread,
  not the stale shadow — and records the watermark from what it actually saw.
- **`send_message` / `send_multipart` / `send_attachment`** resolve before the freshness
  check and the send, so they compare and deliver against the canonical chat.
- **Freshness** keys on the resolved GUID. Known aliases converge on one row and share
  its watermark; an unresolved alias keeps a separate watermark and requires its own
  read. A guarded send stops if the resolver cannot refresh.
- **`create_chat`** checks existence via `ChatResolver.find_for_address` (participant
  match), refreshing its map and paging past the first 1000 rows on a miss. It refuses
  an existing chat under any service alias and stops if the lookup fails.
- **`list_chats` / `find_chats`** dedupe alias rows (`dedupe_chats`). `list_chats`
  applies offset and limit after deduplication, so pages do not repeat aliases.

### Archival reads

Canonical reads are the right default for an assistant, but they cannot recover history
that exists only on an older physical SMS/RCS/iMessage row. Two separately named,
read-only tools preserve that capability for trusted indexers:

- **`list_chat_aliases`** paginates physical BlueBubbles chat rows without
  deduplication.
- **`get_chat_alias_messages`** reads the exact supplied physical GUID without alias
  resolution and does not advance the freshness watermark used to authorize sends.

These tools should be withheld from ordinary agent profiles and granted only to archival
consumers. All send tools remain forcibly canonicalized.

### Fail-closed guarantees (preserved)

- An unresolved GUID keys on its **raw self** for freshness. A read of another alias
  cannot authorize a send through it.
- Resolver errors fall back to the input GUID for reads; guarded sends stop instead.
- A guarded send refreshes alias selection before comparing the watermark. If another
  row became the most recent, its different GUID requires a re-read.

## Residual edges (documented, not bugs)

- **The alias map covers the 1000 most recently active chats.** Older GUIDs resolve
  through a known address when possible, or keep their raw identity. The `create_chat`
  existence check continues through older pages before allowing a first-contact send.
- **The live freshness check reads the selected row.** If two independently active
  services have messages with equal timestamps, selection may still prefer one row.
  The guard also cannot close the interval between its final check and the send without
  an atomic BlueBubbles API operation.
- **Group aliasing** across services (rare) is not resolved; groups retain their GUID.
  Archival consumers can still inspect each physical group row through the alias tools.
