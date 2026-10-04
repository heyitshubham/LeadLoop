"""Builds the dataset a lead paid for, after they accept a quote.

  1. collect  - Google Maps, 20 rows a page: the sample's city first, then the market's other
                big cities, until the order is filled or the search budget runs out
  2. dedupe   - by Google place id, else by name + phone
  3. score    - a completeness score per row (phone, website, rating, hours ...); best rows first
  4. enrich   - the top rows get what customers mention (Google Maps Reviews) and whether the
                business is hiring right now (Google Jobs)
  5. price    - code re-checks the invoice: fewer rows than ordered means a pro-rata invoice,
                and every search is costed so each order shows its margin

The full CSV is written to data/datasets/; deal state keeps only a preview and the report.
"""

import csv
import io
import re
from pathlib import Path

from app.config import Settings, settings as default_settings
from app.discovery.service import company_key
from app.serp.client import SerpClient

COLUMNS = [
    "name", "type", "address", "city", "phone", "website", "rating", "reviews", "price_level",
    "open_state", "latitude", "longitude", "maps_url", "quality", "customers_mention", "hiring_now",
]
PAGE = 20
MAX_START = 100  # SerpApi: past this offset Maps results repeat or drift off-topic
# Where to look once the sample's city is exhausted.
MARKET_CITIES = {
    "India": ["Mumbai", "Delhi", "Bengaluru", "Pune", "Hyderabad", "Chennai", "Kolkata", "Ahmedabad", "Jaipur"],
    "United States": ["New York", "Los Angeles", "Chicago", "Houston", "Austin", "Phoenix", "Seattle", "Miami"],
    "United Kingdom": ["London", "Manchester", "Birmingham", "Leeds", "Glasgow"],
    "Canada": ["Toronto", "Vancouver", "Montreal", "Calgary"],
    "Australia": ["Sydney", "Melbourne", "Brisbane", "Perth"],
    "Singapore": ["Singapore"],
    "United Arab Emirates": ["Dubai", "Abu Dhabi", "Sharjah"],
}


class _Budget:
    """Counts searches by engine and stops the order at the configured limit."""

    def __init__(self, serp: SerpClient, limit: int):
        self.serp, self.limit = serp, limit
        self.by_engine: dict[str, int] = {}
        self.live = 0

    @property
    def used(self) -> int:
        return sum(self.by_engine.values())

    def left(self) -> int:
        return self.limit - self.used

    def fetch(self, engine: str, **params) -> dict:
        result, source = self.serp.fetch(engine, **{k: v for k, v in params.items() if v is not None})
        self.by_engine[engine] = self.by_engine.get(engine, 0) + 1
        self.live += source == "live"
        return result


def _key(r: dict) -> str:
    if r.get("place_id"):
        return r["place_id"]
    return f"{company_key(r.get('title') or '')}|{re.sub(r'[^0-9]', '', r.get('phone') or '')}"


def quality(row: dict) -> int:
    """0-100: how usable a row is for outreach. Phone and website matter most."""
    score = 30 * bool(row.get("phone")) + 25 * bool(row.get("website")) + 10 * bool(row.get("address"))
    score += 15 * (row.get("rating") is not None) + 10 * ((row.get("reviews") or 0) >= 10)
    score += 10 * bool(row.get("open_state"))
    return score


def _row(r: dict, city: str) -> dict:
    gps = r.get("gps_coordinates") or {}
    row = {
        "name": r.get("title"),
        "type": r.get("type"),
        "address": r.get("address"),
        "city": city,
        "phone": r.get("phone"),
        "website": r.get("website"),
        "rating": r.get("rating"),
        "reviews": r.get("reviews"),
        "price_level": r.get("price"),
        "open_state": r.get("open_state"),
        "latitude": gps.get("latitude"),
        "longitude": gps.get("longitude"),
        "maps_url": f"https://www.google.com/maps/place/?q=place_id:{r['place_id']}" if r.get("place_id") else None,
        "customers_mention": None,
        "hiring_now": None,
        "_ids": {"place_id": r.get("place_id"), "data_id": r.get("data_id")},
    }
    row["quality"] = quality(row)
    return row


