"""Builds the free sample dataset attached to every pitch ("show, don't tell")."""

import csv
import io

from app.serp.client import SerpClient

COLUMNS = ["name", "type", "address", "phone", "website", "rating", "reviews"]


def build_sample(serp: SerpClient, category: str, city: str, rows: int = 10) -> list[dict]:
    result = serp.search("google_maps", q=f"{category} in {city}", type="search", hl="en")
    return [
        {
            "name": r.get("title"),
            "type": r.get("type"),
            "address": r.get("address"),
            "phone": r.get("phone"),
            "website": r.get("website"),
            "rating": r.get("rating"),
            "reviews": r.get("reviews"),
        }
        for r in result.get("local_results", [])[:rows]
    ]


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()
