"""End-to-end DEMO runs: discover -> pitch -> gate -> lead replies -> negotiate -> close.

Fully offline: LEADLOOP_DEMO=1 forces fixtures and template LLM output, sandbox mail
points at a closed port so messages land in the outbox folder, and the background
scheduler is off so tests drive every step.
"""

import os
import tempfile

os.environ.update(
    LEADLOOP_DEMO="1",
    LEADLOOP_DATA_DIR=tempfile.mkdtemp(),
    LEADLOOP_SEND_MODE="sandbox",
    SANDBOX_SMTP_PORT="1",
    LEADLOOP_SCHEDULER="0",
    PRICE_PER_ROW_INR="4",
    MIN_ORDER_INR="1500",
    DEFAULT_ROWS="500",
    DISCOUNT_STEP_PCT="10",
    MAX_DISCOUNT_PCT="20",
    MAX_FOLLOWUPS="2",
    LEADLOOP_MARKETS="India,United States",
    PRICE_PER_ROW_USD="0.10",
    MIN_ORDER_USD="49",
    DEFAULT_CURRENCY="USD",
)

from fastapi.testclient import TestClient  # noqa: E402

from app import store  # noqa: E402
from app.discovery.scoring import score_signal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Signal  # noqa: E402
from app.pipeline.inbox import clean_body  # noqa: E402

client = TestClient(app)


def _lead_id(name: str) -> str:
    client.post("/api/discover")  # leads already in a deal are not rediscovered, but stay stored
    return next(l.id for l in store.list_leads() if l.name == name)


def _pitched(name: str, email: str | None = None) -> str:
    """Starts a deal, approves the pitch, returns the deal id (now waiting on the lead)."""
    lead_id = _lead_id(name)
    client.post(f"/api/leads/{lead_id}/deal", json={"email": email} if email else {})
    deal = client.post(f"/api/deals/{lead_id}/decision", json={"action": "approve"}).json()
    assert deal["pending_gate"]["gate"] == "lead"
    return lead_id


def _reply(deal_id: str, text: str) -> dict:
    return client.post(f"/api/deals/{deal_id}/simulate-reply", json={"text": text}).json()


def _approve(deal_id: str) -> dict:
    return client.post(f"/api/deals/{deal_id}/decision", json={"action": "approve"}).json()


# --- discovery ----------------------------------------------------------------


def test_scoring_explains_itself():
    score, reasons = score_signal(Signal(source="reddit", title="Need a list of gyms in Mumbai", posted="1 day ago"))
    assert score == 45 + 10 + 15
    assert "asks for: need a list" in reasons
    assert "posted 1 day ago" in reasons


def test_discovery_ranks_and_filters():
    leads = client.post("/api/discover").json()
    names = [l["name"] for l in leads]
    assert "Quantive Labs" not in names  # irrelevant, stale job posting
    assert "SalonSuite" in names  # funded 6 days ago: fresh budget is a buying signal
    salon = next(l for l in leads if l["name"] == "SalonSuite")
    assert any("raised funding" in e for e in salon["evidence"])
    brewkart = next(l for l in leads if l["name"] == "Brewkart Cafe Supplies")
    assert len(brewkart["signals"]) == 2  # job post + funding news merged
    assert leads == sorted(leads, key=lambda l: -l["intent_score"])


# --- pitch gate ---------------------------------------------------------------


def test_pitch_waits_for_approval_and_sends_the_edit():
    lead_id = _lead_id("Nestora Realty")
    deal = client.post(f"/api/leads/{lead_id}/deal", json={"email": "ops@nestora.example"}).json()
    assert deal["stage"] == "awaiting_approval"
    assert deal["assessment"]["sample_city"] == "Pune"
    assert len(deal["sample"]) == 10
    assert deal["pending_gate"]["gate"] == "pitch"
    assert client.post(f"/api/leads/{lead_id}/deal", json={}).status_code == 409

    edited = {"subject": "Pune agency data", "body": "Hi, sample attached."}
    deal = client.post(f"/api/deals/{lead_id}/decision", json={"action": "edit", "draft": edited}).json()
    assert deal["stage"] == "pitched"
    assert deal["pitch"] == edited
    assert deal["sent_to"] == "ops@nestora.example"
    assert deal["delivery"] == "outbox"
    assert deal["pending_gate"]["gate"] == "lead"
    assert [m["dir"] for m in deal["thread"]] == ["out"]
    assert client.post(f"/api/deals/{lead_id}/decision", json={"action": "approve"}).status_code == 409


