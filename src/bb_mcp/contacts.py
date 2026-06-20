"""Join BlueBubbles' contact database onto message/chat data.

BlueBubbles keeps contacts (names) separate from message data (raw phone numbers
and emails). This module resolves handles to human names so a client reads "Mom"
instead of ``+15551234567`` and can look chats up by name.

Resolution is **lazy and per-session cached**: enriching a response batches all
its uncached addresses into a single ``/contact/query``, then reuses the result.
The full contact list is only fetched for name search (``find``), and warms the
same cache when it is.
"""

from __future__ import annotations

from typing import Any, Iterable

from bb_mcp.client import BlueBubblesClient
from bb_mcp.policy import DEFAULT_REGION, normalize_address


def contact_display_name(contact: dict[str, Any]) -> str | None:
    """Best human-readable name for a BlueBubbles contact record.

    The server usually fills ``displayName``; we fall back to first/last/nickname
    the same way it does, in case an older server or raw record omits it.
    """
    name = (contact.get("displayName") or "").strip()
    if name:
        return name
    first = (contact.get("firstName") or "").strip()
    last = (contact.get("lastName") or "").strip()
    full = f"{first} {last}".strip()
    if full:
        return full
    return (contact.get("nickname") or "").strip() or None


def contact_addresses(contact: dict[str, Any]) -> list[str]:
    """Every phone number and email on a contact record, as plain strings."""
    out: list[str] = []
    for group in ("phoneNumbers", "emails"):
        for entry in contact.get(group) or []:
            addr = entry.get("address") if isinstance(entry, dict) else entry
            if addr:
                out.append(str(addr))
    return out


def collect_addresses(obj: Any) -> set[str]:
    """Find every handle address anywhere in a chat/message JSON structure."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            addr = node.get("address")
            if isinstance(addr, str) and addr:
                found.add(addr)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(obj)
    return found


def inject_names(obj: Any, names: dict[str, str]) -> Any:
    """Add ``contactName`` beside each handle ``address`` that resolved to a name.

    Mutates ``obj`` in place (and returns it) so it works on the raw API payload.
    """

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            addr = node.get("address")
            if isinstance(addr, str) and addr in names:
                node["contactName"] = names[addr]
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(obj)
    return obj


class ContactResolver:
    """Resolves handle addresses to contact names, cached for the session."""

    def __init__(self, client: BlueBubblesClient, region: str = DEFAULT_REGION) -> None:
        self._client = client
        self._region = region
        self._cache: dict[str, str | None] = {}  # normalized address -> name | None
        self._all_contacts: list[dict[str, Any]] | None = None

    def normalize(self, address: str) -> str:
        return normalize_address(address, self._region)

    def _remember(self, contact: dict[str, Any]) -> None:
        name = contact_display_name(contact)
        if not name:
            return
        for addr in contact_addresses(contact):
            self._cache[self.normalize(addr)] = name

    async def names_for(self, addresses: Iterable[str]) -> dict[str, str]:
        """Map each input address to a contact name, when one exists.

        Batches the not-yet-cached addresses into a single ``/contact/query``.
        Addresses with no contact are cached as ``None`` so they aren't requeried.
        Keys in the returned dict are the original input strings.
        """
        wanted = [a for a in dict.fromkeys(addresses) if a]  # de-dupe, keep order
        missing = [a for a in wanted if self.normalize(a) not in self._cache]
        if missing:
            try:
                contacts = await self._client.query_contacts(missing)
            except Exception:  # noqa: BLE001 — enrichment must never break a read
                contacts = []
            for contact in contacts or []:
                self._remember(contact)
            for a in missing:  # cache misses so we don't requery them
                self._cache.setdefault(self.normalize(a), None)
        return {a: self._cache[self.normalize(a)] for a in wanted if self._cache.get(self.normalize(a))}

    async def all_contacts(self) -> list[dict[str, Any]]:
        """Fetch (once) and cache the full contact list, warming the name cache."""
        if self._all_contacts is None:
            try:
                self._all_contacts = await self._client.get_contacts() or []
            except Exception:  # noqa: BLE001
                self._all_contacts = []
            for contact in self._all_contacts:
                self._remember(contact)
        return self._all_contacts

    async def find(self, name_query: str) -> list[dict[str, Any]]:
        """Contacts whose name contains ``name_query`` (case-insensitive)."""
        q = name_query.strip().lower()
        if not q:
            return []
        out = []
        for contact in await self.all_contacts():
            name = contact_display_name(contact) or ""
            if q in name.lower():
                out.append(contact)
        return out
