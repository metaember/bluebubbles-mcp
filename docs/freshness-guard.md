# Freshness Guard — per-agent "read before you reply" enforcement

Status: **BB side implemented & on by default** in `src/bb_mcp/freshness.py` +
`src/bb_mcp/server.py`. The airlock side below is needed **only** for the pooling-proxy
deployment (identity mode `meta`); standalone deployments need nothing.

## Problem

When an agent replies to an iMessage conversation, it may compose against a **stale
snapshot**: it read the thread some tool-calls (or minutes) ago, a new inbound message
arrived in the gap, and it sends a reply that ignores it — answering a retracted
question, doubling up, missing a follow-up. This is a time-of-check-to-time-of-use race.

We want a server-side invariant: **an agent may only send into a chat if it has recently
read that chat and nothing new has arrived since.** Enforced server-side so a client
cannot bypass it (same posture as the write allowlist in `policy.py`).

## This is a standalone feature; the airlock is just one identity source

The check is inherently per-reader: "did *this* agent see the latest before it sends?" A
single shared watermark would let agent A's read clear the gate for agent B — a bypass.
So the guard needs to **tell agents apart**, and that is the *only* part that varies by
deployment. `BLUEBUBBLES_FRESHNESS_IDENTITY` selects the source:

- **`session`** (default): the MCP transport session. For **stdio** there is one session
  (one agent). For **direct HTTP**, the server assigns one session per connecting client,
  so `ctx.session` distinguishes agents automatically. Zero config, no caller cooperation.
  (Over HTTP this relies on stateful sessions — FastMCP's default.)
- **`meta`**: a per-agent `agentId` the caller stamps into request `_meta`. Needed **only**
  behind a **pooling proxy/airlock**: it terminates MCP and fans many agents over its own
  connection(s) to BB, so every agent shares *one* transport session and `session` can't
  tell them apart. The airlock is then the only layer that authoritatively knows "which
  agent," so it must propagate that identity downstream.

Decision for the airlock case: **identity up, semantics down.** The airlock supplies the
id; BB keeps the gate logic (it owns the domain: chats, inbound-vs-outbound, latest
message) and keys its per-agent watermark on whatever `_agent_id` resolves to. Putting the
whole gate in the airlock was rejected — it would force the airlock to learn BB's domain.

Everything below the next heading applies to **`meta` mode only**. In `session` mode there
is nothing to implement outside BB.

## The contract (airlock ⇄ BlueBubbles MCP) — `meta` mode only

Every JSON-RPC request the airlock forwards to BB carries a per-agent id in MCP request
metadata:

```jsonc
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "tools/call",
  "params": {
    "name": "send_message",
    "arguments": { "chat_guid": "iMessage;-;+15551234567", "message": "ok!" },
    "_meta": { "agentId": "a1b2c3d4-..." }   // <-- stamped by the airlock
  }
}
```

- **Channel:** `params._meta.agentId`. Verified to survive end-to-end: MCP's
  `RequestParams.Meta` is declared `extra="allow"`, and the server forwards the request's
  `_meta` into `RequestContext.meta`. Transport-agnostic (works for stdio and HTTP);
  no HTTP-header coupling. (An `X-Agent-Id` header is a fallback only if you ever proxy
  raw HTTP without terminating MCP — not the plan here.)
- **`agentId` requirements:**
  - **Non-empty string.**
  - **Stable** for the lifetime of one agent session — the agent's read and its later
    send must carry the *same* id, or the watermark won't match.
  - **Unique** per concurrent agent — no shared/default value (e.g. never `"agent"` for
    everyone), or two agents collide into one watermark = bypass. A UUID per agent
    session, or the airlock's own per-agent session id, is ideal.
  - **Opaque** to BB — BB treats it as an arbitrary dictionary key.

### Asserts on both sides

- **Airlock (outbound):** never forward a gated send (`send_message`, `send_multipart`,
  `send_attachment`) without a non-empty `agentId`. If it doesn't have one, fail the call
  at the airlock rather than forward an unidentified send.
- **BB (inbound):** on the enforced tools, if `_meta.agentId` is missing/blank, reject
  the call. BB never trusts that the airlock did its job — it re-checks. This is
  fail-closed, mirroring the allowlist denying a chat whose participants can't be
  resolved.

## Airlock side — implementation spec

The airlock terminates MCP (it is an MCP server to agents and an MCP client to BB,
multiplexing many agents over its own connection(s)). It must:

1. **Mint an id per agent session.** When an agent connects/authenticates, generate a
   stable unique id (UUID v4, or reuse the agent's own session identifier). Hold it for
   the agent's lifetime.
2. **Stamp it on every forwarded request.** For each `tools/call` (and ideally every
   request) the airlock proxies to BB, set `params._meta.agentId = <that agent's id>`.
   Merge into any existing `_meta` rather than overwriting it (preserve `progressToken`
   etc.).
3. **Outbound assert.** Before forwarding any of the gated send tools
   (`send_message`, `send_multipart`, `send_attachment`), assert a non-empty id exists;
   if not, return an error to the agent instead of forwarding.
4. **No id reuse across concurrent agents.** Two live agents must never share an id.
   (Sequential reuse after an agent fully disconnects is harmless but unnecessary —
   prefer fresh ids.)

Stamping the id on *all* forwarded calls (not just the gated ones) is simplest and lets
BB enforce wherever it needs to; the airlock does not need to know which BB tools are
gated.

### Handling BB's rejections

BB returns a tool error when the gate trips (stale/absent read, or new inbound since
read). The airlock should pass that error through to the agent verbatim — the message
text tells the agent what to do ("call get_chat_messages first"). No special handling
required; the agent re-reads and retries.

## BlueBubbles side — implementation spec

### New module: `src/bb_mcp/freshness.py`

A pure, sync, unit-testable tracker (mirrors how `policy.py` is separate from the server):

```python
class FreshnessError(Exception):
    """Raised when a send is blocked for staleness. Message is agent-facing
    and tells the agent how to recover (re-read the chat)."""

class FreshnessTracker:
    def __init__(self, ttl_seconds: float, max_agents: int,
                 clock: Callable[[], float] = time.monotonic): ...

    def record(self, agent_id: str, chat_guid: str,
               last_message_ts: int | None) -> None:
        """Record what `agent_id` last knows of this chat: its newest message
        (any sender), dated `last_message_ts` (epoch-ms; None if no messages).
        Called on read AND on the agent's own send (so its send doesn't make its
        next send look stale)."""

    def last_seen(self, agent_id: str, chat_guid: str) -> int | None:
        """Return the newest-message timestamp this agent last saw for the chat.
        Raises FreshnessError if:
          - no read recorded for (agent_id, chat_guid), or
          - the recorded read is older than ttl_seconds (stale).
        Returns None when a fresh read exists but the chat had no messages."""
```

- **Storage:** `OrderedDict[agent_id, dict[chat_guid, _Entry]]` where
  `_Entry = (last_message_ts: int | None, recorded_at: float /* monotonic */)`.
- **Eviction (this is a feature, not just memory hygiene):**
  - **TTL = primary.** A read older than `ttl_seconds` (default **3600** = 1h) no longer
    counts → `last_seen` raises → the agent is forced to re-read. This is the desired
    forcing function: idle agents must re-confirm before replying.
  - **Count cap = backstop only.** `max_agents` (default **10000**, well above expected
    concurrency) bounds memory. Move-to-end on access; `popitem(last=False)` when over.
    Keep it *generous* so active agents never evict each other's hot watermarks under
    contention (that would cause re-read thrash). Eviction should fire on *age*, not
    *crowding*.
  - Eviction is always safe: a dropped watermark fails closed → a harmless forced
    re-read, never a bypass. So aggressive forgetting is fine and wanted.
- **Counts all senders, not just inbound.** "Nothing new since the read" includes
  outbound messages the agent didn't author — another agent, or the user from another
  device, barging in makes the queued reply stale too (this is the shared-inbox "agent
  collision" case; see Prior art). The agent's *own* send is handled by recording a
  watermark on send (below), so it doesn't self-block its next send.
- **Why timestamp, not GUID equality:** comparing newest-message `dateCreated` is a clean
  monotonic version token — `live > expected` means a newer message exists, `expected
  None` + any live message means first-contact-then-activity. GUID is kept only for
  debugging.

### Wiring in `server.py`

- **Config (lifespan):**
  - `BLUEBUBBLES_FRESHNESS` — `parse_override`; **on by default**, disabled only for an
    explicit off value (`off`/`false`/`0`/`no`). When off, all hooks below are no-ops.
  - `BLUEBUBBLES_FRESHNESS_IDENTITY` — `session` (default) or `meta`; anything else falls
    back to `session`.
  - `BLUEBUBBLES_WATERMARK_TTL_SECONDS` — default `3600`.
  - `BLUEBUBBLES_WATERMARK_MAX_AGENTS` — default `10000`.
  - `_build_freshness(env) -> (FreshnessTracker | None, identity)`; yield both as
    `"freshness"` and `"freshness_identity"`.
  - `_freshness(ctx) -> FreshnessTracker | None`; `_freshness_identity(ctx) -> str`.
- **`_agent_id(ctx) -> str`** — resolves a stable per-agent key per the identity mode.
  In `meta` mode: `getattr(ctx.request_context.meta, "agentId", None)`, raising
  `FreshnessError` (clear message) if missing/blank, returns `f"meta:{id}"`. In `session`
  mode: `f"session:{id(ctx.request_context.session)}"` — stable per connection, no caller
  cooperation. Only called when the guard is enabled.
- **Startup assert** — `_assert_freshness_transport_compatible(transport, env)`, called
  from `main()`, **refuses to start** when `transport == "streamable-http"` and
  `mcp.settings.stateless_http` and the guard is on in `session` mode. Stateless HTTP
  gives each request a fresh session, so every send would see "no prior read" and be
  blocked — fail fast with a clear fix (disable stateless HTTP / use `meta` identity /
  turn the guard off) rather than silently rejecting all sends. `meta` mode and stdio are
  exempt (their sessions are irrelevant / always persistent). Note: this covers the
  `main()` entry path; a custom ASGI mount of `streamable_http_app()` bypasses it.
- **Record on read** — helper `_record_watermark(ctx, chat_guid, raw_messages)` computes
  the newest message `dateCreated` over *all* senders (`newest_message_ts`: `max(dateCreated
  for m in raw_messages if "isFromMe" in m)`, else None) and calls `tracker.record(...)`.
  Called (when enabled) from:
  - `get_chat_messages` — with its `data` and `chat_guid`.
  - `get_unread_chats` — per chat, with each chat's fetched `messages` and `chat["guid"]`.
  - **Not** `get_recent_messages` / `search_messages` — cross-chat firehoses, not a thread
    read; recording from them would let an agent "see" a chat without reading it.
- **Record on send** — helper `_record_sent(ctx, chat_guid, sent)` advances the agent's
  watermark to its own just-sent message (from the send response's `dateCreated`; falls
  back to one re-fetch only if the response lacks it). Called after the send in
  `send_message` / `send_multipart` / `send_attachment`. Without this, an agent's own
  message would block its next send to the same chat.
- **Gate on send** — helper `_check_freshness(ctx, chat_guid)`:
  ```
  tracker = _freshness(ctx);  if tracker is None: return
  agent_id = _agent_id(ctx)                       # session-derived or _meta.agentId
  expected = tracker.last_seen(agent_id, chat_guid)  # raises if no/stale read
  live = await _latest_message_ts(ctx, chat_guid)    # one extra API round-trip
  if live is not None and (expected is None or live > expected):
      raise FreshnessError("The conversation moved since you last read it ... re-plan "
                           "in light of them: your reply may need revising, or may no "
                           "longer be warranted.")
  ```
  Called at the top of: **`send_message`, `send_multipart`, `send_attachment`** (after the
  existing `_guard(ctx).check_chat(...)`).
  - `_latest_message_ts(ctx, chat_guid)` fetches recent messages
    (`get_chat_messages(chat_guid, limit=10, sort="DESC")`) and returns the max
    `dateCreated` over all senders, or None.

### Tool scope (v1)

"Identify the agent" below means resolve `_agent_id` — automatic in `session` mode,
or require `_meta.agentId` (fail closed) in `meta` mode.

| Tool | Behavior when guard enabled |
|------|------------------------------|
| `get_chat_messages` | record watermark; identify the agent |
| `get_unread_chats` | record watermark (per chat); identify the agent |
| `send_message` | gate; identify the agent |
| `send_multipart` | gate; identify the agent |
| `send_attachment` | gate; identify the agent |
| `send_message_to_address`, `create_group_chat` | **exempt** — address-based, may be first contact / new chat (no thread to be stale about) |
| `send_reaction`, `edit_message` | **exempt v1** — lower-stakes; candidates for phase 2 |
| all other tools | unaffected |

### Tests (`tests/test_freshness.py`)

Pure-tracker tests with an injected clock:
- record then `last_seen` returns the stored ts within TTL.
- `last_seen` raises after TTL elapses (clock advanced past `ttl_seconds`).
- `last_seen` raises when no read recorded.
- fresh read, nothing newer → server compare allows (live <= expected, or both None).
- fresh read, newer message (`live > expected`) → blocked.
- `newest_message_ts` counts outbound too; tolerates missing `dateCreated`.
- count cap evicts oldest agent past `max_agents`; evicted agent's next send fails closed.
- two agents are isolated: A's read does not satisfy B's send.

Outbound / own-send behavior (through the real `send_message` tool):
- outbound barge-in (a message the agent didn't author) → next send blocked.
- the agent's own send advances its watermark → a follow-up send isn't self-blocked.

`_build_freshness` / identity tests:
- on by default (`session`); off only for explicit off values.
- `meta` identity selected; unknown identity falls back to `session`.
- TTL / max-agents env overrides honored.

Server-wiring tests (through the real `send_message` tool):
- guard disabled → sends work, no identity needed.
- `session` mode: read-then-send allowed; send without a read blocked; two sessions isolated.
- `meta` mode: missing `agentId` on a gated send/read rejected; read-then-send allowed.

## Why the 1h TTL is not safety-critical

The send path re-fetches the chat's live newest-message timestamp every time and compares,
so a genuinely new message (from anyone) is caught at **any** watermark age — the TTL never
governs that case. The TTL only forces a re-read in the "nothing new but time passed" case:
a context-decay backstop for a long-dormant thread (the human's intent may have moved on
even with zero new messages). So a tighter TTL buys redundant re-reads, not real safety; 1h
(or even a few hours) is fine. Tune via env without a redeploy.

We considered the inverse — "if the read is stale *enough*, just proceed" — and rejected it:
no surveyed system (OCC, ETag/If-Match, DB version columns, leases, CRDTs) ever re-grants
write permission based on read *age*; the norm is "honor a read of any age as long as the
version matches," and where time enters it always gates the *other* way (more staleness →
less trust). See Prior art.

## Prior art

This is optimistic concurrency control specialized to a conversation, and the design mirrors
established messaging practice:

- **Shared-inbox collision detection** (Help Scout): a new message — from the customer *or
  another agent* — pauses the send and forces the agent to review before committing. Our
  all-senders watermark + block-and-re-plan is the same pattern; counting outbound is exactly
  their "agent collision" case.
- **Chatbot "answer only the latest" / debounce-and-consolidate** (Intercom Fin; ~5s
  debounce conventions): respond to the merged latest state, not a stale in-flight turn. Our
  block → re-read → re-plan composes with an agent-side debounce (out of scope for a server
  gate, which can only block).
- **Voice barge-in & LangGraph `interrupt`**: new input cancels the in-flight response and
  re-plans on the latest. Our gate is the server-side trip-wire for that re-plan.
- **OCC / conditional requests** (RFC 7232 `If-Match`/412, DB `@Version`, DynamoDB
  conditional writes): version-match, age-independent — the basis for "honor any-age read if
  the version still matches," and why the TTL is a backstop, not the gate.

## Out of scope / future

- **Hard double-texting prevention.** Counting outbound now catches the *sequential* case —
  if another agent's send lands before this agent's send, the gate trips and forces a
  re-plan (collision *detection*, like Help Scout). It does **not** prevent two agents that
  pass their checks *simultaneously* from both sending (a TOCTOU window between check and
  send). The complete fix is a shared per-chat lock (mutual exclusion) — still out of scope.
- Gating `send_reaction` / `edit_message`.
- Gating address-based sends after resolving them to an existing chat.