def _collect(b: _Budget, category: str, cities: list[str], want: int, reserve: int) -> tuple[list[dict], dict]:
    rows: dict[str, dict] = {}
    raw = 0
    searched: list[str] = []
    for city in cities:
        for start in range(0, MAX_START + 1, PAGE):
            if len(rows) >= want or b.left() <= reserve:
                break
            page = b.fetch("google_maps", q=f"{category} in {city}", type="search", hl="en", start=start or None)
            results = page.get("local_results", [])
            raw += len(results)
            before = len(rows)
            for r in results:
                rows.setdefault(_key(r), _row(r, city))
            if len(rows) > before and city not in searched:  # only cities that added new rows
                searched.append(city)
            if len(results) < PAGE or len(rows) == before:  # last page, or only repeats
                break
        if len(rows) >= want or b.left() <= reserve:
            break
    return list(rows.values()), {"raw": raw, "unique": len(rows), "cities": searched}


def _review_topics(b: _Budget, row: dict) -> str | None:
    ids = row["_ids"]
    params = {"data_id": ids["data_id"]} if ids.get("data_id") else {"place_id": ids["place_id"]} if ids.get("place_id") else None
    if not params:
        return None
    topics = b.fetch("google_maps_reviews", hl="en", **params).get("topics", [])
    top = sorted(topics, key=lambda t: -(t.get("mentions") or 0))[:3]
    return ", ".join(f"{t['keyword']} ({t.get('mentions', 0)})" for t in top if t.get("keyword")) or None


def _hiring(b: _Budget, row: dict) -> str:
    """Google Jobs returns other employers too, so only postings by this business count."""
    jobs = b.fetch("google_jobs", q=f"{row['name']} {row['city']}", hl="en").get("jobs_results", [])
    own = [j for j in jobs if company_key(j.get("company_name") or "") == company_key(row["name"] or "")]
    return f"yes: {own[0].get('title')}" + (f" (+{len(own) - 1} more)" if len(own) > 1 else "") if own else "no"


def _cost(searches: int, currency: str, cfg: Settings) -> float:
    usd = searches * cfg.serp_cost_per_search_usd
    return round(usd * cfg.usd_to_inr, 2) if currency == "INR" else round(usd, 2)


def build_order(
    serp: SerpClient, category: str, city: str, market: str | None, quote: dict, cfg: Settings = default_settings
) -> tuple[list[dict], dict]:
    """Returns (rows best-first, report). Never invoices for rows it did not deliver."""
    b = _Budget(serp, cfg.fulfil_max_searches)
    want = quote["rows"]
    enrich = min(cfg.fulfil_enrich_rows, want)
    cities = [city] + [c for c in MARKET_CITIES.get(market or "", []) if c.lower() != city.lower()]

    rows, seen = _collect(b, category, cities, want, reserve=2 * enrich)
    rows.sort(key=lambda r: -r["quality"])
    rows = rows[:want]

    enriched = 0
    for row in rows[:enrich]:
        if b.left() < 2:
            break
        row["customers_mention"] = _review_topics(b, row)
        row["hiring_now"] = _hiring(b, row)
        enriched += 1
    for row in rows:
        row.pop("_ids", None)

    n = len(rows)
    price = quote["price"] if n >= want else round(quote["price"] * n / want)
    cost = _cost(b.used, quote["currency"], cfg)

    def pct(field):
        return round(100 * sum(1 for r in rows if r.get(field)) / n) if n else 0

    report = {
        "ordered": want,
        "delivered": n,
        "cities": seen["cities"],
        "duplicates_dropped": seen["raw"] - seen["unique"],
        "enriched": enriched,
        "avg_quality": round(sum(r["quality"] for r in rows) / n) if n else 0,
        "completeness": {f: pct(f) for f in ("phone", "website", "rating", "open_state")},
        "searches": dict(b.by_engine),
        "searches_total": b.used,
        "live_credits": b.live,
        "currency": quote["currency"],
        "invoice": price,
        "invoice_note": "as quoted" if n >= want else f"pro-rata: {n} of {want} rows found",
        "serp_cost": cost,
        "margin_pct": round(100 * (price - cost) / price) if price else None,
    }
    return rows, report


def datasets_dir(cfg: Settings = default_settings) -> Path:
    d = cfg.data_dir / "datasets"
    d.mkdir(exist_ok=True)
    return d


def save(deal_id: str, rows: list[dict]) -> Path:
    path = datasets_dir() / f"{re.sub(r'[^a-zA-Z0-9_-]', '_', deal_id)}.csv"
    path.write_text(to_csv(rows))
    return path


def load(deal_id: str) -> str | None:
    path = datasets_dir() / f"{re.sub(r'[^a-zA-Z0-9_-]', '_', deal_id)}.csv"
    return path.read_text() if path.exists() else None


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()
