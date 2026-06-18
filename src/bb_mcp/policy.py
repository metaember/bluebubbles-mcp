"""Recipient allowlisting for write operations.

A write allowlist restricts which recipients the server is permitted to message
or otherwise act on. It is enforced server-side (in the tool layer) so that an
MCP client — including one reaching the HTTP transport — cannot bypass it.

Only writes are guarded today. The :class:`Allowlist` loader is keyed by env var
name so a read variant (``BLUEBUBBLES_READ_ALLOWLIST``) can be added later
without changing this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

import phonenumbers

from bb_mcp.client import BlueBubblesClient

DEFAULT_REGION = "US"


def normalize_address(address: str, region: str = DEFAULT_REGION) -> str:
    """Normalize a phone number or email for stable allowlist comparison.

    Emails are lowercased. Phone numbers are parsed to E.164 (so ``(555)
    123-4567``, ``+1 555-123-4567`` and ``5551234567`` all compare equal). If
    parsing fails, we fall back to a digits-only form so comparison still works
    for inputs ``phonenumbers`` can't interpret.

    Args:
        address: A phone number or email address.
        region: Default region used to parse national-format phone numbers.
    """
    address = address.strip()
    if "@" in address:
        return address.lower()
    try:
        parsed = phonenumbers.parse(address, region)
    except phonenumbers.NumberParseException:
        digits = "".join(ch for ch in address if ch.isdigit())
        return digits or address.lower()
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def address_from_guid(chat_guid: str) -> str | None:
    """Extract the recipient address from a one-on-one chat GUID.

    One-on-one GUIDs embed the address as their final segment, e.g.
    ``iMessage;-;+15551234567`` -> ``+15551234567``. Group GUIDs use a ``+``
    marker and an opaque id (``iMessage;+;chat123``); those return ``None`` and
    must be resolved via the API.
    """
    parts = chat_guid.split(";")
    if len(parts) == 3 and parts[1] == "-":
        return parts[2]
    return None


@dataclass(frozen=True)
class Allowlist:
    """A set of permitted recipient addresses.

    ``allowed is None`` means *unrestricted* — no allowlist configured — which is
    the backward-compatible default. A present-but-empty set means *deny all*,
    a safe failure mode for a misconfigured (e.g. ``""``) env var.
    """

    allowed: frozenset[str] | None
    region: str = DEFAULT_REGION

    @classmethod
    def from_env(cls, env: Mapping[str, str], var: str) -> Allowlist:
        """Build an allowlist from a comma-separated env var.

        Absent var -> unrestricted. Present var -> only the listed (normalized)
        addresses are permitted.
        """
        region = env.get("BLUEBUBBLES_ALLOWLIST_REGION", DEFAULT_REGION)
        raw = env.get(var)
        if raw is None:
            return cls(None, region)
        items = {
            normalize_address(part, region)
            for part in raw.split(",")
            if part.strip()
        }
        return cls(frozenset(items), region)

    @property
    def restricted(self) -> bool:
        return self.allowed is not None

    def contains(self, address: str) -> bool:
        if self.allowed is None:
            return True
        return normalize_address(address, self.region) in self.allowed

    def rejected(self, addresses: Iterable[str]) -> set[str]:
        """Return the normalized addresses that are NOT permitted."""
        if self.allowed is None:
            return set()
        return {
            norm
            for norm in (normalize_address(a, self.region) for a in addresses)
            if norm not in self.allowed
        }


class AccessDenied(Exception):
    """Raised when a write targets recipients outside the allowlist."""

    def __init__(self, addresses: Iterable[str]) -> None:
        self.addresses = sorted(addresses)
        joined = ", ".join(self.addresses) if self.addresses else "(unresolved)"
        super().__init__(
            f"Blocked by write allowlist: {joined} is not a permitted recipient. "
            "Add it to BLUEBUBBLES_WRITE_ALLOWLIST to allow."
        )


class Guard:
    """Enforces a write :class:`Allowlist`, resolving chat GUIDs to participants.

    One-on-one GUIDs are resolved by parsing (no API call). Group GUIDs are
    resolved via ``get_chat`` and cached, since participants rarely change
    within a session. When no allowlist is configured, every check is a no-op
    and no API calls are made.
    """

    def __init__(self, allowlist: Allowlist, client: BlueBubblesClient) -> None:
        self._allow = allowlist
        self._client = client
        self._participants_cache: dict[str, set[str]] = {}

    async def _participants(self, chat_guid: str) -> set[str]:
        addr = address_from_guid(chat_guid)
        if addr is not None:
            return {addr}
        if chat_guid in self._participants_cache:
            return self._participants_cache[chat_guid]
        chat = await self._client.get_chat(chat_guid, with_fields=["participants"])
        addrs = {
            p["address"]
            for p in (chat.get("participants") or [])
            if p.get("address")
        }
        self._participants_cache[chat_guid] = addrs
        return addrs

    async def check_chat(self, chat_guid: str) -> None:
        """Allow only if every participant of the chat is on the allowlist.

        Fails closed: a chat whose participants can't be resolved is denied.
        """
        if not self._allow.restricted:
            return
        participants = await self._participants(chat_guid)
        if not participants:
            raise AccessDenied(set())
        rejected = self._allow.rejected(participants)
        if rejected:
            raise AccessDenied(rejected)

    def check_address(self, address: str) -> None:
        """Allow only if the given address is on the allowlist."""
        if not self._allow.restricted:
            return
        if not self._allow.contains(address):
            raise AccessDenied({normalize_address(address, self._allow.region)})
