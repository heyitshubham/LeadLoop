"""One LangGraph thread per deal.

  research -> qualify -> sample -> draft_pitch -> [gate: pitch] -> send_pitch
          -> wait_for_lead <-------------------------------------------+
               | lead replied                | no reply (follow-up due)  |
               v                             v                           |
            negotiate                    follow_up                       |
               |  (unsubscribe -> lost)      |  (max reached -> lost)    |
               v                             v                           |
          [gate: reply] -> send_reply -> won / lost / back to waiting ---+
                                           |
                                          won -> fulfil -> [gate: delivery] -> send_delivery

Gates use `interrupt()`: the graph pauses, the board shows the draft, and the human's
decision resumes the thread from its checkpoint. Gates named in `autopilot` are skipped.
`research` lets the LLM search the web, news and Maps about the lead (see research.py) before
`qualify` reads the evidence. `fulfil` builds the dataset the lead bought (see fulfil.py) and the delivery gate shows it
with its cost and margin before the CSV goes out.
`wait_for_lead` is also an interrupt, resumed by the inbox poller, the follow-up
scheduler, or the board's "simulate reply" box in sandbox mode.
"""

import operator
import re
import smtplib
import sqlite3
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app import llm, store
from app.research import research as research_lead
from app.config import settings
from app.models import Assessment, Lead, Pitch, ReplyRead, Stage
from app.pipeline import fulfil as orders
from app.pipeline import mailer, pricing
from app.pipeline.sample import build_sample, to_csv
from app.serp.client import CreditBudgetExceeded, serp

MIN_FIT = 50


class DealState(TypedDict, total=False):
    lead: dict
    autopilot: list[str]
    stage: str
    research: dict  # {"notes", "searches", "by", "skipped"}: what the agent looked up before qualifying
    assessment: dict
    sample: list[dict]
    pitch: dict
    sent_to: str
    delivery: str
    send_error: str | None
    thread: Annotated[list[dict], operator.add]
    message_ids: Annotated[list[str], operator.add]
    event: str
    inbound: dict
    read: dict
    quote: dict | None
    quote_history: Annotated[list[dict], operator.add]  # every quote we sent, for the negotiation chart
    objections: int
    requested_rows: int | None
    followups: int
    draft: dict  # our next reply, waiting for the reply gate
    after_send: str  # wait | won | lost
    order: dict  # fulfilment report: rows, searches, cost, invoice, margin
    order_preview: list[dict]  # first rows of the delivered dataset (the full CSV lives on disk)
    delivery_email: dict  # {"subject", "body"} waiting for the delivery gate
    audit: Annotated[list[dict], operator.add]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")  # ms keeps same-second steps in order


def _log(actor: str, note: str) -> list[dict]:
    return [{"at": _now(), "actor": actor, "note": note}]


def _lead(state: DealState) -> Lead:
    return Lead(**state["lead"])


# --- prospecting --------------------------------------------------------------


def research(state: DealState) -> DealState:
    r = research_lead(_lead(state), serp)
    if r["skipped"]:
        note = f"research skipped: {r['skipped']}"
    else:
        live = sum(s["source"] == "live" for s in r["searches"])
        note = (f"researched the lead with {len(r['searches'])} search{'es' * (len(r['searches']) != 1)} "
                f"it chose ({live} live) via {r['by']}: {len(r['notes'])} finding{'s' * (len(r['notes']) != 1)}")
    return {"research": r, "audit": _log("agent", note)}


def _notes(state: DealState) -> list[str]:
    return (state.get("research") or {}).get("notes") or []


def qualify(state: DealState) -> DealState:
    a = llm.assess(_lead(state), _notes(state))
    stage = Stage.QUALIFIED.value if a.fit_score >= MIN_FIT else Stage.DISQUALIFIED.value
    return {
        "assessment": a.model_dump(),
        "stage": stage,
        "audit": _log("agent", f"fit {a.fit_score}/100 — {a.need_summary}"),
    }


def sample(state: DealState) -> DealState:
    a = Assessment(**state["assessment"])
    rows = build_sample(serp, a.sample_category, a.sample_city)
    return {
        "sample": rows,
        "stage": Stage.SAMPLE_BUILT.value,
        "audit": _log("agent", f"built {len(rows)}-row sample: {a.sample_category} in {a.sample_city}"),
    }


def draft_pitch(state: DealState) -> DealState:
    p = llm.write_pitch(_lead(state), Assessment(**state["assessment"]), state["sample"], _notes(state))
    return {
        "pitch": p.model_dump(),
        "stage": Stage.AWAITING_APPROVAL.value,
        "audit": _log("agent", f"drafted pitch: {p.subject}"),
    }


# --- human gates --------------------------------------------------------------


