import json
import logging
import os
import queue
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from app import stats, store
from app.config import settings
from app.discovery.service import discover
from app.pipeline import inbox, mailer, runner
from app.pipeline.sample import to_csv
from app.serp.client import CreditBudgetExceeded, serp

# Make app warnings (LLM fallbacks, send failures) visible in the server log.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("leadloop")
POLL_SECONDS = 60


def _background_loop(stop: threading.Event) -> None:
    """Every minute: pull replies from the live inbox, then draft any follow-ups that are due."""
    while not stop.wait(POLL_SECONDS):
        try:
            if n := inbox.poll():
                log.info("routed %d replies", n)
            for deal_id in runner.due_followups():
                runner.nudge(deal_id)
        except Exception:  # keep the loop alive; the next tick retries
            log.exception("background loop failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    stop = threading.Event()
    if os.getenv("LEADLOOP_SCHEDULER", "1") == "1":
        threading.Thread(target=_background_loop, args=(stop,), daemon=True).start()
    yield
    stop.set()


app = FastAPI(title="LeadLoop", lifespan=lifespan)
BOARD = Path(__file__).parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def board():
    return FileResponse(BOARD)


def _deal(deal_id: str) -> dict:
    v = runner.view(deal_id)
    if not v:
        raise HTTPException(404, "No deal for this lead")
    return v


@app.get("/api/health")
def health():
    return {
        "serp_mode": serp.mode,
        "llm_mode": " → ".join(p.upper() for p in settings.llm_providers) or "DEMO",
        "credits_used": serp.credits_used(),
        "credit_budget": settings.credit_budget,
        "send_mode": settings.send_mode,
        "reads_inbox": settings.reads_inbox,
        "sender": settings.sender,
        "sends_today": store.sends_today(),
        "daily_send_cap": settings.daily_send_cap,
        "markets": [m["name"] for m in settings.markets],
        "pricing": {**settings.pricing, "max_discount_pct": settings.max_discount_pct},
    }


@app.get("/api/stats")
def insights():
    return stats.compute(store.list_leads(), [v for i in store.list_deal_ids() if (v := runner.view(i))])


@app.get("/api/mail/check")
def mail_check():
    """Logs in to SMTP and IMAP without sending anything."""
    return mailer.check_connection()


@app.post("/api/discover")
def run_discovery(min_score: int = 40):
    try:
        leads = discover(serp, min_score=min_score)
    except CreditBudgetExceeded as e:
        raise HTTPException(429, str(e))
    store.save_leads(leads)
    return leads


@app.get("/api/discover/stream")
def discover_stream(min_score: int = 40):
    """Runs discovery and streams each step (Server-Sent Events) so the board can show the agent's work."""
    events: queue.Queue = queue.Queue()

    def work():
        t0 = time.perf_counter()
        used_before = serp.credits_used()
        try:
            leads = discover(serp, min_score=min_score, emit=events.put)
            store.save_leads(leads)
            events.put({"type": "done", "count": len(leads), "ms": round((time.perf_counter() - t0) * 1000),
                        "credits_spent": serp.credits_used() - used_before, "serp_mode": serp.mode,
                        "top": [{"name": l.name, "score": l.intent_score} for l in leads[:3]]})
        except CreditBudgetExceeded as e:
            events.put({"type": "error", "message": str(e)})
        except Exception as e:  # surface failures in the panel instead of a silent stall
            log.exception("discovery failed")
            events.put({"type": "error", "message": f"Discovery failed: {e}"})
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream():
        while (event := events.get()) is not None:
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/leads")
def leads():
    return store.list_leads()


class StartDeal(BaseModel):
    autopilot: list[Literal["pitch", "reply"]] = []
    email: str | None = Field(default=None, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.post("/api/leads/{lead_id}/deal")
def start_deal(lead_id: str, body: StartDeal):
    lead = store.get_lead(lead_id)
    if not lead:
        raise HTTPException(404, "Unknown lead")
    if body.email:
        lead.email = body.email.strip()
    if settings.live_mail and not lead.email:
        raise HTTPException(422, "Live mode: add the lead's email address before starting the deal")
    if not store.add_deal(lead_id):
        raise HTTPException(409, "Deal already started")
    runner.start(lead_id, lead.model_dump(), body.autopilot)
    return _deal(lead_id)


@app.get("/api/deals")
def deals():
    return [v for i in store.list_deal_ids() if (v := runner.view(i))]


@app.get("/api/deals/{deal_id}")
def deal(deal_id: str):
    return _deal(deal_id)


class Decision(BaseModel):
    action: Literal["approve", "edit", "reject"]
    draft: dict | None = None  # {"subject", "body"} for a pitch, {"body"} for a reply
    note: str | None = None


@app.post("/api/deals/{deal_id}/decision")
def decide(deal_id: str, decision: Decision):
    _deal(deal_id)
    if decision.action == "edit" and not (decision.draft and decision.draft.get("body")):
        raise HTTPException(422, "An edit needs the edited draft")
    try:
        runner.decide(deal_id, decision.model_dump())
    except runner.NotWaiting as e:
        raise HTTPException(409, str(e))
    return _deal(deal_id)


class SimulatedReply(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.post("/api/deals/{deal_id}/simulate-reply")
def simulate_reply(deal_id: str, body: SimulatedReply):
    """Record the lead's reply by hand: role-play in sandbox, or paste it from your inbox in live
    mode when IMAP isn't available."""
    if settings.reads_inbox:
        raise HTTPException(403, "Replies are read from the inbox automatically")
    _deal(deal_id)
    try:
        runner.lead_replied(deal_id, body.text)
    except runner.NotWaiting as e:
        raise HTTPException(409, str(e))
    return _deal(deal_id)


@app.post("/api/deals/{deal_id}/nudge")
def nudge(deal_id: str):
    """Draft the next follow-up now instead of waiting for it to fall due."""
    _deal(deal_id)
    try:
        runner.nudge(deal_id)
    except runner.NotWaiting as e:
        raise HTTPException(409, str(e))
    return _deal(deal_id)


@app.post("/api/inbox/poll")
def poll_inbox():
    if not settings.reads_inbox:
        raise HTTPException(400, "No IMAP inbox configured: record replies with simulate-reply")
    return {"routed": inbox.poll()}


@app.get("/api/deals/{deal_id}/sample.csv", response_class=PlainTextResponse)
def sample_csv(deal_id: str):
    return to_csv(_deal(deal_id).get("sample", []))
