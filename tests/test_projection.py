"""Tests for compact projection and sender filtering (bb_mcp.projection)."""

from __future__ import annotations

from bb_mcp.projection import (
    compact_message,
    filter_by_sender,
    message_sender,
    project,
)

RAW = {
    "guid": "g1",
    "text": "hello",
    "isFromMe": False,
    "dateCreated": 1700000000000,
    "itemType": 0,
    "groupActionType": 0,
    "attributedBody": {"blob": "x" * 50},
    "handle": {"address": "+15551112222", "contactName": "Alice", "country": "us"},
    "attachments": [
        {"guid": "a1", "transferName": "p.jpg", "mimeType": "image/jpeg",
         "totalBytes": 9, "metadata": {"big": 1}},
    ],
    "chats": [{"guid": "iMessage;-;x", "displayName": "", "style": 45}],
    "associatedMessageGuid": "g0",
    "associatedMessageType": 2000,
}


def _norm(addr: str) -> str:
    return addr.strip().lower()


# ===========================================================================
# compact_message
# ===========================================================================

class TestCompactMessage:
    def test_drops_bulky_fields(self) -> None:
        c = compact_message(RAW)
        for dropped in ("itemType", "groupActionType", "attributedBody"):
            assert dropped not in c

    def test_keeps_core_fields(self) -> None:
        c = compact_message(RAW)
        assert c["guid"] == "g1"
        assert c["text"] == "hello"
        assert c["isFromMe"] is False
        assert c["dateCreated"] == 1700000000000

    def test_handle_keeps_address_and_contact_name(self) -> None:
        assert compact_message(RAW)["handle"] == {
            "address": "+15551112222",
            "contactName": "Alice",
        }

    def test_attachments_compacted(self) -> None:
        assert compact_message(RAW)["attachments"] == [
            {"guid": "a1", "transferName": "p.jpg", "mimeType": "image/jpeg", "totalBytes": 9}
        ]

    def test_chat_context_kept(self) -> None:
        assert compact_message(RAW)["chats"] == [{"guid": "iMessage;-;x"}]

    def test_reaction_linkage_kept(self) -> None:
        c = compact_message(RAW)
        assert c["associatedMessageGuid"] == "g0"
        assert c["associatedMessageType"] == 2000

    def test_absent_fields_omitted(self) -> None:
        c = compact_message({"guid": "g", "isFromMe": True})
        assert "handle" not in c and "attachments" not in c and "text" not in c


# ===========================================================================
# project
# ===========================================================================

class TestProject:
    def test_extended_is_passthrough(self) -> None:
        assert project([RAW], extended=True) == [RAW]

    def test_compacts_message_list(self) -> None:
        out = project([RAW], extended=False)
        assert "attributedBody" not in out[0]

    def test_compacts_nested_messages(self) -> None:
        nested = [{"chat": {"guid": "c", "lastMessage": RAW}, "recent_messages": [RAW]}]
        out = project(nested, extended=False)
        assert "attributedBody" not in out[0]["recent_messages"][0]
        assert "attributedBody" not in out[0]["chat"]["lastMessage"]
        assert out[0]["chat"]["guid"] == "c"  # non-message structure preserved

    def test_non_message_dict_preserved(self) -> None:
        node = {"foo": "bar", "n": 1}
        assert project(node, extended=False) == node


# ===========================================================================
# message_sender / filter_by_sender
# ===========================================================================

class TestSenderFiltering:
    def test_from_me_resolves_to_my_address(self) -> None:
        assert message_sender({"isFromMe": True}, "+MINE") == "+MINE"

    def test_received_resolves_to_handle(self) -> None:
        assert message_sender(RAW, "+MINE") == "+15551112222"

    def test_filter_keeps_only_target_sender(self) -> None:
        msgs = [
            RAW,  # from Alice
            {"guid": "g2", "isFromMe": True, "handle": {"address": "+15551112222"}},  # from me
        ]
        kept = filter_by_sender(msgs, "+mine", "+MINE", _norm)
        assert [m["guid"] for m in kept] == ["g2"]

    def test_filter_by_other_party(self) -> None:
        msgs = [
            RAW,
            {"guid": "g2", "isFromMe": True, "handle": {"address": "+15551112222"}},
        ]
        kept = filter_by_sender(msgs, "+15551112222", "+MINE", _norm)
        assert [m["guid"] for m in kept] == ["g1"]

    def test_ignores_non_dict_entries(self) -> None:
        assert filter_by_sender([None, "x", RAW], "+15551112222", None, _norm) == [RAW]
