"""Tests for the per-agent freshness guard (bb_mcp.freshness + server wiring)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp.types import RequestParams

from bb_mcp.freshness import (
    FreshnessError,
    FreshnessTracker,
    newest_message_ts,
)
from bb_mcp.server import (
    _agent_id,
    _assert_freshness_transport_compatible,
    _build_freshness,
    _check_freshness,
    _record_watermark,
    mcp,
    send_message,
)


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ===========================================================================
# FreshnessTracker
# ===========================================================================


class TestFreshnessTracker:
    def test_record_then_last_seen_returns_stamp(self) -> None:
        tracker = FreshnessTracker(ttl_seconds=3600, clock=FakeClock())
        tracker.record("agent-1", "chat-A", 42)
        assert tracker.last_seen("agent-1", "chat-A") == 42

    def test_no_inbound_records_none(self) -> None:
        tracker = FreshnessTracker(ttl_seconds=3600, clock=FakeClock())
        tracker.record("agent-1", "chat-A", None)
        assert tracker.last_seen("agent-1", "chat-A") is None

    def test_last_seen_raises_without_a_read(self) -> None:
        tracker = FreshnessTracker(clock=FakeClock())
        with pytest.raises(FreshnessError):
            tracker.last_seen("agent-1", "chat-A")

    def test_last_seen_raises_after_ttl(self) -> None:
        clock = FakeClock()
        tracker = FreshnessTracker(ttl_seconds=3600, clock=clock)
        tracker.record("agent-1", "chat-A", 42)
        clock.advance(3601)
        with pytest.raises(FreshnessError):
            tracker.last_seen("agent-1", "chat-A")

    def test_fresh_just_under_ttl_still_valid(self) -> None:
        clock = FakeClock()
        tracker = FreshnessTracker(ttl_seconds=3600, clock=clock)
        tracker.record("agent-1", "chat-A", 42)
        clock.advance(3599)
        assert tracker.last_seen("agent-1", "chat-A") == 42

    def test_agents_are_isolated(self) -> None:
        tracker = FreshnessTracker(clock=FakeClock())
        tracker.record("agent-1", "chat-A", 42)
        # agent-2 never read chat-A, so it must not inherit agent-1's watermark.
        with pytest.raises(FreshnessError):
            tracker.last_seen("agent-2", "chat-A")

    def test_count_cap_evicts_oldest_agent(self) -> None:
        tracker = FreshnessTracker(max_agents=2, clock=FakeClock())
        tracker.record("agent-1", "chat-A", 1)
        tracker.record("agent-2", "chat-A", 2)
        tracker.record("agent-3", "chat-A", 3)  # evicts least-recently-used agent-1
        with pytest.raises(FreshnessError):
            tracker.last_seen("agent-1", "chat-A")
        assert tracker.last_seen("agent-2", "chat-A") == 2
        assert tracker.last_seen("agent-3", "chat-A") == 3

    def test_access_refreshes_lru_recency(self) -> None:
        tracker = FreshnessTracker(max_agents=2, clock=FakeClock())
        tracker.record("agent-1", "chat-A", 1)
        tracker.record("agent-2", "chat-A", 2)
        tracker.last_seen("agent-1", "chat-A")  # touch agent-1 -> now most recent
        tracker.record("agent-3", "chat-A", 3)  # evicts agent-2 instead
        assert tracker.last_seen("agent-1", "chat-A") == 1
        with pytest.raises(FreshnessError):
            tracker.last_seen("agent-2", "chat-A")


# ===========================================================================
# newest_inbound_ts
# ===========================================================================


class TestNewestMessageTs:
    def test_returns_newest_over_all_senders(self) -> None:
        messages = [
            {"guid": "a", "isFromMe": False, "dateCreated": 100},
            {"guid": "b", "isFromMe": True, "dateCreated": 200},  # outbound counts now
            {"guid": "c", "isFromMe": False, "dateCreated": 150},
        ]
        assert newest_message_ts(messages) == 200

    def test_counts_outbound_messages(self) -> None:
        messages = [
            {"guid": "a", "isFromMe": True, "dateCreated": 100},
            {"guid": "b", "isFromMe": True, "dateCreated": 200},
        ]
        assert newest_message_ts(messages) == 200

    def test_empty_and_non_list(self) -> None:
        assert newest_message_ts([]) is None
        assert newest_message_ts(None) is None
        assert newest_message_ts({"isFromMe": False}) is None

    def test_skips_non_message_dicts(self) -> None:
        messages = [
            {"some": "chat-node"},  # no isFromMe -> not a message
            {"guid": "c", "isFromMe": False, "dateCreated": 150},
        ]
        assert newest_message_ts(messages) == 150

    def test_tolerates_missing_datecreated(self) -> None:
        messages = [
            {"guid": "a", "isFromMe": True, "dateCreated": None},
            {"guid": "c", "isFromMe": False, "dateCreated": 150},
        ]
        assert newest_message_ts(messages) == 150


# ===========================================================================
# Server wiring (_agent_id / _record_watermark / _check_freshness)
# ===========================================================================


class FakeBB:
    def __init__(self, messages: list[dict], send_ts: int | None = None) -> None:
        self.messages = messages
        self.send_ts = send_ts
        self.sent: list[tuple] = []

    async def get_chat_messages(self, chat_guid: str, **kwargs) -> list[dict]:
        return self.messages

    async def send_message(self, chat_guid: str, message: str, **kwargs) -> dict:
        self.sent.append((chat_guid, message))
        sent = {"guid": "sent-1", "isFromMe": True, "dateCreated": self.send_ts}
        self.messages = self.messages + [sent]  # the send advances the conversation
        return sent


class FakeGuard:
    async def check_chat(self, chat_guid: str) -> None:
        return None


def make_ctx(*, freshness=None, meta=None, bb=None, private_api=True,
             identity="meta", session=None):
    meta_obj = RequestParams.Meta.model_validate(meta) if meta is not None else None
    request_context = SimpleNamespace(
        lifespan_context={
            "freshness": freshness,
            "freshness_identity": identity,
            "bb": bb,
            "guard": FakeGuard(),
            "private_api": private_api,
        },
        meta=meta_obj,
        session=session if session is not None else SimpleNamespace(),
    )
    return SimpleNamespace(request_context=request_context)


class TestAgentId:
    def test_meta_mode_returns_stamped_id(self) -> None:
        ctx = make_ctx(identity="meta", meta={"agentId": "agent-1"})
        assert _agent_id(ctx) == "meta:agent-1"

    def test_meta_mode_missing_meta_raises(self) -> None:
        with pytest.raises(FreshnessError):
            _agent_id(make_ctx(identity="meta", meta=None))

    @pytest.mark.parametrize("bad", [{"agentId": ""}, {"agentId": "  "}, {"other": "x"}])
    def test_meta_mode_blank_or_absent_id_raises(self, bad: dict) -> None:
        with pytest.raises(FreshnessError):
            _agent_id(make_ctx(identity="meta", meta=bad))

    def test_session_mode_uses_transport_session_no_meta_needed(self) -> None:
        ctx = make_ctx(identity="session", meta=None)
        agent_id = _agent_id(ctx)
        assert agent_id.startswith("session:")
        # Stable across calls within the same connection.
        assert _agent_id(ctx) == agent_id

    def test_session_mode_distinct_sessions_get_distinct_ids(self) -> None:
        assert _agent_id(make_ctx(identity="session")) != _agent_id(
            make_ctx(identity="session")
        )


class TestBuildFreshness:
    """The on/off switch, identity mode, and tuning knobs."""

    def test_on_by_default_with_session_identity(self) -> None:
        tracker, identity = _build_freshness({})
        assert isinstance(tracker, FreshnessTracker)
        assert identity == "session"

    @pytest.mark.parametrize("val", ["false", "0", "no", "off"])
    def test_disabled_only_for_explicit_off(self, val: str) -> None:
        assert _build_freshness({"BLUEBUBBLES_FRESHNESS": val}) == (None, "off")

    @pytest.mark.parametrize("val", ["true", "1", "yes", "on", "auto", "garbage", ""])
    def test_stays_on_otherwise(self, val: str) -> None:
        tracker, _ = _build_freshness({"BLUEBUBBLES_FRESHNESS": val})
        assert isinstance(tracker, FreshnessTracker)

    def test_meta_identity_selected(self) -> None:
        _, identity = _build_freshness({"BLUEBUBBLES_FRESHNESS_IDENTITY": "meta"})
        assert identity == "meta"

    @pytest.mark.parametrize("val", ["session", "SESSION", "bogus", ""])
    def test_identity_defaults_to_session(self, val: str) -> None:
        _, identity = _build_freshness({"BLUEBUBBLES_FRESHNESS_IDENTITY": val})
        assert identity == "session"

    def test_env_overrides_ttl_and_cap(self) -> None:
        tracker, _ = _build_freshness({
            "BLUEBUBBLES_WATERMARK_TTL_SECONDS": "60",
            "BLUEBUBBLES_WATERMARK_MAX_AGENTS": "5",
        })
        assert tracker is not None
        assert tracker._ttl == 60.0
        assert tracker._max_agents == 5


class TestStatelessHttpAssert:
    """Fail fast when session-identity freshness meets stateless HTTP, where every
    send would otherwise be silently blocked."""

    def test_raises_for_stateless_http_session_mode(self, monkeypatch) -> None:
        monkeypatch.setattr(mcp.settings, "stateless_http", True)
        with pytest.raises(RuntimeError, match="stateless HTTP"):
            _assert_freshness_transport_compatible("streamable-http", {})

    def test_ok_in_meta_mode(self, monkeypatch) -> None:
        monkeypatch.setattr(mcp.settings, "stateless_http", True)
        _assert_freshness_transport_compatible(
            "streamable-http", {"BLUEBUBBLES_FRESHNESS_IDENTITY": "meta"}
        )

    def test_ok_when_guard_off(self, monkeypatch) -> None:
        monkeypatch.setattr(mcp.settings, "stateless_http", True)
        _assert_freshness_transport_compatible(
            "streamable-http", {"BLUEBUBBLES_FRESHNESS": "off"}
        )

    def test_ok_when_http_is_stateful(self, monkeypatch) -> None:
        monkeypatch.setattr(mcp.settings, "stateless_http", False)
        _assert_freshness_transport_compatible("streamable-http", {})

    def test_ok_for_stdio_even_if_flag_set(self, monkeypatch) -> None:
        # stateless_http does nothing for stdio (sessions are always persistent).
        monkeypatch.setattr(mcp.settings, "stateless_http", True)
        _assert_freshness_transport_compatible("stdio", {})


class TestGuardDisabled:
    """With the guard off (default), no agentId is needed and nothing blocks —
    even in a scenario that would block when enabled."""

    def test_record_is_noop_without_agent_id(self) -> None:
        ctx = make_ctx(freshness=None, meta=None)
        _record_watermark(ctx, "chat-A", [{"isFromMe": False, "dateCreated": 1}])

    async def test_check_is_noop_without_agent_id(self) -> None:
        ctx = make_ctx(freshness=None, meta=None)
        await _check_freshness(ctx, "chat-A")  # does not raise

    async def test_send_with_no_read_and_new_inbound_is_allowed_when_off(self) -> None:
        # No prior read AND fresh inbound present — would block if enabled; with the
        # guard off it must sail through (preserves single-agent stdio behavior).
        bb = FakeBB([{"guid": "a", "isFromMe": False, "dateCreated": 999}])
        ctx = make_ctx(freshness=None, meta=None, bb=bb)
        await _check_freshness(ctx, "chat-A")


class TestGuardEnabled:
    def _ctx(self, tracker, messages, agent="agent-1"):
        return make_ctx(
            freshness=tracker, meta={"agentId": agent}, bb=FakeBB(messages)
        )

    async def test_read_then_send_is_allowed(self) -> None:
        tracker = FreshnessTracker(clock=FakeClock())
        msgs = [{"guid": "a", "isFromMe": False, "dateCreated": 100}]
        ctx = self._ctx(tracker, msgs)
        _record_watermark(ctx, "chat-A", msgs)
        await _check_freshness(ctx, "chat-A")  # nothing new -> allowed

    async def test_send_without_prior_read_is_blocked(self) -> None:
        tracker = FreshnessTracker(clock=FakeClock())
        ctx = self._ctx(tracker, [])
        with pytest.raises(FreshnessError):
            await _check_freshness(ctx, "chat-A")

    async def test_new_inbound_since_read_is_blocked(self) -> None:
        tracker = FreshnessTracker(clock=FakeClock())
        seen = [{"guid": "a", "isFromMe": False, "dateCreated": 100}]
        _record_watermark(self._ctx(tracker, seen), "chat-A", seen)
        # A newer inbound message lands before the send.
        live = seen + [{"guid": "b", "isFromMe": False, "dateCreated": 200}]
        with pytest.raises(FreshnessError):
            await _check_freshness(self._ctx(tracker, live), "chat-A")

    async def test_outbound_bargein_since_read_is_blocked(self) -> None:
        # A message from our side (another agent, or the user on another device)
        # landed after the read -> the queued reply is stale, block it.
        tracker = FreshnessTracker(clock=FakeClock())
        seen = [{"guid": "a", "isFromMe": False, "dateCreated": 100}]
        _record_watermark(self._ctx(tracker, seen), "chat-A", seen)
        live = seen + [{"guid": "b", "isFromMe": True, "dateCreated": 300}]
        with pytest.raises(FreshnessError):
            await _check_freshness(self._ctx(tracker, live), "chat-A")


class TestSendMessageToolWiring:
    """End-to-end through the real send_message tool: the gate is actually wired
    in, and the switch turns the whole thing on/off."""

    async def test_disabled_send_works_without_agent_id(self) -> None:
        bb = FakeBB([{"guid": "a", "isFromMe": False, "dateCreated": 100}])
        ctx = make_ctx(freshness=None, meta=None, bb=bb)
        await send_message(ctx, "iMessage;-;+15551234567", "hi")
        assert bb.sent == [("iMessage;-;+15551234567", "hi")]

    async def test_enabled_blocks_send_without_a_read(self) -> None:
        tracker = FreshnessTracker(clock=FakeClock())
        bb = FakeBB([])
        ctx = make_ctx(freshness=tracker, meta={"agentId": "agent-1"}, bb=bb)
        with pytest.raises(FreshnessError):
            await send_message(ctx, "chat-A", "hi")
        assert bb.sent == []  # blocked before the send

    async def test_enabled_allows_send_after_a_read(self) -> None:
        tracker = FreshnessTracker(clock=FakeClock())
        msgs = [{"guid": "a", "isFromMe": False, "dateCreated": 100}]
        bb = FakeBB(msgs)
        ctx = make_ctx(freshness=tracker, meta={"agentId": "agent-1"}, bb=bb)
        _record_watermark(ctx, "chat-A", msgs)
        await send_message(ctx, "chat-A", "hi")
        assert bb.sent == [("chat-A", "hi")]

    async def test_enabled_requires_agent_id_on_send(self) -> None:
        tracker = FreshnessTracker(clock=FakeClock())
        ctx = make_ctx(freshness=tracker, meta=None, bb=FakeBB([]))
        with pytest.raises(FreshnessError):
            await send_message(ctx, "chat-A", "hi")


class TestSessionModeFlow:
    """Standalone (no airlock): identity comes from the transport session, so no
    _meta.agentId is needed — stdio and direct-HTTP get freshness for free."""

    async def test_read_then_send_same_session_allowed(self) -> None:
        tracker = FreshnessTracker(clock=FakeClock())
        msgs = [{"guid": "a", "isFromMe": False, "dateCreated": 100}]
        bb = FakeBB(msgs)
        ctx = make_ctx(identity="session", freshness=tracker, meta=None, bb=bb)
        _record_watermark(ctx, "chat-A", msgs)
        await send_message(ctx, "chat-A", "hi")
        assert bb.sent == [("chat-A", "hi")]

    async def test_send_without_read_blocked(self) -> None:
        tracker = FreshnessTracker(clock=FakeClock())
        ctx = make_ctx(identity="session", freshness=tracker, meta=None, bb=FakeBB([]))
        with pytest.raises(FreshnessError):
            await send_message(ctx, "chat-A", "hi")

    async def test_two_sessions_are_isolated(self) -> None:
        tracker = FreshnessTracker(clock=FakeClock())
        msgs = [{"guid": "a", "isFromMe": False, "dateCreated": 100}]
        reader = make_ctx(identity="session", freshness=tracker, bb=FakeBB(msgs))
        _record_watermark(reader, "chat-A", msgs)
        # A different connection (session) never read chat-A -> still blocked.
        other = make_ctx(identity="session", freshness=tracker, bb=FakeBB(msgs))
        with pytest.raises(FreshnessError):
            await send_message(other, "chat-A", "hi")

    async def test_own_send_does_not_block_followup(self) -> None:
        # Sending advances the agent's own watermark, so a second send to the same
        # chat (without re-reading) isn't blocked by the agent's own first message.
        tracker = FreshnessTracker(clock=FakeClock())
        seen = [{"guid": "a", "isFromMe": False, "dateCreated": 100}]
        bb = FakeBB(seen, send_ts=300)
        ctx = make_ctx(identity="session", freshness=tracker, meta=None, bb=bb)
        _record_watermark(ctx, "chat-A", seen)
        await send_message(ctx, "chat-A", "first")
        await send_message(ctx, "chat-A", "second")
        assert len(bb.sent) == 2

    async def test_outbound_bargein_blocks_send(self) -> None:
        # Another agent (or the user on another device) sends -> outbound message
        # the agent didn't author -> its queued send is blocked.
        tracker = FreshnessTracker(clock=FakeClock())
        seen = [{"guid": "a", "isFromMe": False, "dateCreated": 100}]
        bb = FakeBB(seen)
        ctx = make_ctx(identity="session", freshness=tracker, meta=None, bb=bb)
        _record_watermark(ctx, "chat-A", seen)
        bb.messages = seen + [{"guid": "x", "isFromMe": True, "dateCreated": 200}]
        with pytest.raises(FreshnessError):
            await send_message(ctx, "chat-A", "hi")
        assert bb.sent == []
