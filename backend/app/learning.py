"""What past deals say about each lead source, fed back into intent scoring.

A source whose pitches get replies more often than average earns its next leads a bonus;
one that stays silent loses points. Rates are smoothed (one imagined reply in two pitches)
and a source needs MIN_PITCHED pitches before it moves any score, so one lucky reply
can't reorder the board. Every adjustment is written into the lead's evidence.
"""

WON = {"won", "delivered"}
MIN_PITCHED = 3
MAX_ADJUST = 10


def _smoothed(replied: int, pitched: int) -> float:
    return (replied + 1) / (pitched + 2)


def source_performance(deals: list[dict]) -> dict[str, dict]:
    perf: dict[str, dict] = {}
    for d in deals:
        source = (d.get("lead") or {}).get("source")
        thread = d.get("thread", [])
        if not source or not any(m["dir"] == "out" for m in thread):
            continue
        p = perf.setdefault(source, {"pitched": 0, "replied": 0, "won": 0})
        p["pitched"] += 1
        p["replied"] += any(m["dir"] == "in" for m in thread)
        p["won"] += d.get("stage") in WON

    pitched = sum(p["pitched"] for p in perf.values())
    replied = sum(p["replied"] for p in perf.values())
    overall = _smoothed(replied, pitched)
    for source, p in perf.items():
        p["reply_rate"] = round(100 * p["replied"] / p["pitched"])
        adjust = 0
        if p["pitched"] >= MIN_PITCHED:
            adjust = max(-MAX_ADJUST, min(MAX_ADJUST, round(40 * (_smoothed(p["replied"], p["pitched"]) - overall))))
        p["adjust"] = adjust
        p["reason"] = (
            f"learned: {source} leads replied {p['replied']} of {p['pitched']} times "
            f"(vs {round(100 * replied / pitched)}% overall) {'+' if adjust > 0 else ''}{adjust}"
            if adjust else None
        )
    return perf
