"""Team teaching loop.

Corrections submitted from the dashboard are stored durably as PRIVATE NOTES on
a dedicated Freshdesk ticket ("AI Agent Training Log"). That gives us:
- persistence across app restarts (free tier has no disk),
- a human-auditable record inside the helpdesk the team already uses.

Every agent run injects the most recent corrections into the prompt, so a
teaching takes effect on the very next ticket/chat.
"""
from __future__ import annotations

import html as html_mod
import logging
import re
import time

from .freshdesk import fd, strip_html

log = logging.getLogger("training")

TRAINING_EMAIL = "ai-agent-training@trudigital.net"
TRAINING_SUBJECT = "AI Agent Training Log (internal - do not close)"
MARKER = "[teach]"

_ticket_id: int | None = None
_cache: tuple[float, list[str]] | None = None  # (fetched_at, corrections newest-first)
CACHE_TTL = 300  # seconds


def _find_or_create_ticket() -> int:
    global _ticket_id
    if _ticket_id:
        return _ticket_id
    try:
        results = fd._request("GET", f"/tickets?email={TRAINING_EMAIL}")
        if isinstance(results, list) and results:
            _ticket_id = results[0]["id"]
            return _ticket_id
    except Exception:
        log.exception("Training ticket lookup failed")
    t = fd._request(
        "POST",
        "/tickets",
        json={
            "subject": TRAINING_SUBJECT,
            "description": (
                "<p>This ticket stores corrections the support team gives the AI agent "
                "via the dashboard's <b>Teach</b> button. Each private note starting with "
                "[teach] is read by the agent before every draft. Edit or add notes to "
                "teach it directly. Keep this ticket open.</p>"
            ),
            "email": TRAINING_EMAIL,
            "name": "AI Training Log",
            "status": 3,  # pending, so it stays out of the open queue
            "priority": 1,
            "type": "Notification",
            "tags": ["ai-training-log"],
        },
    )
    _ticket_id = t["id"]
    log.info("Created AI Training Log ticket #%s", _ticket_id)
    return _ticket_id


def add_correction(correction: str, context_subject: str = "", ticket_ref: int = 0, author: str = "") -> None:
    tid = _find_or_create_ticket()
    parts = [f"<p><b>{MARKER}</b></p>"]
    if context_subject or ticket_ref:
        ref = f" (ticket #{ticket_ref})" if ticket_ref else ""
        parts.append(f"<p><i>Re: {html_mod.escape(context_subject)}{ref}</i></p>")
    parts.append(f"<p>{html_mod.escape(correction).replace(chr(10), '<br>')}</p>")
    if author:
        parts.append(f"<p><i>— {html_mod.escape(author)}</i></p>")
    fd.private_note(tid, "".join(parts))
    global _cache
    _cache = None  # bust cache so it applies immediately


def load_corrections() -> list[str]:
    global _cache
    now = time.time()
    if _cache and now - _cache[0] < CACHE_TTL:
        return _cache[1]
    corrections: list[str] = []
    try:
        tid = _find_or_create_ticket()
        ticket = fd.get_ticket(tid)
        for c in ticket.get("conversations") or []:
            body = strip_html(c.get("body") or "")
            if MARKER in body:
                text = body.split(MARKER, 1)[1].strip()
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    corrections.append(text)
        corrections.reverse()  # newest first
    except Exception:
        log.exception("Could not load corrections (continuing without)")
    _cache = (now, corrections)
    return corrections


def corrections_block(max_chars: int = 3500) -> str:
    """Formatted block for prompt injection. Empty string if no teachings yet."""
    items = load_corrections()
    if not items:
        return ""
    lines, used = [], 0
    for i, c in enumerate(items, 1):
        line = f"{i}. {c[:600]}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return (
        "\n\nTEAM TEACHINGS — corrections your human teammates gave on past drafts. "
        "These override everything else; follow them exactly:\n" + "\n".join(lines)
    )