def test_rejecting_the_pitch_ends_the_deal():
    lead_id = _lead_id("MediLink Health")
    client.post(f"/api/leads/{lead_id}/deal", json={})
    deal = client.post(f"/api/deals/{lead_id}/decision", json={"action": "reject", "note": "not now"}).json()
    assert deal["stage"] == "rejected"
    assert "sent_to" not in deal


# --- negotiation --------------------------------------------------------------


def test_negotiation_never_goes_below_the_floor_then_closes():
    deal_id = _pitched("Brewkart Cafe Supplies", "buy@brewkart.example")

    deal = _reply(deal_id, "Interesting. How much for 1,000 rows across Bengaluru?")
    assert deal["read"]["intent"] == "interested"
    assert deal["quote"] == {"currency": "INR", "rows": 1000, "list_price": 4000, "discount_pct": 0, "price": 4000, "note": "list price"}
    assert deal["pending_gate"]["gate"] == "reply"
    assert "₹4,000" in deal["draft"]["body"]
    deal = _approve(deal_id)
    assert deal["pending_gate"]["gate"] == "lead"

    prices = []
    for _ in range(4):
        _reply(deal_id, "That's too expensive for our budget, can you do better?")
        deal = _approve(deal_id)
        prices.append((deal["quote"]["rows"], deal["quote"]["price"]))
    # 10% off, 20% off (max), then a smaller scope at the max discount — never lower.
    assert prices == [(1000, 3600), (1000, 3200), (500, 1600), (500, 1600)]

    deal = _reply(deal_id, "OK deal, please send the invoice.")
    assert deal["read"]["intent"] == "accept"
    assert deal["quote"]["price"] == 1600  # acceptance never re-prices
    deal = _approve(deal_id)
    assert deal["stage"] == "won"
    assert deal["pending_gate"]["gate"] == "delivery"  # the order is built and waits for you
    out = [m for m in deal["thread"] if m["dir"] == "out"]
    assert all(m["subject"].startswith("Re: ") for m in out[1:])


def test_won_deal_builds_enriches_and_delivers_the_order():
    deal_id = next(d["id"] for d in client.get("/api/deals").json() if d["lead"]["name"] == "Brewkart Cafe Supplies")
    deal = client.get(f"/api/deals/{deal_id}").json()
    order = deal["order"]
    # Fixtures hold 12 businesses however many pages are asked for: duplicates are dropped, not padded.
    assert order["ordered"] == 500 and order["delivered"] == 12 and order["duplicates_dropped"] > 0
    assert order["invoice"] == round(1600 * 12 / 500) and order["invoice_note"].startswith("pro-rata")
    assert order["searches"]["google_maps_reviews"] == order["enriched"] == 10
    assert order["searches"]["google_jobs"] == 10 and order["live_credits"] == 0
    assert order["searches_total"] <= 50 + order["presale_searches"] and order["serp_cost"] > 0
    assert order["presale_searches"] == 1  # the sample's Maps page; research is skipped without an LLM
    assert order["margin_pct"] is not None
    assert [r["quality"] for r in deal["order_preview"]] == sorted((r["quality"] for r in deal["order_preview"]), reverse=True)
    assert deal["order_preview"][0]["customers_mention"].startswith("site visits")
    assert deal["order_preview"][0]["hiring_now"] == "no"  # other employers' postings don't count
    assert "12 of the 500 rows" in deal["delivery_email"]["body"]

    csv_text = client.get(f"/api/deals/{deal_id}/dataset.csv").text
    assert csv_text.splitlines()[0].startswith("name,type,address,city") and len(csv_text.splitlines()) == 13

    deal = _approve(deal_id)
    assert deal["stage"] == "delivered" and deal["pending_gate"] is None
    assert deal["thread"][-1]["subject"] == deal["delivery_email"]["subject"]
    assert client.get("/api/deals/nobody/dataset.csv").status_code == 404


