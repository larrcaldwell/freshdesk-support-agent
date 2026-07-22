"""Lightweight SQLite event log for the dashboard.

Lives in DATA_DIR (a Render persistent disk, e.g. /data) when that env var is
set; otherwise falls back to /tmp (which resets on restart/deploy).
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

_data_dir = Path(os.environ.get("DATA_DIR", "/tmp"))
try:
    _data_dir.mkdir(parents=True, exist_ok=True)
except Exception:
    _data_dir = Path("/tmp")
DB = _data_dir / "agent_events.db"


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
                ref TEXT,             -- stable key (e.g. chat conversation id) for upserts
                channel TEXT,         -- email | portal | phone | chat | live-chat | ...
                customer TEXT         -- requester / chat user name
            )"""
        )
        # Migrations for databases created before these columns existed.
        for col in ("channel", "customer"):
            try:
                c.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass


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
    channel: str = "",
    customer: str = "",
) -> None:
    init()
    with _conn() as c:
        if ref:
            c.execute("DELETE FROM events WHERE ref = ?", (ref,))
        c.execute(
            "INSERT INTO events (ts, ticket_id, subject, category, priority, sentiment,"
            " confidence, needs_human, action, detail, ref, channel, customer) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                channel,
                customer[:120],
            ),
        )


def recent(limit: int = 50) -> list[dict]:
    init()
    with _conn() as c:
        rows = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def events_since(hours: float | None = None, limit: int = 2000) -> list[dict]:
    """All events, newest first, optionally restricted to the last N hours."""
    init()
    with _conn() as c:
        if hours:
            since = time.time() - hours * 3600
            rows = c.execute(
                "SELECT * FROM events WHERE ts > ? ORDER BY id DESC LIMIT ?", (since, limit)
            ).fetchall()
        else:
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


def init_shipments() -> None:
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS shipments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                so_id TEXT,            -- Zoho sales order id
                so_number TEXT,
                customer TEXT,
                service TEXT,
                charge TEXT,
                trackings TEXT,        -- comma-separated tracking numbers
                labels TEXT,           -- JSON list of base64 GIF labels
                players INTEGER,       -- players in this shipment
                address TEXT           -- ship-to used for this label
            )"""
        )
        for col, typ in (("players", "INTEGER"), ("address", "TEXT")):
            try:
                c.execute(f"ALTER TABLE shipments ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        c.execute(
            """CREATE TABLE IF NOT EXISTS ship_status (
                so_id TEXT PRIMARY KEY,
                status TEXT,           -- completed | dismissed
                ts REAL
            )"""
        )


def record_shipment(so_id: str, so_number: str, customer: str, service: str, charge: str,
                    trackings: list, labels: list, players: int = 0, address: str = "") -> int:
    import json as _json

    init_shipments()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO shipments (ts, so_id, so_number, customer, service, charge, trackings, labels, players, address)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), str(so_id), so_number, customer, service, charge, ",".join(trackings),
             _json.dumps(labels), players, address[:300]),
        )
        return cur.lastrowid


def players_shipped(so_id: str) -> int:
    init_shipments()
    with _conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(players), 0) n FROM shipments WHERE so_id = ?", (str(so_id),)
        ).fetchone()
    return int(row["n"] or 0)


def shipments_for_so(so_id: str) -> list[dict]:
    init_shipments()
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ts, service, charge, trackings, players, address FROM shipments"
            " WHERE so_id = ? ORDER BY id DESC", (str(so_id),)
        ).fetchall()
    return [dict(r) for r in rows]


def set_ship_status(so_id: str, status: str | None) -> None:
    init_shipments()
    with _conn() as c:
        if status:
            c.execute(
                "INSERT INTO ship_status (so_id, status, ts) VALUES (?,?,?)"
                " ON CONFLICT(so_id) DO UPDATE SET status=excluded.status, ts=excluded.ts",
                (str(so_id), status, time.time()),
            )
        else:
            c.execute("DELETE FROM ship_status WHERE so_id = ?", (str(so_id),))


def ship_statuses() -> dict[str, str]:
    init_shipments()
    with _conn() as c:
        rows = c.execute("SELECT so_id, status FROM ship_status").fetchall()
    return {r["so_id"]: r["status"] for r in rows}


def get_shipment(shipment_id: int) -> dict | None:
    init_shipments()
    with _conn() as c:
        row = c.execute("SELECT * FROM shipments WHERE id = ?", (shipment_id,)).fetchone()
    return dict(row) if row else None


def shipment_for_so(so_id: str) -> dict | None:
    init_shipments()
    with _conn() as c:
        row = c.execute(
            "SELECT id, ts, so_number, service, charge, trackings FROM shipments WHERE so_id = ? ORDER BY id DESC LIMIT 1",
            (str(so_id),),
        ).fetchone()
    return dict(row) if row else None


def init_journal() -> None:
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                kind TEXT,             -- ticket | chat
                ref TEXT,              -- ticket id or conversation id
                input TEXT,            -- what the agent was given (excerpt)
                teachings INTEGER,     -- number of team teachings active at the time
                trace TEXT,            -- JSON list of tool calls + result previews
                verdict TEXT           -- JSON final decision incl. reply + reasoning
            )"""
        )


def journal(kind: str, ref: str, input_text: str, teachings: int, trace: list, verdict: dict) -> None:
    import json as _json

    init_journal()
    with _conn() as c:
        c.execute(
            "INSERT INTO journal (ts, kind, ref, input, teachings, trace, verdict) VALUES (?,?,?,?,?,?,?)",
            (
                time.time(),
                kind,
                str(ref),
                input_text[:2000],
                teachings,
                _json.dumps(trace)[:8000],
                _json.dumps(verdict)[:6000],
            ),
        )


def journal_rows(limit: int = 2000) -> list[dict]:
    init_journal()
    with _conn() as c:
        rows = c.execute("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
