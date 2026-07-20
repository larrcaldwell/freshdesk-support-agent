"""Lightweight SQLite event log for the dashboard.

Lives in /tmp — on Render's free tier this resets when the service restarts
or spins down, so the dashboard shows recent activity, not full history.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DB = Path("/tmp/agent_events.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                ticket_id INTEGER NOT NULL,
                subject TEXT,
                category TEXT,
                priority TEXT,
                sentiment TEXT,
                confidence INTEGER,
                needs_human INTEGER,
                action TEXT,          -- auto-replied | draft-posted | triage-only | error
                detail TEXT,
                ref TEXT              -- stable key (e.g. chat conversation id) for upserts
            )"""
        )


def record(
    ticket_id: int,
    subject: str = "",
    category: str = "",
    priority: str = "",
    sentiment: str = "",
    confidence: int | None = None,
    needs_human: bool | None = None,
    action: str = "",
    detail: str = "",
    ref: str = "",
) -> None:
    init()
    with _conn() as c:
        if ref:
            c.execute("DELETE FROM events WHERE ref = ?", (ref,))
        c.execute(
            "INSERT INTO events (ts, ticket_id, subject, category, priority, sentiment,"
            " confidence, needs_human, action, detail, ref) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.time(),
                ticket_id,
                subject[:200],
                category,
                priority,
                sentiment,
                confidence,
                None if needs_human is None else int(needs_human),
                action,
                detail[:500],
                ref,
            ),
        )


def recent(limit: int = 50) -> list[dict]:
    init()
    with _conn() as c:
        rows = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def counts(hours: float) -> dict:
    init()
    since = time.time() - hours * 3600
    with _conn() as c:
        rows = c.execute(
            "SELECT action, COUNT(*) n FROM events WHERE ts > ? GROUP BY action", (since,)
        ).fetchall()
    out = {r["action"]: r["n"] for r in rows}
    out["total"] = sum(out.values())
    return out


def init_feedback() -> None:
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                ticket_id INTEGER,
                subject TEXT,
                correction TEXT,
                author TEXT
            )"""
        )


def record_feedback(ticket_id: int, subject: str, correction: str, author: str) -> None:
    init_feedback()
    with _conn() as c:
        c.execute(
            "INSERT INTO feedback (ts, ticket_id, subject, correction, author) VALUES (?,?,?,?,?)",
            (time.time(), ticket_id, subject[:200], correction[:2000], author[:100]),
        )