def _gate(state: DealState, gate: str, draft_key: str) -> dict[str, Any] | None:
    """Pauses for a human decision unless this gate is on autopilot (and the last send didn't fail)."""
    if gate in state.get("autopilot", []) and not state.get("send_error"):
        return None
    return interrupt(
        {
            "gate": gate,
            "lead": state["lead"]["name"],
            "draft": state[draft_key],
            "send_error": state.get("send_error"),
        }
    )


def approve_pitch(state: DealState) -> DealState:
    decision = _gate(state, "pitch", "pitch")
    if decision is None:
        return {"audit": _log("agent", "pitch auto-approved (autopilot)")}
    note = (decision.get("note") or "").strip()
    if decision["action"] == "reject":
        return {"stage": Stage.REJECTED.value, "audit": _log("human", f"rejected pitch. {note}".strip())}
    if decision["action"] == "edit":
        edited = Pitch(**decision["draft"])
        return {"pitch": edited.model_dump(), "audit": _log("human", f"edited and approved pitch. {note}".strip())}
    return {"audit": _log("human", f"approved pitch. {note}".strip())}


def approve_reply(state: DealState) -> DealState:
    decision = _gate(state, "reply", "draft")
    if decision is None:
        return {"audit": _log("agent", "reply auto-approved (autopilot)")}
    note = (decision.get("note") or "").strip()
    if decision["action"] == "reject":
        return {
            "after_send": "discard",
            "stage": Stage.PITCHED.value,
            "audit": _log("human", f"discarded the draft; waiting for the lead. {note}".strip()),
        }
    if decision["action"] == "edit":
        return {
            "draft": {**state["draft"], "body": decision["draft"]["body"]},
            "audit": _log("human", f"edited and approved reply. {note}".strip()),
        }
    return {"audit": _log("human", f"approved reply. {note}".strip())}


# --- sending ------------------------------------------------------------------


def _send(state: DealState, subject: str, body: str, attachment=None) -> tuple[DealState, bool]:
    lead = state["lead"]
    to = lead.get("email") or f"{lead['id']}@leads.local"
    ids = state.get("message_ids", [])
    try:
        delivery, mid = mailer.send(
            lead["id"], to, subject, body, attachment, in_reply_to=ids[-1] if ids else None, references=ids or None
        )
    except (mailer.SendBlocked, smtplib.SMTPException, OSError) as e:
        return {"send_error": str(e), "audit": _log("agent", f"send failed: {e}")}, False
    return {
        "sent_to": to,
        "delivery": delivery,
        "send_error": None,
        "message_ids": [mid],
        "thread": [{"dir": "out", "at": _now(), "subject": subject, "body": body}],
    }, True


def send_pitch(state: DealState) -> DealState:
    a = Assessment(**state["assessment"])
    filename = f"{a.sample_category}-{a.sample_city}-sample.csv".replace(" ", "-").lower()
    update, ok = _send(state, state["pitch"]["subject"], state["pitch"]["body"], (filename, to_csv(state["sample"])))
    if ok:
        update |= {"stage": Stage.PITCHED.value, "audit": _log("agent", f"sent pitch to {update['sent_to']} via {update['delivery']}")}
    return update


def send_reply(state: DealState) -> DealState:
    if state.get("after_send") == "discard":
        return {}
    subject = "Re: " + state["pitch"]["subject"]
    update, ok = _send(state, subject, state["draft"]["body"])
    if not ok:
        return update
    outcome = state.get("after_send", "wait")
    stage = {"won": Stage.WON.value, "lost": Stage.LOST.value}.get(outcome, Stage.PITCHED.value)
    note = {"won": "deal closed — won", "lost": "closed — lead not interested for now"}.get(outcome, "sent reply")
    update |= {"stage": stage, "audit": _log("agent", f"{note} (via {update['delivery']})")}
    if state["draft"].get("kind") == "followup":
        update["followups"] = state.get("followups", 0) + 1
    return update


# --- fulfilment ---------------------------------------------------------------


def _presale_searches(state: DealState) -> dict[str, int]:
    """Searches spent before the sale: the agent's research, plus one Maps page for the sample."""
    out: dict[str, int] = {}
    for s in (state.get("research") or {}).get("searches", []):
        out[s["engine"]] = out.get(s["engine"], 0) + 1
    if state.get("sample"):
        out["google_maps"] = out.get("google_maps", 0) + 1
    return out