def test_quality_prefers_reachable_rows():
    from app.pipeline.fulfil import quality

    full = {"phone": "1", "website": "w", "address": "a", "rating": 4.1, "reviews": 30, "open_state": "Open"}
    assert quality(full) == 100
    assert quality({**full, "phone": None}) == 70
    assert quality({}) == 0


def test_editing_a_reply_sends_the_human_version():
    deal_id = _pitched("r/delhi poster")
    _reply(deal_id, "What does each row include?")
    deal = client.post(
        f"/api/deals/{deal_id}/decision", json={"action": "edit", "draft": {"body": "Name, phone, rating. ₹2,000."}}
    ).json()
    assert deal["thread"][-1]["body"] == "Name, phone, rating. ₹2,000."
    assert deal["audit"][-2]["actor"] == "human"


def test_unsubscribe_suppresses_the_address():
    deal_id = _pitched("r/mumbai poster", "owner@gymapp.example")
    deal = _reply(deal_id, "Please stop emailing me.")
    assert deal["stage"] == "lost"
    assert deal["pending_gate"] is None
    assert store.is_suppressed("owner@gymapp.example")
    assert [m["dir"] for m in deal["thread"]] == ["out", "in"]  # we did not answer


def test_followups_then_close():
    from app.pipeline import runner

    lead = store.list_leads()[0].model_copy(update={"id": "followup-lead", "name": "Followup Co"})
    store.save_leads([lead])
    store.add_deal(lead.id)
    runner.start(lead.id, lead.model_dump(), autopilot=["pitch", "reply"])  # fully automatic
    assert runner.view(lead.id)["pending_gate"]["gate"] == "lead"

    for expected in (1, 2):
        deal = client.post(f"/api/deals/{lead.id}/nudge").json()
        assert deal["followups"] == expected
        assert deal["pending_gate"]["gate"] == "lead"
    deal = client.post(f"/api/deals/{lead.id}/nudge").json()
    assert deal["stage"] == "lost"
    assert deal["pending_gate"] is None


def test_sandbox_rejects_bad_email_and_live_only_endpoints():
    lead_id = _lead_id("r/delhi poster")
    assert client.post(f"/api/leads/{lead_id}/deal", json={"email": "not-an-email"}).status_code == 422
    assert client.post("/api/inbox/poll").status_code == 400


def test_clean_body_drops_quoted_thread():
    text = "Sounds good, 2000 rows please.\n\nOn Mon, 5 Oct 2026 at 10:00, Desk <d@x.in> wrote:\n> old pitch"
    assert clean_body(text) == "Sounds good, 2000 rows please."


def test_stats_reflect_the_pipeline():
    s = client.get("/api/stats").json()
    funnel = {f["stage"]: f["count"] for f in s["funnel"]}
    assert funnel["Discovered"] >= funnel["Qualified"] >= funnel["Pitched"] >= funnel["Replied"] >= funnel["Won"] >= 1
    brewkart = next(n for n in s["negotiations"] if n["deal"] == "Brewkart Cafe Supplies")
    assert [p["price"] for p in brewkart["points"]] == [4000, 3600, 3200, 1600, 1600, 1600]
    assert "USD" not in s["economics"] or "INR" in s["economics"]  # never mixed with USD
    assert brewkart["currency"] == "INR" and brewkart["points"][0]["pct"] == 100
    assert sum(b["count"] for b in s["intent_bins"]) == s["kpis"]["leads"]
    assert set(s["flow"]) == {"discovered", "pitch_approval", "waiting_on_lead", "reply_approval",
                              "delivery_approval", "won", "delivered", "closed"}
    brew_order = next(o for o in s["orders"] if o["deal"] == "Brewkart Cafe Supplies")
    assert brew_order["invoice"] == 38 and s["economics"]["INR"]["invoiced"] >= 38
    assert s["kpis"]["revenue_won"]["INR"] >= 38  # what was invoiced, not the 1,600 quoted for 500 rows
    assert len(s["activity"]) <= 12


