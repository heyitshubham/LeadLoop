"""The only way the rest of the app moves a deal. The board, the inbox poller and the
follow-up scheduler can all resume the same deal, so each deal has its own lock."""

import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from langgraph.types import Command

from app import store
from app.config import settings
from app.pipeline.graph import deal_graph

_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)


class NotWaiting(RuntimeError):
    pass


def _cfg(deal_id: str) -> dict:
    return {"configurable": {"thread_id": deal_id}}


def view(deal_id: str) -> dict | None:
    snap = deal_graph.get_state(_cfg(deal_id))
    if not snap.values:
        return None
    return {"id": deal_id, **snap.values, "pending_gate": snap.interrupts[0].value if snap.interrupts else None}


def _gate(deal_id: str) -> str | None:
    v = view(deal_id)
    return v["pending_gate"]["gate"] if v and v["pending_gate"] else None


def start(deal_id: str, lead: dict, autopilot: list[str]) -> None:
    with _locks[deal_id]:
        deal_graph.invoke({"lead": lead, "autopilot": autopilot, "audit": []}, _cfg(deal_id))


def decide(deal_id: str, decision: dict) -> None:
    with _locks[deal_id]:
        if _gate(deal_id) not in ("pitch", "reply"):
            raise NotWaiting("This deal is not waiting for your decision")
        deal_graph.invoke(Command(resume=decision), _cfg(deal_id))


def lead_replied(deal_id: str, text: str, message_id: str | None = None, subject: str = "") -> None:
    with _locks[deal_id]:
        if _gate(deal_id) != "lead":
            raise NotWaiting("This deal is not waiting for the lead")
        event = {"type": "reply", "text": text, "message_id": message_id, "subject": subject}
        deal_graph.invoke(Command(resume=event), _cfg(deal_id))


def nudge(deal_id: str) -> None:
    """Trigger a follow-up now (the scheduler calls this when one is due)."""
    with _locks[deal_id]:
        if _gate(deal_id) != "lead":
            raise NotWaiting("This deal is not waiting for the lead")
        deal_graph.invoke(Command(resume={"type": "followup"}), _cfg(deal_id))


def due_followups() -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.followup_days)
    due = []
    for deal_id in store.list_deal_ids():
        last = store.last_send_at(deal_id)
        if last and datetime.fromisoformat(last) < cutoff and _gate(deal_id) == "lead":
            due.append(deal_id)
    return due