def fulfil(state: DealState) -> DealState:
    a = Assessment(**state["assessment"])
    lead = state["lead"]
    q = state["quote"]
    try:
        rows, report = orders.build_order(serp, a.sample_category, a.sample_city, lead.get("market"), q,
                                          presale=_presale_searches(state))
    except CreditBudgetExceeded as e:
        return {"send_error": str(e), "audit": _log("agent", f"could not build the order: {e}")}
    if not rows:
        return {"send_error": "no rows found for this order", "audit": _log("agent", "found no rows for the order; nothing to deliver")}
    orders.save(lead["id"], rows)
    cur = report["currency"]
    shortfall = (
        f"\n\nWe found {report['delivered']} of the {report['ordered']} rows you ordered, so the invoice is "
        f"{pricing.money(report['invoice'], cur)} instead of {pricing.money(q['price'], cur)}."
        if report["delivered"] < report["ordered"] else ""
    )
    body = (
        f"Hi {lead['name']} team,\n\nThanks for the order. Your dataset is attached: {report['delivered']} "
        f"{a.sample_category} across {', '.join(report['cities'])}, best rows first.\n\n"
        f"Each row has name, address, phone, website, rating, review count, opening status and map "
        f"coordinates, plus a quality score. The top {report['enriched']} rows also show what customers "
        f"mention in reviews and whether the business is hiring right now.\n\n"
        f"Phone numbers are present on {report['completeness']['phone']}% of rows and websites on "
        f"{report['completeness']['website']}%. Everything comes from public search listings."
        f"{shortfall}\n\nInvoice: {pricing.money(report['invoice'], cur)}.\n\n— {settings.signature}"
    )
    return {
        "order": report,
        "order_preview": rows[:10],
        "delivery_email": {"subject": f"Your {a.sample_category} dataset ({report['delivered']} rows)", "body": body},
        "send_error": None,
        "audit": _log(
            "agent",
            f"built the order: {report['delivered']}/{report['ordered']} rows from {report['searches_total']} searches "
            f"({report['live_credits']} live credits), SerpApi cost {pricing.money(report['serp_cost'], cur)}, "
            f"invoice {pricing.money(report['invoice'], cur)}, margin {report['margin_pct']}%",
        ),
    }


def approve_delivery(state: DealState) -> DealState:
    decision = _gate(state, "delivery", "delivery_email")
    if decision is None:
        return {"audit": _log("agent", "delivery auto-approved (autopilot)")}
    note = (decision.get("note") or "").strip()
    if decision["action"] == "reject":
        return {"after_send": "hold", "audit": _log("human", f"held the delivery. {note}".strip())}
    if decision["action"] == "edit":
        return {
            "delivery_email": {**state["delivery_email"], **{k: v for k, v in decision["draft"].items() if k in ("subject", "body")}},
            "after_send": "deliver",
            "audit": _log("human", f"edited and approved the delivery. {note}".strip()),
        }
    return {"after_send": "deliver", "audit": _log("human", f"approved the delivery. {note}".strip())}


def send_delivery(state: DealState) -> DealState:
    if state.get("after_send") == "hold":
        return {}
    lead = state["lead"]
    a = Assessment(**state["assessment"])
    filename = re.sub(r"[^a-z0-9]+", "-", f"{a.sample_category} {state['order']['delivered']} rows".lower()).strip("-") + ".csv"
    update, ok = _send(state, state["delivery_email"]["subject"], state["delivery_email"]["body"],
                       (filename, orders.load(lead["id"]) or ""))
    if ok:
        update |= {"stage": Stage.DELIVERED.value,
                   "audit": _log("agent", f"delivered {state['order']['delivered']} rows to {update['sent_to']}")}
    return update


# --- the lead's side ----------------------------------------------------------


def wait_for_lead(state: DealState) -> DealState:
    event: dict[str, Any] = interrupt(
        {"gate": "lead", "lead": state["lead"]["name"], "followups": state.get("followups", 0)}
    )
    if event.get("type") == "followup":
        return {"event": "followup"}
    return {
        "event": "reply",
        "inbound": event,
        "thread": [{"dir": "in", "at": _now(), "subject": event.get("subject", ""), "body": event["text"]}],
        "message_ids": [event["message_id"]] if event.get("message_id") else [],
        "audit": _log("lead", f"replied: {event['text'].strip().splitlines()[0][:120]}"),
    }


def negotiate(state: DealState) -> DealState:
    lead = _lead(state)
    read: ReplyRead = llm.read_reply(lead, state.get("thread", [])[:-1], state["inbound"]["text"])

    if read.intent == "unsubscribe":
        if state.get("sent_to"):
            store.suppress(state["sent_to"])
        return {
            "read": read.model_dump(),
            "stage": Stage.LOST.value,
            "audit": _log("agent", "lead opted out — address suppressed, no further emails"),
        }

    objections = state.get("objections", 0) + (read.intent == "price_objection")
    rows = read.requested_rows or state.get("requested_rows")
    q = None
    if read.intent == "accept" and state.get("quote"):
        q = state["quote"]  # they accepted what we last offered; never re-price on acceptance
    elif read.intent != "not_now":
        q = pricing.quote(rows, objections, lead.currency).model_dump()

    draft = llm.write_reply(lead, state.get("thread", []), read, q)
    after = {"accept": "won", "not_now": "lost"}.get(read.intent, "wait")
    quote_note = f" — quote {q['rows']} rows at {pricing.money(q['price'], q['currency'])} ({q['note']})" if q else ""
    return {
        "read": read.model_dump(),
        "quote": q,
        "quote_history": [{**q, "intent": read.intent, "at": _now()}] if q else [],
        "objections": objections,
        "requested_rows": rows,
        "draft": {"body": draft.body, "kind": "reply"},
        "after_send": after,
        "stage": Stage.NEGOTIATING.value,
        "audit": _log("agent", f"read reply as '{read.intent}'{quote_note}; drafted answer"),
    }


