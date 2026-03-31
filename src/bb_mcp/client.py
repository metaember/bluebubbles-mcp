"""Async client for the BlueBubbles REST API."""

from __future__ import annotations

import uuid
from typing import Any

import httpx


class BlueBubblesClient:
    """Thin async wrapper around the BlueBubbles v1 REST API.

    Every request authenticates via the ``password`` query parameter.
    """

    def __init__(self, base_url: str, password: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._password = password
        self._http = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._http.aclose()

    # -- internal helpers -----------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}/api/v1{path}"

    def _auth_params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"password": self._password}
        if extra:
            params.update(extra)
        return params

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._http.get(
            self._url(path), params=self._auth_params(params)
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") and body["status"] >= 400:
            raise BlueBubblesError(body.get("message", "Unknown error"), body)
        return body.get("data")

    async def _post(
        self, path: str, json: dict[str, Any] | None = None, params: dict[str, Any] | None = None
    ) -> Any:
        resp = await self._http.post(
            self._url(path), json=json, params=self._auth_params(params)
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") and body["status"] >= 400:
            raise BlueBubblesError(body.get("message", "Unknown error"), body)
        return body.get("data")

    async def _delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._http.delete(
            self._url(path), params=self._auth_params(params)
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") and body["status"] >= 400:
            raise BlueBubblesError(body.get("message", "Unknown error"), body)
        return body.get("data")

    async def _put(
        self, path: str, json: dict[str, Any] | None = None, params: dict[str, Any] | None = None
    ) -> Any:
        resp = await self._http.put(
            self._url(path), json=json, params=self._auth_params(params)
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") and body["status"] >= 400:
            raise BlueBubblesError(body.get("message", "Unknown error"), body)
        return body.get("data")

    # -- server ---------------------------------------------------------------

    async def ping(self) -> Any:
        return await self._get("/ping")

    async def server_info(self) -> Any:
        return await self._get("/server/info")

    # -- chats ----------------------------------------------------------------

    async def list_chats(
        self,
        limit: int = 25,
        offset: int = 0,
        sort: str = "lastmessage",
        with_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "sort": sort,
        }
        if with_fields:
            body["with"] = with_fields
        return await self._post("/chat/query", json=body)

    async def get_chat(self, chat_guid: str, with_fields: list[str] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if with_fields:
            params["with"] = ",".join(with_fields)
        return await self._get(f"/chat/{chat_guid}", params=params)

    async def get_chat_messages(
        self,
        chat_guid: str,
        limit: int = 25,
        offset: int = 0,
        sort: str = "DESC",
        after: int | None = None,
        before: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "with": "attachment",
        }
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        return await self._get(f"/chat/{chat_guid}/message", params=params)

    async def mark_chat_read(self, chat_guid: str) -> Any:
        return await self._post(f"/chat/{chat_guid}/read")

    async def mark_chat_unread(self, chat_guid: str) -> Any:
        return await self._post(f"/chat/{chat_guid}/unread")

    async def start_typing(self, chat_guid: str) -> Any:
        return await self._post(f"/chat/{chat_guid}/typing")

    async def stop_typing(self, chat_guid: str) -> Any:
        return await self._delete(f"/chat/{chat_guid}/typing")

    async def delete_chat(self, chat_guid: str) -> Any:
        return await self._delete(f"/chat/{chat_guid}")

    async def rename_group(self, chat_guid: str, display_name: str) -> Any:
        return await self._put(f"/chat/{chat_guid}", json={"displayName": display_name})

    async def add_participant(self, chat_guid: str, address: str) -> Any:
        return await self._post(f"/chat/{chat_guid}/participant/add", json={"address": address})

    async def remove_participant(self, chat_guid: str, address: str) -> Any:
        return await self._post(f"/chat/{chat_guid}/participant/remove", json={"address": address})

    async def leave_chat(self, chat_guid: str) -> Any:
        return await self._post(f"/chat/{chat_guid}/leave")

    # -- messages -------------------------------------------------------------

    async def send_message(
        self,
        chat_guid: str,
        message: str,
        method: str = "private-api",
        subject: str | None = None,
        reply_to_guid: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "chatGuid": chat_guid,
            "tempGuid": f"temp-{uuid.uuid4().hex}",
            "message": message,
            "method": method,
        }
        if subject:
            body["subject"] = subject
        if reply_to_guid:
            body["selectedMessageGuid"] = reply_to_guid
        return await self._post("/message/text", json=body)

    async def send_message_to_address(
        self,
        address: str,
        message: str,
        service: str = "iMessage",
        method: str = "private-api",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "addresses": [address],
            "message": message,
            "method": method,
            "service": service,
            "tempGuid": f"temp-{uuid.uuid4().hex}",
        }
        return await self._post("/chat/new", json=body)

    async def send_reaction(
        self,
        chat_guid: str,
        message_guid: str,
        reaction: str,
        part_index: int = 0,
    ) -> Any:
        body: dict[str, Any] = {
            "chatGuid": chat_guid,
            "selectedMessageGuid": message_guid,
            "reaction": reaction,
            "partIndex": part_index,
        }
        return await self._post("/message/react", json=body)

    async def edit_message(
        self,
        message_guid: str,
        new_text: str,
        backwards_compat: str | None = None,
        part_index: int = 0,
    ) -> Any:
        body: dict[str, Any] = {
            "editedMessage": new_text,
            "backwardsCompatibilityMessage": backwards_compat or f"Edited to: {new_text}",
            "partIndex": part_index,
        }
        return await self._post(f"/message/{message_guid}/edit", json=body)

    async def unsend_message(self, message_guid: str, part_index: int = 0) -> Any:
        return await self._post(
            f"/message/{message_guid}/unsend", json={"partIndex": part_index}
        )

    async def search_messages(
        self,
        query: str | None = None,
        chat_guid: str | None = None,
        limit: int = 25,
        offset: int = 0,
        sort: str = "DESC",
        after: int | None = None,
        before: int | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "sort": sort,
            "with": ["chats", "attachment"],
        }
        if chat_guid:
            body["chatGuid"] = chat_guid
        if after:
            body["after"] = after
        if before:
            body["before"] = before
        if query:
            body["where"] = [{"statement": "message.text LIKE :query", "args": {"query": f"%{query}%"}}]
        return await self._post("/message/query", json=body)

    async def get_message(self, message_guid: str) -> dict[str, Any]:
        return await self._get(
            f"/message/{message_guid}",
            params={"with": "chats,attachments"},
        )

    # -- contacts -------------------------------------------------------------

    async def get_contacts(self) -> list[dict[str, Any]]:
        return await self._get("/contact")

    async def query_contacts(self, addresses: list[str]) -> list[dict[str, Any]]:
        return await self._post("/contact/query", json={"addresses": addresses})

    # -- handles --------------------------------------------------------------

    async def check_imessage_availability(self, address: str) -> Any:
        return await self._get("/handle/availability/imessage", params={"address": address})

    async def check_facetime_availability(self, address: str) -> Any:
        return await self._get("/handle/availability/facetime", params={"address": address})

    # -- attachments ----------------------------------------------------------

    async def get_attachment(self, attachment_guid: str) -> dict[str, Any]:
        return await self._get(f"/attachment/{attachment_guid}")

    async def download_attachment(self, attachment_guid: str) -> bytes:
        resp = await self._http.get(
            self._url(f"/attachment/{attachment_guid}/download"),
            params=self._auth_params({"original": "true"}),
        )
        resp.raise_for_status()
        return resp.content

    async def send_attachment(
        self,
        chat_guid: str,
        file_data: bytes,
        filename: str,
        mime_type: str = "application/octet-stream",
        method: str = "private-api",
    ) -> dict[str, Any]:
        resp = await self._http.post(
            self._url("/message/attachment"),
            params=self._auth_params(),
            data={
                "chatGuid": chat_guid,
                "tempGuid": f"temp-{uuid.uuid4().hex}",
                "method": method,
                "name": filename,
            },
            files={"attachment": (filename, file_data, mime_type)},
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") and body["status"] >= 400:
            raise BlueBubblesError(body.get("message", "Unknown error"), body)
        return body.get("data")

    # -- scheduled messages ---------------------------------------------------

    async def list_scheduled_messages(self) -> list[dict[str, Any]]:
        return await self._get("/message/schedule")

    async def create_scheduled_message(
        self,
        chat_guid: str,
        message: str,
        scheduled_for: int,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "chatGuid": chat_guid,
            "message": message,
            "scheduledFor": scheduled_for,
            "tempGuid": f"temp-{uuid.uuid4().hex}",
        }
        return await self._post("/message/schedule", json=body)

    async def delete_scheduled_message(self, schedule_id: int) -> Any:
        return await self._delete(f"/message/schedule/{schedule_id}")


class BlueBubblesError(Exception):
    def __init__(self, message: str, response_body: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.response_body = response_body