def test_discovery_streams_every_step():
    import json as _json

    body = client.get("/api/discover/stream").text
    events = [_json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    steps = [e for e in events if e["type"] == "step"]
    ids = list(dict.fromkeys(e["id"] for e in steps))
    assert ids == ["plan", "jobs-in-0", "jobs-in-1", "jobs-in-2", "jobs-us-0", "jobs-us-1", "jobs-us-2",
                   "reddit", "news-in", "news-us", "merge", "history", "score", "vet"]
    # every search goes running -> done, and says where the data came from
    for sid in ("jobs-in-0", "jobs-us-0", "reddit", "news-in"):
        statuses = [e["status"] for e in steps if e["id"] == sid]
        assert statuses == ["running", "done"]
        done = next(e for e in steps if e["id"] == sid and e["status"] == "done")
        assert done["source"] in ("demo", "cache", "live") and done["items"]
    worked = next(e for e in steps if e["id"] == "history" and e["status"] == "done")["items"]
    assert worked  # earlier tests started deals; those leads are skipped, not rediscovered
    assert events[-1]["type"] == "done" and events[-1]["count"] + len(worked) == 6
    assert events[-1]["credits_spent"] == 0
    score = next(e for e in steps if e["id"] == "score" and e["status"] == "done")
    assert any(i.startswith("✗ Quantive Labs") for i in score["items"])


def test_company_names_are_read_from_headlines():
    from app.llm import _demo_company, extract_companies

    assert _demo_company("Bengaluru D2C brand Brewkart Cafe Supplies raises ₹12 crore seed round") == "Brewkart Cafe Supplies"
    assert _demo_company("SalonSuite raises pre-seed to digitise salons in Chennai") == "SalonSuite"
    assert _demo_company("Zepto-backed Quickly Labs secures $2M from Peak XV") == "Quickly Labs"
    assert _demo_company("5 Indian startups that raised funding this week") is None
    assert extract_companies([]) == []


def test_news_clusters_are_flattened_and_named(monkeypatch):
    from app.discovery import signals
    from app.serp.client import serp

    clustered = {"news_results": [
        {"title": "Fintech Paysy raises ₹30 crore in Series A", "link": "https://x.example/a"},
        {"highlight": {"title": "Funding roundup"}, "stories": [
            {"title": "Agritech startup KhetBuddy bags $1M seed", "link": "https://x.example/b"},
            {"title": "10 startups that raised money this week", "link": "https://x.example/c"},
        ]},
    ]}
    monkeypatch.setattr(serp, "fetch", lambda engine, **p: (clustered, "live"))
    events = []
    sigs = signals._news_for(serp, events.append, {"name": "India", "gl": "in", "currency": "INR"})
    assert [s.company for s in sigs] == ["Paysy", "KhetBuddy"]
    assert {s.market for s in sigs} == {"India"}
    extract = [e for e in events if e["id"] == "news-extract-in" and e["status"] == "done"][0]
    assert extract["summary"].startswith("2 of 3 headlines name a single company")
    assert "pattern rule" in extract["summary"]  # DEMO mode: the panel says the AI didn't do this


def test_usd_pricing_and_currency_per_lead():
    from app.pipeline.pricing import money, quote

    assert [quote(1000, o, "USD").price for o in range(4)] == [100, 90, 80, 40]
    assert quote(None, 0, "USD").list_price == 50  # 500 default rows x $0.10, above the $49 minimum
    assert money(1234567, "INR") == "₹12,34,567" and money(1500, "USD") == "$1,500"

    client.post("/api/discover")
    leads = {l.name: l for l in store.list_leads()}
    assert leads["Nestora Realty"].market == "India" and leads["Nestora Realty"].currency == "INR"
    assert leads["r/delhi poster"].market is None and leads["r/delhi poster"].currency == "USD"


def test_buyer_check_drops_job_boards_and_competitors():
    from app.discovery.service import company_key
    from app.llm import _demo_vet

    assert _demo_vet("remote quest jobs")[0] == "job_board"
    assert _demo_vet("HireGrid")[0] == "job_board"
    assert _demo_vet("SenseGrid Market Research")[0] == "data_vendor"
    assert _demo_vet("Nestora Realty")[0] == "unclear"  # without the AI, nobody is called a buyer
    assert company_key("Hive Business Solution Pvt. Ltd.") == company_key("Hive Business Solution")


def test_dates_with_and_without_timezones():
    from datetime import datetime, timedelta, timezone

    from app.discovery.scoring import days_ago

    two_days = datetime.now(timezone.utc) - timedelta(days=2)
    assert round(days_ago(two_days.isoformat())) == 2
    assert round(days_ago(two_days.replace(tzinfo=None).isoformat())) == 2  # no timezone: treated as UTC
    assert round(days_ago(two_days.strftime("%Y-%m-%dT%H:%M:%SZ"))) == 2
    assert days_ago("12 days ago") == 12 and days_ago("2 weeks ago") == 14
    assert days_ago("10/03/2026, 07:00 AM, +0000 UTC") is None and days_ago(None) is None


def test_strict_schema_is_flat_and_closed():
    from app.llm import CompanyVerdicts, HeadlineCompanies, strict_schema
    from app.models import ReplyRead

    def walk(node):
        if isinstance(node, dict):
            assert "$ref" not in node and "default" not in node
            if node.get("type") == "object" and "properties" in node:
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for model in (CompanyVerdicts, HeadlineCompanies, ReplyRead):
        schema = strict_schema(model)
        assert "$defs" not in schema
        walk(schema)


def test_provider_chain_falls_through(monkeypatch):
    from app import llm
    from app.models import EmailDraft

    monkeypatch.setattr(llm, "_groq", object())
    monkeypatch.setattr(llm, "_gemini", object())
    monkeypatch.setattr(llm, "_parse_groq", lambda p, s, effort="low": (None, "Groq rate limit on openai/gpt-oss-120b"))
    monkeypatch.setattr(llm, "_parse_gemini", lambda p, s: (EmailDraft(body="hi"), None))
    assert llm._parse("x", EmailDraft).body == "hi"
    assert llm.last_provider == "Gemini" and llm.last_fallback is None

    monkeypatch.setattr(llm, "_parse_gemini", lambda p, s: (None, "Gemini free-tier daily quota used up"))
    assert llm._parse("x", EmailDraft) is None
    assert llm.last_fallback == "Groq rate limit on openai/gpt-oss-120b; Gemini free-tier daily quota used up"


def test_batches_reask_skipped_items_and_never_default_to_buyer(monkeypatch):
    from app import llm

    calls = []

    def fake_parse(prompt, schema, effort="low"):
        n = sum(1 for line in prompt.splitlines() if line[:1].isdigit() and ". " in line)
        calls.append(n)
        # First pass: answer only the first item of each batch; re-ask: answer everything.
        answer = range(n) if len(calls) > 2 else range(1)
        llm.last_provider = "Groq"
        return llm.CompanyVerdicts(items=[llm.CompanyVerdict(index=i, kind="buyer", reason="ok") for i in answer])

    monkeypatch.setattr(llm, "_parse", fake_parse)
    cands = [{"name": f"Co {i}", "evidence": "x"} for i in range(20)]
    verdicts = llm.vet_companies(cands)
    assert calls[:2] == [15, 5]  # batches of 15
    assert all(k == "buyer" for k, _ in verdicts) and llm.last_unanswered == 0

    monkeypatch.setattr(llm, "_parse", lambda p, s, effort="low": llm.CompanyVerdicts(items=[]))
    llm.last_fallback = None
    verdicts = llm.vet_companies(cands[:3])
    assert [k for k, _ in verdicts] == ["unclear"] * 3  # nothing answered: rules, never "buyer"


def test_reply_rates_feed_back_into_scoring():
    from app.learning import source_performance

    def deal(source, replied, stage="pitched"):
        thread = [{"dir": "out"}] + ([{"dir": "in"}] if replied else [])
        return {"lead": {"source": source}, "thread": thread, "stage": stage}

    deals = [deal("reddit", True, "delivered"), deal("reddit", True), deal("reddit", True),
             deal("jobs", False), deal("jobs", False), deal("jobs", False), deal("news", True)]
    perf = source_performance(deals)
    assert perf["reddit"]["adjust"] > 0 and perf["jobs"]["adjust"] < 0
    assert perf["news"]["adjust"] == 0  # one pitch is not enough to learn from
    assert perf["reddit"]["won"] == 1 and perf["reddit"]["reply_rate"] == 100
    assert perf["reddit"]["reason"].startswith("learned: reddit leads replied 3 of 3 times")

    from app.discovery.service import discover
    from app.serp.client import serp

    base = {l.name: l.intent_score for l in discover(serp)}
    nudged = {l.name: l for l in discover(serp, learned=perf)}
    assert nudged["r/delhi poster"].intent_score == min(100, base["r/delhi poster"] + perf["reddit"]["adjust"])
    assert any(e.startswith("learned: reddit") for e in nudged["r/delhi poster"].evidence)


def test_worked_leads_are_not_rediscovered():
    from app.discovery.service import discover, worked_keys
    from app.serp.client import serp

    fresh = {l.name: l for l in discover(serp)}
    deals = [  # one closed company deal under a name variant, one Reddit post already pitched
        {"lead": {**fresh["Nestora Realty"].model_dump(), "name": "Nestora Realty Pvt. Ltd."}, "stage": "lost"},
        {"lead": fresh["r/delhi poster"].model_dump(), "stage": "pitched"},
    ]
    again = {l.name for l in discover(serp, worked=worked_keys(deals))}
    assert "Nestora Realty" not in again and "r/delhi poster" not in again
    assert "r/mumbai poster" in again  # another poster, not worked
    assert again == set(fresh) - {"Nestora Realty", "r/delhi poster"}


def test_reddit_posts_from_one_subreddit_are_separate_leads():
    from app.discovery.service import identity

    a = Signal(source="reddit", title="Need gyms", url="https://reddit.com/r/delhi/comments/1/a/", company="r/delhi poster")
    b = Signal(source="reddit", title="Need cafes", url="https://reddit.com/r/delhi/comments/2/b/", company="r/delhi poster")
    assert identity(a) != identity(b)
    assert identity(Signal(source="jobs", title="x", company="Hive Pvt Ltd")) == identity(
        Signal(source="news", title="y", company="Hive"))


def test_discovery_cache_expires(tmp_path):
    import json as _json
    import os as _os
    import time as _time

    from app.config import settings
    from app.serp.client import SerpClient

    class Live:
        calls = 0

        def search(self, params):
            Live.calls += 1
            return {"n": Live.calls}

    s = SerpClient(settings.__class__(**{**settings.__dict__, "data_dir": tmp_path}))
    s._client = Live()
    assert s.fetch("google", max_age_hours=24, q="x") == ({"n": 1}, "live")
    assert s.fetch("google", max_age_hours=24, q="x") == ({"n": 1}, "cache")
    cached = next((tmp_path / "serp_cache").glob("google-*.json"))
    old = _time.time() - 25 * 3600
    _os.utime(cached, (old, old))
    assert s.fetch("google", q="x") == ({"n": 1}, "cache")  # no max age: cached for good (fulfilment)
    assert s.fetch("google", max_age_hours=24, q="x") == ({"n": 2}, "live")
    assert _json.loads(cached.read_text()) == {"n": 2}


# --- lead research (the LLM picks its own searches) ------------------------------


class _FakeSerp:
    def __init__(self):
        self.calls = []

    def fetch(self, engine, **params):
        self.calls.append((engine, params))
        return {"organic_results": [{"title": "Nestora Realty — Pune homes", "link": "https://nestora.example",
                                     "snippet": "Brokerage with 3 offices in Pune"}]}, "live"


def _company_lead():
    from app.discovery.service import discover
    from app.serp.client import serp

    return next(l for l in discover(serp) if l.name == "Nestora Realty")


def test_research_runs_the_searches_the_llm_picks(monkeypatch):
    from types import SimpleNamespace as NS

    from app import llm
    from app.research import research

    seen = []

    def create(**kw):
        seen.append(kw)
        if len(seen) == 1:
            call = NS(id="c1", function=NS(name="web_search", arguments='{"query": "Nestora Realty Pune"}'))
            return NS(choices=[NS(message=NS(content=None, tool_calls=[call]))])
        return NS(choices=[NS(message=NS(content="- Brokerage with 3 offices in Pune (web_search)", tool_calls=None))])

    lead = _company_lead()  # before patching: discovery calls the LLM too
    monkeypatch.setattr(llm, "_groq", NS(chat=NS(completions=NS(create=create))))
    fake = _FakeSerp()
    r = research(lead, fake)

    assert fake.calls == [("google", {"q": "Nestora Realty Pune"})]
    assert r["by"] == "Groq" and r["skipped"] is None
    assert r["searches"] == [{"engine": "google", "query": "Nestora Realty Pune", "source": "live"}]
    assert r["notes"] == ["Brokerage with 3 offices in Pune (web_search)"]
    tool_msg = seen[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and "3 offices in Pune" in tool_msg["content"]
    assert {t["function"]["name"] for t in seen[0]["tools"]} == {"web_search", "news_search", "maps_search"}


def test_research_stops_at_the_search_cap(monkeypatch):
    from types import SimpleNamespace as NS

    from app import llm
    from app.config import settings
    from app.research import research

    choices = []

    def create(**kw):
        choices.append(kw.get("tool_choice", "no tools"))
        if "tools" not in kw:
            return NS(choices=[NS(message=NS(content="- nothing about this company", tool_calls=None))])
        call = NS(id=f"c{len(choices)}", function=NS(name="news_search", arguments=f'{{"query": "q{len(choices)}"}}'))
        return NS(choices=[NS(message=NS(content=None, tool_calls=[call]))])

    lead = _company_lead()  # before patching: discovery calls the LLM too
    monkeypatch.setattr(llm, "_groq", NS(chat=NS(completions=NS(create=create))))
    fake = _FakeSerp()
    r = research(lead, fake)
    assert len(fake.calls) == settings.research_max_searches == 3
    assert choices == ["auto"] * 3 + ["no tools"]
    assert r["notes"] == ["nothing about this company"]


def test_research_claude_loop(monkeypatch):
    from types import SimpleNamespace as NS

    from app import llm
    from app.research import research

    sent = []

    def create(**kw):
        sent.append(kw)
        if len(sent) == 1:
            use = NS(type="tool_use", id="t1", name="maps_search", input={"query": "Nestora Realty", "location": "Pune"})
            return NS(stop_reason="tool_use", content=[use])
        return NS(stop_reason="end_turn", content=[NS(type="text", text="- Listed on Maps in Pune (maps_search)")])

    lead = _company_lead()
    monkeypatch.setattr(llm, "_groq", None)
    monkeypatch.setattr(llm, "_claude", NS(beta=NS(messages=NS(create=create))))
    fake = _FakeSerp()
    r = research(lead, fake)
    assert fake.calls[0][0] == "google_maps" and fake.calls[0][1]["location"] == "Pune"
    assert r["by"] == "Claude" and r["notes"] == ["Listed on Maps in Pune (maps_search)"]
    result = sent[1]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1"
    assert sent[0]["tools"][0]["input_schema"]["required"] == ["query"]


def test_research_skips_reddit_and_runs_without_an_llm():
    from app.research import research

    lead = _company_lead()
    assert research(lead.model_copy(update={"source": "reddit"}), _FakeSerp())["skipped"].startswith("anonymous")
    r = research(lead, _FakeSerp())  # DEMO: no Groq or Claude key
    assert r["skipped"] == "no LLM key set" and r["notes"] == []


def test_research_gemini_loop(monkeypatch):
    from types import SimpleNamespace as NS

    from app import llm
    from app.research import research

    sent = []

    def generate_content(model, contents, config):
        sent.append((list(contents), config))
        if len(sent) == 1:
            call = NS(name="news_search", args={"query": "Nestora Realty funding"})
            return NS(function_calls=[call], text=None, candidates=[NS(content=llm.genai_types.Content(role="model"))])
        return NS(function_calls=None, text="- No funding news found (news_search)")

    lead = _company_lead()
    monkeypatch.setattr(llm, "_groq", None)
    monkeypatch.setattr(llm, "_gemini", NS(models=NS(generate_content=generate_content)))
    fake = _FakeSerp()
    r = research(lead, fake)
    assert fake.calls == [("google_news", {"q": "Nestora Realty funding"})]
    assert r["by"] == "Gemini" and r["notes"] == ["No funding news found (news_search)"]
    reply = sent[1][0][-1].parts[0].function_response
    assert reply.name == "news_search" and reply.response["result"] == "{\"no_results\":true}"  # fake has no news_results
    assert sent[0][1].tool_config.function_calling_config.mode == "AUTO"