def follow_up(state: DealState) -> DealState:
    n = state.get("followups", 0)
    if n >= settings.max_followups:
        return {"stage": Stage.LOST.value, "audit": _log("agent", f"no reply after {n} follow-ups — closed")}
    a = Assessment(**state["assessment"])
    lead = state["lead"]["name"]
    body = (
        f"Hi {lead} team,\n\nJust bumping this up in case it got buried — the free sample of "
        f"{a.sample_category} in {a.sample_city} is attached to my first email. Would the full list help?"
        if n == 0
        else f"Hi {lead} team,\n\nLast note from me on this. If {a.sample_category} data for "
        f"{a.sample_city} isn't a priority right now, no worries at all."
    ) + f"\n\n— {settings.signature}"
    return {
        "draft": {"body": body, "kind": "followup"},
        "after_send": "wait",
        "stage": Stage.NEGOTIATING.value,
        "audit": _log("agent", f"no reply — drafted follow-up #{n + 1}"),
    }


# --- routing ------------------------------------------------------------------


def _after_qualify(state: DealState) -> str:
    return END if state["stage"] == Stage.DISQUALIFIED.value else "sample"


def _after_pitch_gate(state: DealState) -> str:
    return END if state["stage"] == Stage.REJECTED.value else "send_pitch"


def _after_send_pitch(state: DealState) -> str:
    return "approve_pitch" if state.get("send_error") else "wait_for_lead"


def _after_wait(state: DealState) -> str:
    return "follow_up" if state["event"] == "followup" else "negotiate"


def _after_draft(state: DealState) -> str:
    return END if state["stage"] == Stage.LOST.value else "approve_reply"


def _after_send_reply(state: DealState) -> str:
    if state.get("send_error"):
        return "approve_reply"
    if state["stage"] == Stage.WON.value:
        return "fulfil"
    return END if state["stage"] == Stage.LOST.value else "wait_for_lead"


def _after_fulfil(state: DealState) -> str:
    return END if state.get("send_error") else "approve_delivery"  # out of credits: stays won, nothing sent


def _after_send_delivery(state: DealState) -> str:
    return "approve_delivery" if state.get("send_error") else END


def build_graph(checkpointer):
    g = StateGraph(DealState)
    for name, fn in [
        ("research", research),
        ("qualify", qualify),
        ("sample", sample),
        ("draft_pitch", draft_pitch),
        ("approve_pitch", approve_pitch),
        ("send_pitch", send_pitch),
        ("wait_for_lead", wait_for_lead),
        ("negotiate", negotiate),
        ("follow_up", follow_up),
        ("approve_reply", approve_reply),
        ("send_reply", send_reply),
        ("fulfil", fulfil),
        ("approve_delivery", approve_delivery),
        ("send_delivery", send_delivery),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "research")
    g.add_edge("research", "qualify")
    g.add_conditional_edges("qualify", _after_qualify, ["sample", END])
    g.add_edge("sample", "draft_pitch")
    g.add_edge("draft_pitch", "approve_pitch")
    g.add_conditional_edges("approve_pitch", _after_pitch_gate, ["send_pitch", END])
    g.add_conditional_edges("send_pitch", _after_send_pitch, ["approve_pitch", "wait_for_lead"])
    g.add_conditional_edges("wait_for_lead", _after_wait, ["follow_up", "negotiate"])
    g.add_conditional_edges("negotiate", _after_draft, ["approve_reply", END])
    g.add_conditional_edges("follow_up", _after_draft, ["approve_reply", END])
    g.add_edge("approve_reply", "send_reply")
    g.add_conditional_edges("send_reply", _after_send_reply, ["approve_reply", "wait_for_lead", "fulfil", END])
    g.add_conditional_edges("fulfil", _after_fulfil, ["approve_delivery", END])
    g.add_edge("approve_delivery", "send_delivery")
    g.add_conditional_edges("send_delivery", _after_send_delivery, ["approve_delivery", END])
    return g.compile(checkpointer=checkpointer)


_conn = sqlite3.connect(settings.data_dir / "checkpoints.db", check_same_thread=False)
deal_graph = build_graph(SqliteSaver(_conn))
