"""Thin client for the Freshdesk REST API v2.

Auth: HTTP Basic with the API key as username and "X" as password.
Docs: https://developers.freshdesk.com/api/
"""
from __future__ import annotations

import html
import logging
import re
import time
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("freshdesk")

SOURCE_NAMES = {1: "email", 2: "portal", 3: "phone", 7: "chat", 9: "feedback-widget", 10: "outbound-email"}
STATUS_NAMES = {2: "open", 3: "pending", 4: "resolved", 5: "closed"}
PRIORITY_NAMES = {1: "low", 2: "medium", 3: "high", 4: "urgent"}
PRIORITY_IDS = {v: k for k, v in PRIORITY_NAMES.items()}


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


class FreshdeskClient:
    def __init__(self) -> None:
        self.base = f"https://{settings.freshdesk_domain}.freshdesk.com/api/v2"
        self.client = httpx.Client(
            auth=(settings.freshdesk_api_key, "X"),
            timeout=30,
            headers={"Content-Type": "application/json"},
        )

    def _request(self, method: str, path: str, **kwargs) -> Any:
        for attempt in range(3):
            resp = self.client.request(method, f"{self.base}{path}", **kwargs)
            if resp.status_code == 429:  # rate limited
                wait = int(resp.headers.get("Retry-After", "10"))
                log.warning("Rate limited; sleeping %ss", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                log.error("Freshdesk %s %s -> %s: %s", method, path, resp.status_code, resp.text[:500])
            resp.raise_for_status()
            return resp.json() if resp.content else None
        resp.raise_for_status()

    # ---- Tickets -----------------------------------------------------------

    def get_ticket(self, ticket_id: int) -> dict:
        """Ticket with requester info and full conversation history."""
        t = self._request("GET", f"/tickets/{ticket_id}?include=requester,conversations")
        return t

    def update_ticket(self, ticket_id: int, **fields) -> dict:
        """Update priority, tags, group_id, responder_id, status, custom_fields, etc."""
        return self._request("PUT", f"/tickets/{ticket_id}", json=fields)

    def reply(self, ticket_id: int, body_html: str) -> dict:
        """Public, customer-facing reply."""
        return self._request("POST", f"/tickets/{ticket_id}/reply", json={"body": body_html})

    def private_note(self, ticket_id: int, body_html: str) -> dict:
        """Private note visible only to agents."""
        return self._request(
            "POST", f"/tickets/{ticket_id}/notes", json={"body": body_html, "private": True}
        )

    def search_tickets(self, query: str, page: int = 1) -> list[dict]:
        """Search past tickets. Query uses Freshdesk's query language,
        e.g. 'status:4 OR status:5'. Free-text queries are not supported on
        all plans, so failures degrade to an empty result."""
        try:
            data = self._request("GET", f"/search/tickets?query=\"{query}\"&page={page}")
            return data.get("results", []) if isinstance(data, dict) else []
        except httpx.HTTPStatusError:
            log.warning("Ticket search unavailable for query %r", query)
            return []

    # ---- Knowledge base (Solutions) ----------------------------------------

    def search_solutions(self, term: str) -> list[dict]:
        """Keyword search over published KB articles."""
        try:
            from urllib.parse import quote

            data = self._request("GET", f"/search/solutions?term={quote(term)}")
            return data if isinstance(data, list) else []
        except httpx.HTTPStatusError as e:
            # /search/solutions requires certain plans; degrade gracefully.
            log.warning("Solutions search unavailable (%s)", e.response.status_code)
            return []

    # ---- Formatting helpers --------------------------------------------------

    @staticmethod
    def ticket_to_text(t: dict) -> str:
        """Render a ticket + conversation as plain text for the model."""
        req = t.get("requester") or {}
        lines = [
            f"Ticket #{t['id']}: {t.get('subject', '(no subject)')}",
            f"Source: {SOURCE_NAMES.get(t.get('source'), t.get('source'))}"
            f" | Status: {STATUS_NAMES.get(t.get('status'), t.get('status'))}"
            f" | Priority: {PRIORITY_NAMES.get(t.get('priority'), t.get('priority'))}",
            f"Requester: {req.get('name', 'unknown')} <{req.get('email', '')}>",
            f"Tags: {', '.join(t.get('tags') or []) or '(none)'}",
            "",
            "--- Original message ---",
            strip_html(t.get("description")),
        ]
        for c in t.get("conversations") or []:
            who = "CUSTOMER" if c.get("incoming") else ("PRIVATE NOTE" if c.get("private") else "AGENT")
            lines += ["", f"--- {who} ({c.get('created_at', '')}) ---", strip_html(c.get("body"))]
        return "\n".join(lines)


fd = FreshdeskClient()
