import hashlib
import re
from collections import Counter

from app import llm
from app.config import settings
from app.discovery import signals as src
from app.discovery.scoring import score_lead
from app.discovery.signals import Emit
from app.models import Lead, Signal
from app.serp.client import SerpClient


LEGAL_SUFFIX = re.compile(r"\b(pvt|private|ltd|limited|llp|inc|co|corp|corporation|opc)\b\.?", re.I)
KEEP_KINDS = {"buyer", "unclear", "reddit"}
KIND_LABEL = {"job_board": "job board / staffing", "data_vendor": "sells data or research itself",
              "enterprise": "large enterprise", "unclear": "unclear, kept for review"}


def company_key(name: str) -> str:
    """"Hive Business Solution Pvt. Ltd." and "Hive Business Solution" are the same company."""
    return re.sub(r"[^a-z0-9]+", " ", LEGAL_SUFFIX.sub("", name.lower())).strip()


def _lead_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32]
    return f"{slug}-{hashlib.sha1(name.lower().encode()).hexdigest()[:6]}"


def discover(serp: SerpClient, min_score: int = 40, emit: Emit = lambda e: None, learned: dict | None = None) -> list[Lead]:
    """`learned` is learning.source_performance() over past deals: it nudges each source's scores."""
    learned = learned or {}
    searches = src.plan()
    markets = ", ".join(m["name"] for m in settings.markets)
    emit({"type": "step", "id": "plan", "status": "done", "label": "Planning searches",
          "summary": f"{len(searches)} searches in {markets} (+ Reddit worldwide)",
          "items": [f"{src.ENGINE_LABELS[s['engine']]} · {s['market']}: {s['q']} — {s['why']}" for s in searches]})

    found: list[Signal] = src.from_jobs(serp, emit) + src.from_reddit(serp, emit) + src.from_news(serp, emit)

    emit({"type": "step", "id": "merge", "status": "running", "label": "Merging signals"})
    by_company: dict[str, list[Signal]] = {}
    seen: set[tuple] = set()
    for s in found:
        key = (s.source, s.title.lower(), (s.company or "").lower())
        if key in seen:  # the same posting often matches several queries
            continue
        seen.add(key)
        if s.company and company_key(s.company):
            by_company.setdefault(company_key(s.company), []).append(s)
    multi = [sigs[0].company for sigs in by_company.values() if len({x.source for x in sigs}) > 1]
    emit({"type": "step", "id": "merge", "status": "done",
          "summary": f"{len(found)} signals → {len(by_company)} companies ({len(found) - len(seen)} duplicates dropped)",
          "items": [f"{name}: confirmed by more than one source" for name in multi]})

    emit({"type": "step", "id": "score", "status": "running", "label": "Scoring buying intent"})
    leads, dropped = [], []
    for sigs in by_company.values():
        score, evidence = score_lead(sigs)
        name = sigs[0].company
        if (p := learned.get(sigs[0].source)) and p["adjust"]:
            score = max(0, min(100, score + p["adjust"]))
            evidence = evidence + [p["reason"]]
        if score < min_score:
            dropped.append((name, score))
            continue
        market = _market_of(sigs)
        leads.append(
            Lead(
                id=_lead_id(name),
                name=name,
                source=sigs[0].source,
                location=next((s.location for s in sigs if s.location), None),
                market=market,
                currency=_currency_of(market),
                intent_score=score,
                evidence=evidence,
                signals=sigs,
            )
        )
    leads.sort(key=lambda l: -l.intent_score)
    emit({"type": "step", "id": "score", "status": "done",
          "summary": f"{len(leads)} leads at or above {min_score}/100 · {len(dropped)} dropped"
          + "".join(f" · {src_} {'+' if p['adjust'] > 0 else ''}{p['adjust']} (learned)"
                    for src_, p in learned.items() if p["adjust"]),
          "items": [f"✓ {l.name} — {l.intent_score}/100 · {'; '.join(l.evidence[1:3]) or l.evidence[0]}" for l in leads]
          + [f"✗ {name} — {score}/100, no clear need for data" for name, score in dropped]})
    return _vet(leads, emit)


def _market_of(sigs: list[Signal]) -> str | None:
    found = Counter(s.market for s in sigs if s.market)
    return found.most_common(1)[0][0] if found else None


def _currency_of(market: str | None) -> str:
    return next((m["currency"] for m in settings.markets if m["name"] == market), settings.default_currency)


def _vet(leads: list[Lead], emit: Emit) -> list[Lead]:
    """Drop job boards, competitors and enterprises: they score well on intent but won't buy."""
    companies = [l for l in leads if l.source != "reddit"]  # Reddit posters are people, not companies
    if not companies:
        return leads
    emit({"type": "step", "id": "vet", "status": "running", "label": "Checking who would actually buy"})
    verdicts = llm.vet_companies(
        [{"name": l.name, "evidence": f"{l.signals[0].title} ({l.location or l.market or 'location unknown'})"} for l in companies]
    )
    kind_of = {l.id: v for l, v in zip(companies, verdicts)}
    kept, items = [], []
    for l in leads:
        kind, reason = kind_of.get(l.id, ("reddit", "a person asking on Reddit"))
        if kind in KEEP_KINDS:
            if kind != "reddit":
                l.evidence.append(f"buyer check: {reason}")
            kept.append(l)
            items.append(f"✓ {l.name} — {reason}")
        else:
            items.append(f"✗ {l.name} — {KIND_LABEL[kind]}: {reason}")
    how = (f" · ⚠ {llm.last_fallback}: used keyword rules, unchecked companies marked 'review'"
           if llm.last_fallback else f" · checked by {llm.last_provider}"
           + (f" · {llm.last_unanswered} not classified, kept for review" if llm.last_unanswered else ""))
    emit({"type": "step", "id": "vet", "status": "done",
          "summary": f"{len(kept)} kept · {len(leads) - len(kept)} dropped (job boards, competitors, enterprises){how}",
          "items": items})
    return kept
