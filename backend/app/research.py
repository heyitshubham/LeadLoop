"""Lead research: before qualifying, the LLM decides what to look up about the lead and runs the
searches itself, using serpapi-search-tools (web, news and Maps) as LangGraph tools.

The tools search through our SerpClient, so every search is cached, counted against the credit
budget and replayable offline, and each lead gets at most RESEARCH_MAX_SEARCHES of them.
"""

import json
import logging

from langchain_core.utils.function_calling import convert_to_openai_tool
from serpapi_search_tools import SearchResultFormat, maps_search, news_search, web_search

from app import llm
from app.config import settings
from app.models import Lead
from app.serp.client import CreditBudgetExceeded, SerpClient

log = logging.getLogger(__name__)

MAX_NOTES = 5
# What the model reads from each result. Links, thumbnails and tracking fields are most of a raw
# SerpApi response; dropping them keeps three searches inside Groq's free-tier tokens per minute.
KEEP_FIELDS = ("title", "link", "snippet", "source", "date", "address", "type", "rating", "reviews",
               "phone", "website", "description", "text", "snippet_highlighted_words")
MAX_TEXT = 300


def _slim(result: dict) -> dict:
    def item(x):
        if not isinstance(x, dict):
            return x
        out = {k: x[k] for k in KEEP_FIELDS if x.get(k) not in (None, "", [])}
        if isinstance(x.get("source"), dict):  # news: {"name": ..., "icon": ...}
            out["source"] = x["source"].get("name")
        return {k: v[:MAX_TEXT] if isinstance(v, str) else v for k, v in out.items()}

    return {k: [item(x) for x in v] if isinstance(v, list) else item(v) for k, v in result.items()}


class _CappedClient:
    """The search client the tools call: routes through SerpClient and records each search.
    A search repeated with the same parameters (e.g. by a fallback model) is free and not counted again."""

    def __init__(self, serp: SerpClient, cap: int):
        self.serp, self.cap = serp, cap
        self.searches: list[dict] = []
        self._seen: set[str] = set()
        self.out_of_credits = False

    def may_search(self) -> bool:
        return len(self.searches) < self.cap and not self.out_of_credits

    def search(self, params: dict) -> dict:
        params = dict(params)
        engine = params.pop("engine")
        key = json.dumps([engine, params], sort_keys=True)
        entry = {"engine": engine, "query": params.get("q", ""), "source": "failed"}
        if key not in self._seen:
            self._seen.add(key)
            self.searches.append(entry)
        try:
            result, entry["source"] = self.serp.fetch(engine, **params)
        except CreditBudgetExceeded:
            self.out_of_credits = True
            raise
        return _slim(result)


def _tools(client: _CappedClient) -> dict:
    kw = dict(provider="langgraph", client=client, response_format=SearchResultFormat.JSON,
              result_limit=5, include_examples=False)
    tools = [web_search(default_engine="google", allowed_engines=["google"], **kw), news_search(**kw), maps_search(**kw)]
    return {t.name: t for t in tools}


def _prompt(lead: Lead, cap: int) -> str:
    return (
        f"{llm._evidence(lead)}\n\n"
        f"Before we pitch, research this lead with at most {cap} searches. Find out what the company does, "
        "where it operates, and anything recent (funding, expansion, hiring) that shows which business data "
        "would help them. Search for the company itself first.\n"
        f"Then answer with at most {MAX_NOTES} lines, each starting with '- ': one fact you found and which "
        "search showed it. Only facts from the search results about this company; if the searches found "
        "nothing about it, say so in one line. No advice and no pitch."
    )


def _notes(text: str) -> list[str]:
    lines = [l.strip().lstrip("-•* ").strip() for l in text.splitlines()]
    bullets = [l for raw, l in zip(text.splitlines(), lines) if raw.strip()[:1] in "-•*" and l]
    return (bullets or [l for l in lines if l])[:MAX_NOTES]


def research(lead: Lead, serp: SerpClient) -> dict:
    """{"notes", "searches", "by", "skipped"}: what the agent found, the searches it chose, which LLM ran it."""
    cap = settings.research_max_searches
    if cap <= 0:
        return {"notes": [], "searches": [], "by": None, "skipped": "turned off (RESEARCH_MAX_SEARCHES=0)"}
    if lead.source == "reddit":
        return {"notes": [], "searches": [], "by": None, "skipped": "anonymous Reddit poster, nothing to look up"}

    client = _CappedClient(serp, cap)
    tools = _tools(client)

    def call(name: str, args: dict) -> str:
        if name not in tools:
            return f"No tool named {name}."
        if not client.may_search():
            return "Search limit reached. Answer with what you have."
        try:
            return tools[name].invoke(args)
        except Exception as e:  # the model sees the failure and can carry on without it
            log.warning("research search failed: %s", e)
            return f"Search failed: {e}"

    text, by = llm.run_tools(_prompt(lead, cap), [convert_to_openai_tool(t) for t in tools.values()],
                             call, client.may_search)
    if text is None:
        return {"notes": [], "searches": client.searches, "by": None, "skipped": by}
    return {"notes": _notes(text), "searches": client.searches, "by": by, "skipped": None}
