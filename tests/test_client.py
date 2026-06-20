"""Comprehensive tests for BlueBubblesClient."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx

from bb_mcp.client import BlueBubblesClient, BlueBubblesError

BASE_URL = "http://bb.local:1234"
API = f"{BASE_URL}/api/v1"
PASSWORD = "test-secret"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ok_json(data: Any = None, *, status: int = 200) -> httpx.Response:
    """Build a successful BlueBubbles-style JSON response."""
    return httpx.Response(status, json={"status": 200, "data": data})


def api_error_json(message: str = "Not found", *, api_status: int = 404) -> httpx.Response:
    """Build a response where HTTP is 200 but the API body signals an error."""
    return httpx.Response(200, json={"status": api_status, "message": message})


# ===========================================================================
# Construction & helpers
# ===========================================================================

class TestClientInit:
    def test_trailing_slash_stripped(self) -> None:
        c = BlueBubblesClient(base_url="http://host:1234/", password="pw")
        assert c._base_url == "http://host:1234"

    def test_url_builder(self) -> None:
        c = BlueBubblesClient(base_url=BASE_URL, password="pw")
        assert c._url("/ping") == f"{API}/ping"

    def test_auth_params_default(self) -> None:
        c = BlueBubblesClient(base_url=BASE_URL, password="pw")
        assert c._auth_params() == {"password": "pw"}

    def test_auth_params_with_extra(self) -> None:
        c = BlueBubblesClient(base_url=BASE_URL, password="pw")
        params = c._auth_params({"foo": "bar"})
        assert params == {"password": "pw", "foo": "bar"}

    async def test_close_delegates(self) -> None:
        c = BlueBubblesClient(base_url=BASE_URL, password="pw")
        with patch.object(c._http, "aclose") as mock_close:
            await c.close()
            mock_close.assert_awaited_once()


# ===========================================================================
# Internal HTTP methods – happy & error paths
# ===========================================================================

class TestInternalGet:
    async def test_get_returns_data(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.get(f"{API}/ping").mock(return_value=ok_json("pong"))
        result = await client._get("/ping")
        assert result == "pong"

    async def test_get_passes_extra_params(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.get(f"{API}/test").mock(return_value=ok_json("ok"))
        await client._get("/test", params={"extra": "1"})
        assert route.called
        req = route.calls[0].request
        assert req.url.params.get("password") == PASSWORD
        assert req.url.params.get("extra") == "1"

    async def test_get_http_error_raises(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.get(f"{API}/fail").mock(return_value=httpx.Response(500))
        with pytest.raises(httpx.HTTPStatusError):
            await client._get("/fail")

    async def test_get_api_error_raises(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.get(f"{API}/bad").mock(return_value=api_error_json("Oops", api_status=400))
        with pytest.raises(BlueBubblesError, match="Oops"):
            await client._get("/bad")

    async def test_get_api_error_attaches_body(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        body = {"status": 500, "message": "boom"}
        mock_api.get(f"{API}/err").mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(BlueBubblesError) as exc_info:
            await client._get("/err")
        assert exc_info.value.response_body == body


class TestInternalPost:
    async def test_post_sends_json_body(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/action").mock(return_value=ok_json("done"))
        await client._post("/action", json={"key": "val"})
        import json
        assert json.loads(route.calls[0].request.content) == {"key": "val"}

    async def test_post_api_error(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.post(f"{API}/action").mock(return_value=api_error_json("fail", api_status=422))
        with pytest.raises(BlueBubblesError, match="fail"):
            await client._post("/action")


class TestInternalDelete:
    async def test_delete_returns_data(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.delete(f"{API}/thing/1").mock(return_value=ok_json(None))
        result = await client._delete("/thing/1")
        assert result is None

    async def test_delete_api_error(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.delete(f"{API}/thing/1").mock(return_value=api_error_json("nope", api_status=403))
        with pytest.raises(BlueBubblesError, match="nope"):
            await client._delete("/thing/1")


class TestInternalPut:
    async def test_put_sends_json(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.put(f"{API}/item").mock(return_value=ok_json("updated"))
        result = await client._put("/item", json={"x": 1})
        assert result == "updated"
        import json
        assert json.loads(route.calls[0].request.content) == {"x": 1}

    async def test_put_api_error(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.put(f"{API}/item").mock(return_value=api_error_json("bad", api_status=400))
        with pytest.raises(BlueBubblesError, match="bad"):
            await client._put("/item")


# ===========================================================================
# Server endpoints
# ===========================================================================

class TestServerEndpoints:
    async def test_ping(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.get(f"{API}/ping").mock(return_value=ok_json("pong"))
        assert await client.ping() == "pong"

    async def test_server_info(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        info = {"os_version": "14.0", "server_version": "1.9.0"}
        mock_api.get(f"{API}/server/info").mock(return_value=ok_json(info))
        assert await client.server_info() == info


# ===========================================================================
# Chat endpoints
# ===========================================================================

class TestChats:
    async def test_list_chats_defaults(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        chats = [{"guid": "chat1"}]
        route = mock_api.post(f"{API}/chat/query").mock(return_value=ok_json(chats))
        result = await client.list_chats()
        assert result == chats
        body = route.calls[0].request.content
        import json
        parsed = json.loads(body)
        assert parsed["limit"] == 25
        assert parsed["offset"] == 0
        assert parsed["sort"] == "lastmessage"
        assert "with" not in parsed

    async def test_list_chats_with_fields(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/chat/query").mock(return_value=ok_json([]))
        await client.list_chats(limit=10, offset=5, with_fields=["participants"])
        import json
        parsed = json.loads(route.calls[0].request.content)
        assert parsed["limit"] == 10
        assert parsed["offset"] == 5
        assert parsed["with"] == ["participants"]

    async def test_get_chat(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        chat = {"guid": "iMessage;+;chat1"}
        mock_api.get(f"{API}/chat/iMessage;+;chat1").mock(return_value=ok_json(chat))
        result = await client.get_chat("iMessage;+;chat1")
        assert result == chat

    async def test_get_chat_with_fields(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.get(f"{API}/chat/g1").mock(return_value=ok_json({}))
        await client.get_chat("g1", with_fields=["participants", "lastMessage"])
        assert route.calls[0].request.url.params.get("with") == "participants,lastMessage"

    async def test_get_chat_messages_defaults(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        msgs = [{"guid": "msg1"}]
        route = mock_api.get(f"{API}/chat/g1/message").mock(return_value=ok_json(msgs))
        result = await client.get_chat_messages("g1")
        assert result == msgs
        params = route.calls[0].request.url.params
        assert params.get("limit") == "25"
        assert params.get("sort") == "DESC"
        assert params.get("with") == "attachment"

    async def test_get_chat_messages_with_bounds(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.get(f"{API}/chat/g1/message").mock(return_value=ok_json([]))
        await client.get_chat_messages("g1", after=100, before=200, limit=5, offset=10, sort="ASC")
        params = route.calls[0].request.url.params
        assert params.get("after") == "100"
        assert params.get("before") == "200"
        assert params.get("limit") == "5"
        assert params.get("offset") == "10"
        assert params.get("sort") == "ASC"

    async def test_mark_chat_read(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.post(f"{API}/chat/g1/read").mock(return_value=ok_json(None))
        await client.mark_chat_read("g1")

    async def test_mark_chat_unread(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.post(f"{API}/chat/g1/unread").mock(return_value=ok_json(None))
        await client.mark_chat_unread("g1")

    async def test_start_typing(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.post(f"{API}/chat/g1/typing").mock(return_value=ok_json(None))
        await client.start_typing("g1")

    async def test_stop_typing(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.delete(f"{API}/chat/g1/typing").mock(return_value=ok_json(None))
        await client.stop_typing("g1")

    async def test_delete_chat(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.delete(f"{API}/chat/g1").mock(return_value=ok_json(None))
        await client.delete_chat("g1")

    async def test_rename_group(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.put(f"{API}/chat/g1").mock(return_value=ok_json(None))
        await client.rename_group("g1", "New Name")
        import json
        assert json.loads(route.calls[0].request.content) == {"displayName": "New Name"}

    async def test_add_participant(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/chat/g1/participant/add").mock(return_value=ok_json(None))
        await client.add_participant("g1", "+15551234567")
        import json
        assert json.loads(route.calls[0].request.content) == {"address": "+15551234567"}

    async def test_remove_participant(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/chat/g1/participant/remove").mock(return_value=ok_json(None))
        await client.remove_participant("g1", "+15551234567")
        import json
        assert json.loads(route.calls[0].request.content) == {"address": "+15551234567"}

    async def test_leave_chat(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.post(f"{API}/chat/g1/leave").mock(return_value=ok_json(None))
        await client.leave_chat("g1")


# ===========================================================================
# Message endpoints
# ===========================================================================

class TestMessages:
    async def test_send_message_defaults(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/message/text").mock(return_value=ok_json({"guid": "m1"}))
        result = await client.send_message("g1", "Hello!")
        assert result == {"guid": "m1"}
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["chatGuid"] == "g1"
        assert body["message"] == "Hello!"
        assert body["method"] == "private-api"
        assert body["tempGuid"].startswith("temp-")
        assert "subject" not in body
        assert "selectedMessageGuid" not in body

    async def test_send_message_with_optionals(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/message/text").mock(return_value=ok_json({}))
        await client.send_message(
            "g1", "Hi", method="apple-script", subject="Subj", reply_to_guid="reply-guid"
        )
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["method"] == "apple-script"
        assert body["subject"] == "Subj"
        assert body["selectedMessageGuid"] == "reply-guid"

    async def test_send_message_unique_temp_guids(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.post(f"{API}/message/text").mock(return_value=ok_json({}))
        import json
        guids = set()
        for _ in range(5):
            await client.send_message("g1", "hi")
        for call in mock_api.calls:
            body = json.loads(call.request.content)
            guids.add(body["tempGuid"])
        assert len(guids) == 5

    async def test_send_message_to_address(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/chat/new").mock(return_value=ok_json({"guid": "new-chat"}))
        result = await client.send_message_to_address("+15551234567", "Hey")
        assert result == {"guid": "new-chat"}
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["addresses"] == ["+15551234567"]
        assert body["message"] == "Hey"
        assert body["service"] == "iMessage"
        assert body["method"] == "private-api"
        assert body["tempGuid"].startswith("temp-")

    async def test_send_reaction(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/message/react").mock(return_value=ok_json(None))
        await client.send_reaction("g1", "msg-guid", "love", part_index=2)
        import json
        body = json.loads(route.calls[0].request.content)
        assert body == {
            "chatGuid": "g1",
            "selectedMessageGuid": "msg-guid",
            "reaction": "love",
            "partIndex": 2,
        }

    async def test_edit_message(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/message/m1/edit").mock(return_value=ok_json(None))
        await client.edit_message("m1", "new text")
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["editedMessage"] == "new text"
        assert body["backwardsCompatibilityMessage"] == "Edited to: new text"
        assert body["partIndex"] == 0

    async def test_edit_message_custom_compat(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/message/m1/edit").mock(return_value=ok_json(None))
        await client.edit_message("m1", "new", backwards_compat="custom", part_index=1)
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["backwardsCompatibilityMessage"] == "custom"
        assert body["partIndex"] == 1

    async def test_unsend_message(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/message/m1/unsend").mock(return_value=ok_json(None))
        await client.unsend_message("m1", part_index=3)
        import json
        assert json.loads(route.calls[0].request.content) == {"partIndex": 3}

    async def test_search_messages_defaults(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/message/query").mock(return_value=ok_json([]))
        await client.search_messages()
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["limit"] == 25
        assert body["offset"] == 0
        assert body["sort"] == "DESC"
        assert body["with"] == ["chats", "attachment"]
        assert "where" not in body
        assert "chatGuid" not in body

    async def test_search_messages_with_query(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/message/query").mock(return_value=ok_json([]))
        await client.search_messages(query="hello", chat_guid="g1", after=10, before=20)
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["chatGuid"] == "g1"
        assert body["after"] == 10
        assert body["before"] == 20
        assert len(body["where"]) == 1
        assert "%hello%" in body["where"][0]["args"]["query"]

    async def test_get_message(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        msg = {"guid": "m1", "text": "hi"}
        route = mock_api.get(f"{API}/message/m1").mock(return_value=ok_json(msg))
        result = await client.get_message("m1")
        assert result == msg
        assert route.calls[0].request.url.params.get("with") == "chats,attachments"


# ===========================================================================
# Contacts
# ===========================================================================

class TestContacts:
    async def test_get_contacts(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        contacts = [{"id": 1, "displayName": "Alice"}]
        mock_api.get(f"{API}/contact").mock(return_value=ok_json(contacts))
        assert await client.get_contacts() == contacts

    async def test_query_contacts(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/contact/query").mock(return_value=ok_json([]))
        await client.query_contacts(["+15551234567"])
        import json
        assert json.loads(route.calls[0].request.content) == {"addresses": ["+15551234567"]}


# ===========================================================================
# Handle availability
# ===========================================================================

class TestHandles:
    async def test_imessage_availability(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.get(f"{API}/handle/availability/imessage").mock(
            return_value=ok_json(True)
        )
        result = await client.check_imessage_availability("+15551234567")
        assert result is True
        assert route.calls[0].request.url.params.get("address") == "+15551234567"

    async def test_facetime_availability(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.get(f"{API}/handle/availability/facetime").mock(
            return_value=ok_json(False)
        )
        result = await client.check_facetime_availability("user@example.com")
        assert result is False
        assert route.calls[0].request.url.params.get("address") == "user@example.com"


# ===========================================================================
# Attachments
# ===========================================================================

class TestAttachments:
    async def test_get_attachment(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        att = {"guid": "att1", "mimeType": "image/png"}
        mock_api.get(f"{API}/attachment/att1").mock(return_value=ok_json(att))
        assert await client.get_attachment("att1") == att

    async def test_download_attachment(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        raw = b"\x89PNG\r\n\x1a\nfake"
        route = mock_api.get(f"{API}/attachment/att1/download").mock(
            return_value=httpx.Response(200, content=raw)
        )
        result = await client.download_attachment("att1")
        assert result == raw
        params = route.calls[0].request.url.params
        assert params.get("password") == PASSWORD
        assert params.get("original") == "true"

    async def test_download_attachment_http_error(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.get(f"{API}/attachment/att1/download").mock(
            return_value=httpx.Response(404)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.download_attachment("att1")


# ===========================================================================
# Scheduled messages
# ===========================================================================

class TestScheduledMessages:
    async def test_list_scheduled_messages(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        scheduled = [{"id": 1, "message": "later"}]
        mock_api.get(f"{API}/message/schedule").mock(return_value=ok_json(scheduled))
        assert await client.list_scheduled_messages() == scheduled

    async def test_create_scheduled_message(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/message/schedule").mock(
            return_value=ok_json({"id": 42})
        )
        result = await client.create_scheduled_message("g1", "Hello later", 1700000000)
        assert result == {"id": 42}
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["chatGuid"] == "g1"
        assert body["message"] == "Hello later"
        assert body["scheduledFor"] == 1700000000
        assert body["tempGuid"].startswith("temp-")

    async def test_delete_scheduled_message(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.delete(f"{API}/message/schedule/42").mock(return_value=ok_json(None))
        await client.delete_scheduled_message(42)


# ===========================================================================
# BlueBubblesError
# ===========================================================================

class TestBlueBubblesError:
    def test_message(self) -> None:
        err = BlueBubblesError("something broke")
        assert str(err) == "something broke"
        assert err.response_body is None

    def test_with_response_body(self) -> None:
        body = {"status": 500, "message": "internal"}
        err = BlueBubblesError("internal", body)
        assert err.response_body == body
        assert isinstance(err, Exception)

    def test_api_error_unknown_message(self) -> None:
        """When the API returns no message, we get 'Unknown error'."""
        body = {"status": 500}
        err = BlueBubblesError(body.get("message", "Unknown error"), body)
        assert str(err) == "Unknown error"


# ===========================================================================
# Newly added endpoints (Find My, multipart, handles, icons, scheduled CRUD)
# ===========================================================================

import json


class TestScheduledMessages:
    async def test_create_sends_typed_payload(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/message/schedule").mock(return_value=ok_json({}))
        await client.create_scheduled_message("g1", "hi", 1999999999000, method="apple-script")
        assert json.loads(route.calls[0].request.content) == {
            "type": "send-message",
            "payload": {"chatGuid": "g1", "message": "hi", "method": "apple-script"},
            "scheduledFor": 1999999999000,
            "schedule": {"type": "once"},
        }

    async def test_update_puts_typed_payload(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.put(f"{API}/message/schedule/7").mock(return_value=ok_json({}))
        await client.update_scheduled_message(7, "g1", "bye", 1999999999000)
        body = json.loads(route.calls[0].request.content)
        assert body["type"] == "send-message"
        assert body["payload"]["message"] == "bye"
        assert body["schedule"] == {"type": "once"}

    async def test_get_by_id(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.get(f"{API}/message/schedule/7").mock(return_value=ok_json({"id": 7}))
        assert await client.get_scheduled_message(7) == {"id": 7}


class TestCreateChat:
    async def test_multiple_addresses(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/chat/new").mock(return_value=ok_json({}))
        await client.create_chat(["+15551112222", "a@b.com"], message="hey")
        body = json.loads(route.calls[0].request.content)
        assert body["addresses"] == ["+15551112222", "a@b.com"]
        assert body["message"] == "hey"

    async def test_message_omitted_when_blank(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/chat/new").mock(return_value=ok_json({}))
        await client.create_chat(["+15551112222"])
        assert "message" not in json.loads(route.calls[0].request.content)


class TestFindMy:
    async def test_devices_get(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.get(f"{API}/icloud/findmy/devices").mock(return_value=ok_json([]))
        await client.find_my_devices()
        assert route.called

    async def test_devices_refresh_posts(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/icloud/findmy/devices/refresh").mock(
            return_value=ok_json([])
        )
        await client.find_my_devices(refresh=True)
        assert route.called

    async def test_friends_get(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.get(f"{API}/icloud/findmy/friends").mock(return_value=ok_json([]))
        await client.find_my_friends()
        assert route.called


class TestMultipart:
    async def test_upload_returns_path(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.post(f"{API}/attachment/upload").mock(
            return_value=ok_json({"path": "uuid123/photo.jpg"})
        )
        out = await client.upload_attachment(b"bytes", "photo.jpg", "image/jpeg")
        assert out == {"path": "uuid123/photo.jpg"}

    async def test_send_multipart_body(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/message/multipart").mock(return_value=ok_json({}))
        parts = [{"partIndex": 0, "text": "hi"}]
        await client.send_multipart("g1", parts)
        body = json.loads(route.calls[0].request.content)
        assert body["chatGuid"] == "g1"
        assert body["parts"] == parts
        assert "method" not in body  # multipart is Private-API only


class TestHandlesAndMisc:
    async def test_query_handles_body(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.post(f"{API}/handle/query").mock(return_value=ok_json([]))
        await client.query_handles(address="+1555", limit=10)
        body = json.loads(route.calls[0].request.content)
        assert body == {"limit": 10, "offset": 0, "address": "+1555"}

    async def test_get_focus_status(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.get(f"{API}/handle/+15551234567/focus").mock(
            return_value=ok_json({"focused": True})
        )
        assert await client.get_focus_status("+15551234567") == {"focused": True}

    async def test_delete_chat_message(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.delete(f"{API}/chat/g1/m1").mock(return_value=ok_json(None))
        await client.delete_chat_message("g1", "m1")
        assert route.called

    async def test_get_group_icon_returns_bytes(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        mock_api.get(f"{API}/chat/g1/icon").mock(
            return_value=httpx.Response(200, content=b"\x89PNG")
        )
        assert await client.get_group_icon("g1") == b"\x89PNG"

    async def test_remove_group_icon(
        self, client: BlueBubblesClient, mock_api: respx.Router
    ) -> None:
        route = mock_api.delete(f"{API}/chat/g1/icon").mock(return_value=ok_json(None))
        await client.remove_group_icon("g1")
        assert route.called
