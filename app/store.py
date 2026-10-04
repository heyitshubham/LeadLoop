"""Discovered leads and the deal index. Deal state itself lives in the LangGraph checkpointer."""

import json
import sqlite3
import threading
from datetime import datetime, timezone

from app.config import settings
from app.models import Lead

_conn = sqlite3.connect(settings.data_dir / "leadloop.db", check_same_thread=False)
_lock = threading.Lock()
_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS leads (id TEXT PRIMARY KEY, data TEXT NOT NULL, score INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS deals (lead_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
    """
)


def save_leads(leads: list[Lead]) -> None:
    with _lock, _conn:
        _conn.executemany(
            "INSERT OR REPLACE INTO leads (id, data, score) VALUES (?, ?, ?)",
            [(l.id, l.model_dump_json(), l.intent_score) for l in leads],
        )


def list_leads() -> list[Lead]:
    rows = _conn.execute("SELECT data FROM leads ORDER BY score DESC").fetchall()
    return [Lead(**json.loads(r[0])) for r in rows]


def get_lead(lead_id: str) -> Lead | None:
    row = _conn.execute("SELECT data FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return Lead(**json.loads(row[0])) if row else None


def add_deal(lead_id: str) -> bool:
    """Returns False if a deal for this lead already exists."""
    with _lock, _conn:
        cur = _conn.execute(
            "INSERT OR IGNORE INTO deals (lead_id, created_at) VALUES (?, ?)",
            (lead_id, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        return cur.rowcount == 1


def list_deal_ids() -> list[str]:
    return [r[0] for r in _conn.execute("SELECT lead_id FROM deals ORDER BY created_at DESC")]


# --- mail bookkeeping -------------------------------------------------------

_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS sends (message_id TEXT PRIMARY KEY, deal_id TEXT NOT NULL, to_addr TEXT NOT NULL, at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS suppressed (email TEXT PRIMARY KEY, at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS seen_inbound (message_id TEXT PRIMARY KEY);
    """
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_send(message_id: str, deal_id: str, to_addr: str) -> None:
    with _lock, _conn:
        _conn.execute("INSERT INTO sends VALUES (?, ?, ?, ?)", (message_id, deal_id, to_addr.lower(), _now()))


def sends_today() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    return _conn.execute("SELECT COUNT(*) FROM sends WHERE at >= ?", (today,)).fetchone()[0]


def deal_for_message(message_ids: list[str]) -> str | None:
    for mid in message_ids:
        row = _conn.execute("SELECT deal_id FROM sends WHERE message_id = ?", (mid,)).fetchone()
        if row:
            return row[0]
    return None


def deal_for_address(addr: str) -> str | None:
    row = _conn.execute(
        "SELECT deal_id FROM sends WHERE to_addr = ? ORDER BY at DESC LIMIT 1", (addr.lower(),)
    ).fetchone()
    return row[0] if row else None


def suppress(email: str) -> None:
    with _lock, _conn:
        _conn.execute("INSERT OR IGNORE INTO suppressed VALUES (?, ?)", (email.lower(), _now()))


def is_suppressed(email: str) -> bool:
    return _conn.execute("SELECT 1 FROM suppressed WHERE email = ?", (email.lower(),)).fetchone() is not None


def mark_inbound_seen(message_id: str) -> bool:
    """Returns False if this inbound message was already processed."""
    with _lock, _conn:
        cur = _conn.execute("INSERT OR IGNORE INTO seen_inbound VALUES (?)", (message_id,))
        return cur.rowcount == 1


def is_inbound_seen(message_id: str) -> bool:
    return _conn.execute("SELECT 1 FROM seen_inbound WHERE message_id = ?", (message_id,)).fetchone() is not None


def last_send_at(deal_id: str) -> str | None:
    row = _conn.execute("SELECT MAX(at) FROM sends WHERE deal_id = ?", (deal_id,)).fetchone()
    return row[0] if row else None
