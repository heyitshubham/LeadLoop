"""Buying-intent sources. Each turns one SerpApi engine's response into Signals.

Job and news searches run once per market in LEADLOOP_MARKETS (each with that country's
Google region); Reddit is searched once, worldwide.

We look for people already asking for data, not for everyone who exists:
  jobs   - companies hiring for research / lead-gen / data-entry work
  reddit - public posts asking for lists or datasets
  news   - freshly funded startups that will need market data

Every search reports its progress through `emit`, so the board can show the agent's work live.
"""

import html
import time
from collections.abc import Callable
from urllib.parse import urlparse

from app import llm
from app.config import settings
from app.models import Signal
from app.serp.client import SerpClient

Emit = Callable[[dict], None]

# Companies hiring for outbound / research work are the ones who need lead lists and market data.
JOB_QUERIES = ["market research analyst", "lead generation executive", "inside sales executive"]
REDDIT_QUERY = 'site:reddit.com ("need a list of" OR "looking for a dataset" OR "need data on")'
NEWS_QUERY = "startup raises seed funding"

ENGINE_LABELS = {"google_jobs": "Google Jobs", "google": "Google Search", "google_news": "Google News"}


def plan() -> list[dict]:
    """Every search discovery will run, in order (also shown to the user up front)."""
    out = []
    for m in settings.markets:
        out += [{"engine": "google_jobs", "q": q, "market": m["name"], "why": "companies hiring for this work need data"}
                for q in JOB_QUERIES]
    out.append({"engine": "google", "q": REDDIT_QUERY, "market": "worldwide", "why": "people publicly asking for lists"})
    out += [{"engine": "google_news", "q": NEWS_QUERY, "market": m["name"], "why": "newly funded startups buy market data"}
            for m in settings.markets]
    return out


def _search(serp: SerpClient, emit: Emit, step_id: str, engine: str, q: str, market: str | None = None, **params) -> dict:
    where = f" · {market}" if market else ""
    emit({"type": "step", "id": step_id, "status": "running", "label": f"Searching {ENGINE_LABELS[engine]}{where}",
          "engine": engine, "query": q})
    params = {k: v for k, v in params.items() if v is not None}
    t0 = time.perf_counter()
    result, source = serp.fetch(engine, q=q, **params)
    return {"result": result, "source": source, "ms": round((time.perf_counter() - t0) * 1000)}


def _done(emit: Emit, step_id: str, run: dict, summary: str, items: list[str]) -> None:
    emit({"type": "step", "id": step_id, "status": "done", "summary": summary, "source": run["source"],
          "ms": run["ms"], "items": items[:8]})


def _posted(job: dict) -> str | None:
    """Real results put the age in `extensions` ("12 days ago"); some carry `posted_at`."""
    ext = job.get("detected_extensions") or {}
    return ext.get("posted_at") or next((e for e in job.get("extensions", []) if "ago" in e), None)


def from_jobs(serp: SerpClient, emit: Emit = lambda e: None) -> list[Signal]:
    signals = []
    for m in settings.markets:
        for i, q in enumerate(JOB_QUERIES):
            sid = f"jobs-{m['gl'] or m['name']}-{i}"
            run = _search(serp, emit, sid, "google_jobs", q, m["name"], location=m["name"], gl=m["gl"], hl="en")
            jobs = run["result"].get("jobs_results", [])
            # "Anywhere" listings are mostly remote-job spam aggregators, not companies with a need.
            placed = [j for j in jobs if (j.get("location") or "").strip().lower() not in ("", "anywhere")]
            for job in placed:
                signals.append(
                    Signal(
                        source="jobs",
                        title=html.unescape(job.get("title", "")),
                        url=job.get("share_link") or (job.get("apply_options") or [{}])[0].get("link"),
                        snippet=html.unescape(job.get("description") or "")[:400],
                        posted=_posted(job),
                        company=html.unescape(job.get("company_name") or "") or None,
                        location=job.get("location"),
                        market=m["name"],
                    )
                )
            skipped = len(jobs) - len(placed)
            _done(emit, sid, run,
                  f"{len(placed)} job post{'s' * (len(placed) != 1)}" + (f" · {skipped} 'Anywhere' listings skipped" if skipped else ""),
                  [f"{html.unescape(j.get('company_name', '?'))} — {html.unescape(j.get('title', ''))} ({j.get('location', '?')})" for j in jobs])
    return signals


def from_reddit(serp: SerpClient, emit: Emit = lambda e: None) -> list[Signal]:
    signals = []
    run = _search(serp, emit, "reddit", "google", REDDIT_QUERY, "worldwide", num=20)
    posts = run["result"].get("organic_results", [])
    for r in posts:
        link = r.get("link", "")
        parts = urlparse(link).path.strip("/").split("/")
        sub = parts[1] if len(parts) > 1 and parts[0] == "r" else "reddit"
        signals.append(
            Signal(
                source="reddit",
                title=r.get("title", ""),
                url=link,
                snippet=r.get("snippet", ""),
                posted=r.get("date"),
                company=f"r/{sub} poster",
            )
        )
    _done(emit, "reddit", run, f"{len(posts)} Reddit post{'s' * (len(posts) != 1)} asking for data",
          [p.get("title", "") for p in posts])
    return signals


def from_news(serp: SerpClient, emit: Emit = lambda e: None) -> list[Signal]:
    signals = []
    for m in settings.markets:
        signals += _news_for(serp, emit, m)
    return signals


def _news_for(serp: SerpClient, emit: Emit, m: dict) -> list[Signal]:
    key = m["gl"] or m["name"]
    run = _search(serp, emit, f"news-{key}", "google_news", NEWS_QUERY, m["name"], gl=m["gl"], hl="en")
    stories = []
    for n in run["result"].get("news_results", []):
        # Google News groups some results into topic clusters; their articles sit under "stories".
        stories.extend([n] if n.get("title") else [])
        stories.extend(s for s in n.get("stories", []) if s.get("title"))
    _done(emit, f"news-{key}", run, f"{len(stories)} funding stor{'ies' if len(stories) != 1 else 'y'}",
          [n["title"] for n in stories])

    # Headlines don't come with a company field, so the LLM reads them (one call for all).
    missing = [i for i, n in enumerate(stories) if not n.get("company")]
    if missing:
        sid = f"news-extract-{key}"
        emit({"type": "step", "id": sid, "status": "running", "label": f"Reading headlines for company names · {m['name']}"})
        t0 = time.perf_counter()
        names = llm.extract_companies([stories[i]["title"] for i in missing])
        for i, name in zip(missing, names):
            stories[i] = {**stories[i], "company": name}
        found = sum(1 for n in names if n)
        emit({"type": "step", "id": sid, "status": "done",
              "summary": f"{found} of {len(missing)} headlines name a single company"
                         + (f" · ⚠ {llm.last_fallback}: used a pattern rule" if llm.last_fallback else f" · read by {llm.last_provider}"
                            + (f" · {llm.last_unanswered} unanswered, pattern rule used" if llm.last_unanswered else "")),
              "ms": round((time.perf_counter() - t0) * 1000),
              "items": [f"{'✓' if name else '✗'} {stories[i]['title']} → {name or 'no single company'}"
                        for i, name in zip(missing, names)]})

    return [
        Signal(
            source="news",
            title=html.unescape(n["title"]),
            url=n.get("link"),
            snippet=n.get("snippet", ""),
            posted=n.get("iso_date") or n.get("date"),
            company=n.get("company"),
            market=m["name"],
        )
        for n in stories
        if n.get("company")
    ]
