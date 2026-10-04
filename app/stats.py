"""Aggregates for the Insights view. All numbers are computed here; the page only draws them."""

from collections import Counter

from app.models import Lead

INTENT_BINS = [(40, 49), (50, 59), (60, 69), (70, 79), (80, 89), (90, 100)]
CLOSED = {"won", "lost", "rejected", "disqualified"}


def _by_currency(quotes) -> dict[str, int]:
    totals: dict[str, int] = {}
    for q in quotes:
        cur = q.get("currency", "INR")
        totals[cur] = totals.get(cur, 0) + q["price"]
    return totals


def compute(leads: list[Lead], deals: list[dict]) -> dict:
    by_id = {d["id"]: d for d in deals}
    names = {l.id: l.name for l in leads}

    pitched = [d for d in deals if any(m["dir"] == "out" for m in d.get("thread", []))]
    replied = [d for d in deals if any(m["dir"] == "in" for m in d.get("thread", []))]
    qualified = [d for d in deals if d.get("assessment") and d.get("stage") != "disqualified"]
    won = [d for d in deals if d.get("stage") == "won"]
    open_quoted = [d for d in deals if d.get("quote") and d.get("stage") not in CLOSED]

    def gate(d):
        return (d.get("pending_gate") or {}).get("gate")

    flow = {
        "discovered": sum(1 for l in leads if l.id not in by_id),
        "pitch_approval": sum(1 for d in deals if gate(d) == "pitch"),
        "waiting_on_lead": sum(1 for d in deals if gate(d) == "lead"),
        "reply_approval": sum(1 for d in deals if gate(d) == "reply"),
        "won": len(won),
        "closed": sum(1 for d in deals if d.get("stage") in CLOSED - {"won"}),
    }

    actors = Counter(a["actor"] for d in deals for a in d.get("audit", []))
    decisions = actors["agent"] + actors["human"]

    # Prices are shown as % of the opening quote so INR and USD deals share one axis.
    negotiations = [
        {"deal": names.get(d["id"], d["id"]), "stage": d.get("stage"),
         "currency": d["quote_history"][0].get("currency", "INR"),
         "points": [{"round": i + 1, "price": q["price"], "rows": q["rows"], "intent": q.get("intent"),
                     "pct": round(100 * q["price"] / d["quote_history"][0]["price"])}
                    for i, q in enumerate(d["quote_history"])]}
        for d in deals if len(d.get("quote_history", [])) >= 1
    ]
    negotiations.sort(key=lambda n: -len(n["points"]))

    # Newest first; a deal's own step order breaks same-millisecond ties.
    steps = [(a["at"], i, {"deal": names.get(d["id"], d["id"]), **a})
             for d in deals for i, a in enumerate(d.get("audit", []))]
    activity = [s for _, _, s in sorted(steps, key=lambda x: (x[0], x[1]), reverse=True)][:12]

    return {
        "kpis": {
            "leads": len(leads),
            "pitched": len(pitched),
            "reply_rate": round(100 * len(replied) / len(pitched)) if pitched else None,
            "won": len(won),
            # Never add rupees to dollars: one total per currency.
            "revenue_won": _by_currency(d["quote"] for d in won if d.get("quote")),
            "pipeline_value": _by_currency(d["quote"] for d in open_quoted),
            "automation_pct": round(100 * actors["agent"] / decisions) if decisions else None,
        },
        "funnel": [
            {"stage": "Discovered", "count": len(leads)},
            {"stage": "Qualified", "count": len(qualified)},
            {"stage": "Pitched", "count": len(pitched)},
            {"stage": "Replied", "count": len(replied)},
            {"stage": "Won", "count": len(won)},
        ],
        "sources": [{"source": s, "count": c} for s, c in Counter(l.source for l in leads).most_common()],
        "markets": [{"market": m or "Global (Reddit)", "count": c}
                    for m, c in Counter(l.market for l in leads).most_common()],
        "intent_bins": [
            {"bin": f"{lo}–{hi}", "count": sum(1 for l in leads if lo <= l.intent_score <= hi)}
            for lo, hi in INTENT_BINS
        ],
        "actors": [{"actor": a, "count": actors[a]} for a in ("agent", "human", "lead")],
        "flow": flow,
        "negotiations": negotiations[:3],  # categorical palette is CVD-safe for 3 series on one chart
        "activity": activity,
    }
