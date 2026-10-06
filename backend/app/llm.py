"""LLM calls with structured output. Providers are tried in order — Groq, Gemini, Claude —
using whichever keys are set; if all fail (quota, outage), each function falls back to
deterministic rules, and `last_fallback` says why so the UI can show it."""

import json
import logging
import re
import time
from typing import Callable, Literal

import anthropic
import groq
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field, ValidationError

from app.config import settings
from app.models import Assessment, EmailDraft, Lead, Pitch, ReplyRead
from app.pipeline.pricing import money

log = logging.getLogger(__name__)

_providers = settings.llm_providers
_groq = groq.Groq(api_key=settings.groq_key, max_retries=0) if "groq" in _providers else None
_gemini = genai.Client(api_key=settings.gemini_key) if "gemini" in _providers else None
_claude = anthropic.Anthropic(api_key=settings.anthropic_key) if "claude" in _providers else None

SYSTEM = (
    "You are the sales brain of a small market-research desk serving India and the US. Ground every claim in "
    "the evidence you are given; never invent facts about the lead.\n"
    "What we offer, exactly: a research service on local businesses. For the client's target market we find "
    "businesses in public Google Maps and Google search results, remove duplicates, score each one for "
    "completeness, and for the best ones summarise what customers mention in reviews and whether they are "
    "hiring. The client gets the research as a CSV with name, type, address, phone, website, rating and review "
    "count where listed. The fee pays for this research work and is priced per business researched; the first "
    "10-business sample is free. Delivery within 48 hours of payment.\n"
    "Never claim anything beyond that: no manual or hand verification, no email addresses, no "
    "owner names, no accuracy guarantees, no prices other than the ones you are given.\n"
    "Write for busy founders and managers: short, specific, polite, in short paragraphs."
)


# Why the most recent call fell back to rules (None if an LLM answered), and which LLM answered.
# Callers show these in the live panel so a rules-based result is never presented as the AI's.
last_fallback: str | None = None
last_provider: str | None = None


def _parse[T: BaseModel](prompt: str, schema: type[T], effort: str = "low") -> T | None:
    global last_fallback, last_provider
    reasons = []
    for name, call in (("Groq", _parse_groq if _groq else None), ("Gemini", _parse_gemini if _gemini else None),
                       ("Claude", _parse_claude_safe if _claude else None)):
        if call is None:
            continue
        result, why = call(prompt, schema, effort) if name == "Groq" else call(prompt, schema)
        if result is not None:
            last_fallback, last_provider = None, name
            return result
        reasons.append(why)
    last_fallback, last_provider = "; ".join(r for r in reasons if r) or "no LLM key set", None
    return None


# --- Groq ------------------------------------------------------------------------


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic's JSON schema in the shape strict structured output needs: no $refs, every
    property required (optional ones are nullable unions), no extra properties, no defaults."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def fix(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return fix(defs[node["$ref"].split("/")[-1]])
            node = {k: fix(v) for k, v in node.items() if k != "default"}
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            return node
        if isinstance(node, list):
            return [fix(x) for x in node]
        return node

    return fix(raw)


def _parse_groq[T: BaseModel](prompt: str, schema: type[T], effort: str = "low") -> tuple[T | None, str | None]:
    fmt = {"type": "json_schema", "json_schema": {"name": schema.__name__, "strict": True, "schema": strict_schema(schema)}}
    why = "Groq unavailable"
    # Limits are per model, so a quota hit on the main model moves on to the backup model.
    for model in [m for m in (settings.groq_model, settings.groq_fallback_model) if m]:
        for attempt in range(2):
            try:
                r = _groq.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                    response_format=fmt,
                    reasoning_effort=effort,
                    include_reasoning=False,
                )
                return schema.model_validate_json(r.choices[0].message.content), None
            except groq.RateLimitError as e:
                wait = float(e.response.headers.get("retry-after", "60"))
                why = f"Groq rate limit on {model}"
                if attempt == 0 and wait <= 20:  # per-minute token limit: a short wait clears it
                    log.warning("%s; retrying in %.0fs", why, wait)
                    time.sleep(wait + 0.5)
                    continue
                log.warning("%s (retry after %.0fs); trying the next model", why, wait)
                break
            except ValidationError:
                why = f"Groq ({model}) returned JSON that didn't match the schema"
                log.warning(why)
                break
            except groq.APIStatusError as e:
                why = f"Groq error {e.status_code} on {model}"
                log.warning("%s: %s", why, str(e)[:200])
                break
            except (groq.APIConnectionError, groq.APITimeoutError):
                log.warning("Groq unreachable")
                return None, "Groq unreachable"
    return None, why


def _parse_claude_safe[T: BaseModel](prompt: str, schema: type[T]) -> tuple[T | None, str | None]:
    result = _parse_claude(prompt, schema)
    return result, None if result else "Claude unavailable"


def _parse_gemini[T: BaseModel](prompt: str, schema: type[T]) -> tuple[T | None, str | None]:
    config = genai_types.GenerateContentConfig(
        system_instruction=SYSTEM,
        response_mime_type="application/json",
        response_schema=schema,
        thinking_config=genai_types.ThinkingConfig(thinking_level=genai_types.ThinkingLevel.LOW),
        automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
    )
    # 503 "high demand" spikes are common: retry the main model, then try the fallback model.
    # Quotas (429) are per model, so a quota error skips straight to the next model.
    attempts = [settings.gemini_model] * 2 + ([settings.gemini_fallback_model] * 2 if settings.gemini_fallback_model else [])
    exhausted: set[str] = set()
    why = "Gemini unavailable"
    response = None
    for attempt, model in enumerate(attempts):
        if model in exhausted:
            continue
        try:
            response = _gemini.models.generate_content(model=model, contents=prompt, config=config)
            break
        except genai_errors.ServerError as e:
            log.warning("Gemini %s on %s (attempt %d/%d)", e.code, model, attempt + 1, len(attempts))
            why = f"Gemini busy ({e.code})"
            time.sleep(1.5 * (attempt + 1))
        except genai_errors.ClientError as e:
            if e.code == 429:
                log.warning("Gemini daily quota used up for %s; trying the next model", model)
                exhausted.add(model)
                why = "Gemini free-tier daily quota used up"
                continue
            log.warning("Gemini call failed (%s %s); using rules fallback", e.code, e.message)
            return None, f"Gemini error {e.code}"
    if response is None:
        log.warning("%s; using rules fallback", why)
        return None, why
    if isinstance(response.parsed, schema):
        return response.parsed, None
    log.warning("Gemini returned no parseable output; using rules fallback")
    return None, "Gemini returned no usable answer"


def _parse_claude[T: BaseModel](prompt: str, schema: type[T]) -> T | None:
    try:
        response = _claude.beta.messages.parse(
            model=settings.model,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIStatusError as e:
        log.warning("Claude call failed (%s); using DEMO fallback", e.status_code)
        return None
    except anthropic.APIConnectionError:
        log.warning("Claude unreachable; using DEMO fallback")
        return None
    if response.stop_reason == "refusal":
        log.warning("Claude declined; using DEMO fallback")
        return None
    return response.parsed_output


# --- tool use (agent steps that search for themselves) -----------------------------

ToolCall = Callable[[str, dict], str]  # (tool name, arguments) -> result text for the model
MAX_TOOL_TURNS = 6


def run_tools(prompt: str, tools: list[dict], call: ToolCall, may_search: Callable[[], bool]) -> tuple[str | None, str]:
    """Lets the LLM call `tools` (OpenAI function format) until it answers in text.
    `may_search` turns tools off once the step's search cap is reached, so the model must answer.
    Groq, then Gemini, then Claude. Returns (answer, provider) or (None, why)."""
    reasons = []
    for name, loop in (("Groq", _tools_groq if _groq else None), ("Gemini", _tools_gemini if _gemini else None),
                       ("Claude", _tools_claude if _claude else None)):
        if loop is None:
            continue
        text, why = loop(prompt, tools, call, may_search)
        if text:
            return text, name
        reasons.append(why)
    return None, "; ".join(r for r in reasons if r) or "no LLM key set"


def _tools_groq(prompt: str, tools: list[dict], call: ToolCall, may_search: Callable[[], bool]) -> tuple[str | None, str | None]:
    why = "Groq unavailable"
    for model in [m for m in (settings.groq_model, settings.groq_fallback_model) if m]:
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
        try:
            for _ in range(MAX_TOOL_TURNS):
                # At the cap, ask for the answer with no tools offered: with tool_choice="none" gpt-oss
                # sometimes calls a tool anyway, and Groq rejects the whole turn.
                more = may_search()
                if not more:
                    messages.append({"role": "user", "content": "No more searches. Answer now, in the format asked, "
                                     "using only what the searches returned."})
                r = _groq.chat.completions.create(
                    model=model, messages=messages, reasoning_effort="low", include_reasoning=False,
                    **({"tools": tools, "tool_choice": "auto"} if more else {}),
                )
                m = r.choices[0].message
                if not m.tool_calls:
                    return m.content, None
                messages.append({"role": "assistant", "content": m.content or "", "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                    for c in m.tool_calls]})
                for c in m.tool_calls:
                    try:
                        args = json.loads(c.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = None
                    result = call(c.function.name, args) if isinstance(args, dict) else "Invalid JSON arguments."
                    messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
            why = f"Groq ({model}) kept searching without answering"
        except groq.RateLimitError:
            why = f"Groq rate limit on {model}"
        except groq.APIStatusError as e:
            why = f"Groq error {e.status_code} on {model}"
            log.warning("%s: %s", why, str(e)[:200])
        except (groq.APIConnectionError, groq.APITimeoutError):
            return None, "Groq unreachable"
        log.warning("%s; trying the next model", why)
    return None, why


def _tools_gemini(prompt: str, tools: list[dict], call: ToolCall, may_search: Callable[[], bool]) -> tuple[str | None, str | None]:
    declarations = [genai_types.FunctionDeclaration(
        name=t["function"]["name"], description=t["function"]["description"],
        parameters_json_schema=t["function"]["parameters"]) for t in tools]
    why = "Gemini unavailable"
    for model in [m for m in (settings.gemini_model, settings.gemini_fallback_model) if m]:
        contents = [genai_types.Content(role="user", parts=[genai_types.Part(text=prompt)])]
        try:
            for _ in range(MAX_TOOL_TURNS):
                mode = "AUTO" if may_search() else "NONE"
                config = genai_types.GenerateContentConfig(
                    system_instruction=SYSTEM,
                    tools=[genai_types.Tool(function_declarations=declarations)],
                    tool_config=genai_types.ToolConfig(function_calling_config=genai_types.FunctionCallingConfig(mode=mode)),
                    thinking_config=genai_types.ThinkingConfig(thinking_level=genai_types.ThinkingLevel.LOW),
                    automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
                )
                r = _gemini.models.generate_content(model=model, contents=contents, config=config)
                calls = r.function_calls or []
                if not calls:
                    return r.text, None
                contents.append(r.candidates[0].content)  # as returned: keeps Gemini's thought signatures
                contents.append(genai_types.Content(role="user", parts=[
                    genai_types.Part.from_function_response(name=c.name, response={"result": call(c.name, dict(c.args or {}))})
                    for c in calls]))
            why = f"Gemini ({model}) kept searching without answering"
        except genai_errors.ServerError as e:
            why = f"Gemini busy ({e.code})"
        except genai_errors.ClientError as e:
            why = "Gemini free-tier daily quota used up" if e.code == 429 else f"Gemini error {e.code}"
        log.warning("%s on %s; trying the next model", why, model)
    return None, why


def _tools_claude(prompt: str, tools: list[dict], call: ToolCall, may_search: Callable[[], bool]) -> tuple[str | None, str | None]:
    specs = [{"name": t["function"]["name"], "description": t["function"]["description"],
              "input_schema": t["function"]["parameters"]} for t in tools]
    messages: list[dict] = [{"role": "user", "content": prompt}]
    try:
        for _ in range(MAX_TOOL_TURNS):
            r = _claude.beta.messages.create(
                model=settings.model, max_tokens=16000, system=SYSTEM, tools=specs, messages=messages,
                tool_choice={"type": "auto" if may_search() else "none"},
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            if r.stop_reason == "refusal":
                return None, "Claude declined"
            uses = [b for b in r.content if b.type == "tool_use"]
            if r.stop_reason != "tool_use" or not uses:
                return "".join(b.text for b in r.content if b.type == "text"), None
            messages.append({"role": "assistant", "content": r.content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": call(b.name, b.input)} for b in uses]})
        return None, "Claude kept searching without answering"
    except anthropic.APIStatusError as e:
        log.warning("Claude tool call failed (%s)", e.status_code)
        return None, f"Claude error {e.status_code}"
    except anthropic.APIConnectionError:
        return None, "Claude unreachable"


def _evidence(lead: Lead, research: list[str] | None = None) -> str:
    lines = [f"- [{s.source}] {s.title} ({s.posted or 'date unknown'}): {s.snippet}" for s in lead.signals]
    found = ("\nWhat our research found (web, news and Maps searches):\n" + "\n".join(f"- {n}" for n in research)
             if research else "")
    return f"Lead: {lead.name}\nLocation: {lead.location or 'unknown'}\nEvidence:\n" + "\n".join(lines) + found


# --- assess -----------------------------------------------------------------

VERTICALS = [
    ("real estate", "real estate agencies"),
    ("cafe", "cafes"),
    ("restaurant", "restaurants"),
    ("clinic", "clinics"),
    ("gym", "gyms"),
    ("coaching", "coaching institutes"),
    ("salon", "salons"),
    ("hotel", "hotels"),
]
CITIES = ["Pune", "Bengaluru", "Mumbai", "Delhi", "Hyderabad", "Chennai", "Kolkata", "Ahmedabad",
          "New York", "Austin", "Chicago", "Los Angeles", "Seattle", "Boston", "Miami", "Atlanta"]
DEFAULT_CITY = {"INR": "Bengaluru", "USD": "New York"}


def assess(lead: Lead, research: list[str] | None = None) -> Assessment:
    prompt = (
        f"{_evidence(lead, research)}\n\nAssess this lead for our research service. Pick the single "
        "business category and a city in the lead's own country whose research would most help them, "
        "so we can do a free 10-business sample for them before pitching."
    )
    return _parse(prompt, Assessment) or _demo_assess(lead)


def _demo_assess(lead: Lead) -> Assessment:
    text = " ".join(f"{s.title} {s.snippet} {s.location or ''}" for s in lead.signals).lower()
    category = next((c for k, c in VERTICALS if k in text), "local businesses")
    city = next((c for c in CITIES if c.lower() in text), DEFAULT_CITY.get(lead.currency, "New York"))
    return Assessment(
        fit_score=lead.intent_score,
        need_summary=f"{lead.name} is actively looking for research on {category} in {city}.",
        sample_category=category,
        sample_city=city,
        angle=lead.signals[0].title,
    )


# --- pitch ------------------------------------------------------------------


def write_pitch(lead: Lead, a: Assessment, sample: list[dict], research: list[str] | None = None) -> Pitch:
    preview = "\n".join(f"- {r['name']} | {r.get('rating')}★ ({r.get('reviews')} reviews)" for r in sample[:3])
    prompt = (
        f"{_evidence(lead, research)}\n\nOur read: {a.need_summary}\nAngle: {a.angle}\n"
        f"We already researched a free sample of {len(sample)} {a.sample_category} in "
        f"{a.sample_city} for them (attached as CSV). First rows:\n{preview}\n\n"
        "Write a cold email under 120 words: reference their exact need, mention the attached free "
        "sample, offer to research their whole market, end with one low-friction question. No hype, no emojis. "
        "Separate paragraphs with blank lines."
    )
    return _parse(prompt, Pitch) or _demo_pitch(lead, a, sample, preview)


def _demo_pitch(lead: Lead, a: Assessment, sample: list[dict], preview: str) -> Pitch:
    first = re.sub(r"\s*[–-].*$", "", a.angle)
    return Pitch(
        subject=f"{len(sample)} {a.sample_category} in {a.sample_city} — free sample for {lead.name}",
        body=(
            f"Hi {lead.name} team,\n\n"
            f"Saw your post about \"{first}\". We research local markets from live search "
            f"results, so I did a free sample of {len(sample)} {a.sample_category} in "
            f"{a.sample_city} for you (CSV attached):\n\n{preview}\n\n"
            "The full research can cover every area you care about, with phone, website, rating "
            "and review counts, plus what customers mention in reviews for the top businesses.\n\n"
            "Would a city-wide version be useful for your team this month?\n\n"
            f"— {settings.signature}"
        ),
    )


# --- replies and negotiation ------------------------------------------------


def _thread_text(thread: list[dict]) -> str:
    return "\n\n".join(f"[{'us' if m['dir'] == 'out' else 'lead'}] {m['body']}" for m in thread[-6:])


def read_reply(lead: Lead, thread: list[dict], text: str) -> ReplyRead:
    prompt = (
        f"Lead: {lead.name}\nConversation so far:\n{_thread_text(thread)}\n\n"
        f"Their new reply:\n{text}\n\nClassify the reply."
    )
    return _parse(prompt, ReplyRead) or _demo_read(text)


def _demo_read(text: str) -> ReplyRead:
    t = text.lower()
    m = re.search(r"(\d[\d,]*)\s*(rows|leads|records|contacts|entries|listings|businesses)", t)
    rows = int(m.group(1).replace(",", "")) if m else None
    if re.search(r"\b(stop|unsubscribe|remove me|don't email|do not email)\b", t):
        intent = "unsubscribe"
    elif re.search(r"\b(deal|go ahead|confirm|send (the )?invoice|let's do|lets do|accepted?|works for us)\b", t):
        intent = "accept"
    elif re.search(r"\b(expensive|discount|budget|too much|costly|lower|cheaper|best price)\b", t):
        intent = "price_objection"
    elif re.search(r"\b(not now|later|next (month|quarter)|no need|not interested)\b", t):
        intent = "not_now"
    elif re.search(r"\b(how much|price|cost|rate|quote)\b", t) or rows:
        intent = "interested"
    else:
        intent = "question"
    return ReplyRead(intent=intent, requested_rows=rows, summary=text.strip().splitlines()[0][:140])


def write_reply(lead: Lead, thread: list[dict], read: ReplyRead, quote: dict | None) -> EmailDraft:
    cur = (quote or {}).get("currency", lead.currency)
    terms = (
        f"Quote to state exactly, in {cur} (do not change any number or currency): {quote['rows']} rows, "
        f"list {money(quote['list_price'], cur)}, discount {quote['discount_pct']}%, "
        f"price {money(quote['price'], cur)} ({quote['note']})."
        if quote
        else "Do not mention any price."
    )
    goals = {
        "interested": "Give the quote, say what we research for each business, ask if they want to go ahead.",
        "question": "Answer their question from what we offer, include the quote, ask one question back.",
        "price_objection": "Acknowledge the budget concern, present the revised quote as our best, "
        "explain the value briefly, ask if it works.",
        "accept": "Thank them, confirm the agreed scope and price, say the invoice and delivery timeline "
        "(48 hours after payment) follow next.",
        "not_now": "Thank them, no pressure, offer to check back next quarter. No price.",
    }
    prompt = (
        f"Lead: {lead.name}\nConversation so far:\n{_thread_text(thread)}\n\n"
        f"Their reply was classified as '{read.intent}': {read.summary}\n{terms}\n"
        f"Goal: {goals.get(read.intent, goals['question'])}\n"
        f"Write the email body under 110 words: greeting line, 2-3 short paragraphs separated by blank "
        f"lines, then '— {settings.signature}' on its own line. No subject line."
    )
    return _parse(prompt, EmailDraft) or _demo_reply(lead, read, quote)


def _demo_reply(lead: Lead, read: ReplyRead, quote: dict | None) -> EmailDraft:
    q = quote or {}
    cur = q.get("currency", lead.currency)
    price_line = (
        f"For researching {q.get('rows', 0):,} businesses (name, phone, website, address, rating, reviews) "
        f"the fee is {money(q.get('price', 0), cur)}"
        + (f" — {q['discount_pct']}% off our list price of {money(q['list_price'], cur)}" if q.get("discount_pct") else "")
        + "."
    )
    bodies = {
        "interested": f"Thanks for getting back to us!\n\n{price_line}\n\nShall we go ahead?",
        "question": f"Happy to help. {price_line}\n\nWhich areas should the research cover?",
        "price_objection": f"Understood — budgets matter. {q.get('note', '').capitalize()}: {price_line}\n\n"
        "That's the best we can do on this scope. Does that work for you?",
        "accept": f"Great, thank you! Confirming research on {q.get('rows', 0):,} businesses for "
        f"{money(q.get('price', 0), cur)}. We'll send the invoice shortly, and the research follows within "
        "48 hours of payment.",
        "not_now": "No problem at all, thanks for letting us know. Mind if we check back next quarter?",
    }
    body = bodies.get(read.intent, bodies["question"])
    return EmailDraft(body=f"Hi {lead.name} team,\n\n{body}\n\n— {settings.signature}")


# --- batched classification ------------------------------------------------------

# How many items the last batched call left unanswered (they fell back to rules or "review").
last_unanswered = 0


def _batched(items: list, size: int, prompt_for, schema, effort: str = "low") -> dict:
    """Asks about `items` in small batches (big lists make models skip entries), re-asks once
    about anything skipped, and returns {item index: answer object}."""
    global last_fallback, last_provider, last_unanswered
    answers, providers, failures = {}, set(), []

    def ask(indices):
        for start in range(0, len(indices), size):
            chunk = indices[start:start + size]
            parsed = _parse(prompt_for([items[i] for i in chunk]), schema, effort)
            if parsed is None:
                failures.append(last_fallback)
                continue
            providers.add(last_provider)
            for item in parsed.items:
                if 0 <= item.index < len(chunk):
                    answers[chunk[item.index]] = item

    ask(list(range(len(items))))
    missing = [i for i in range(len(items)) if i not in answers]
    if answers and missing:
        ask(missing)
    last_unanswered = len(items) - len(answers)
    last_provider = ", ".join(sorted(p for p in providers if p)) or None
    last_fallback = None if answers else ("; ".join(f for f in failures if f) or "no LLM key set")
    return answers


# --- news headlines -> company names -------------------------------------------


class HeadlineCompany(BaseModel):
    index: int = Field(description="The headline's number from the list")
    company: str | None = Field(
        description="The single company the headline is about (e.g. the startup that raised money), "
        "exactly as written. null for roundups, multiple companies, or no company."
    )


class HeadlineCompanies(BaseModel):
    items: list[HeadlineCompany]


FUNDING_VERBS = r"(?:raises|secures|bags|gets|lands|closes|nets|receives|picks up|announces)"


def extract_companies(headlines: list[str]) -> list[str | None]:
    """Asks the LLM in batches of 25; any headline it can't answer falls back to a pattern rule."""
    if not headlines:
        return []

    def prompt_for(batch):
        return (
            "For each news headline, name the single company it is about (usually the startup that "
            "raised funding). Return null when it is a roundup, names several companies, or names none. "
            f"Answer every one of the {len(batch)} headlines, using its number as the index.\n\n"
            + "\n".join(f"{i}. {h}" for i, h in enumerate(batch))
        )

    answers = _batched(headlines, 25, prompt_for, HeadlineCompanies)
    return [
        ((answers[i].company or "").strip() or None) if i in answers else _demo_company(h)
        for i, h in enumerate(headlines)
    ]


# Words that describe a company rather than name it ("Fintech Paysy", "Pune-based Acme").
DESCRIPTORS = {
    "fintech", "edtech", "agritech", "healthtech", "insurtech", "proptech", "deeptech", "cleantech",
    "foodtech", "logistics", "d2c", "saas", "ai", "startup", "indian", "india", "bengaluru", "bangalore",
    "mumbai", "delhi", "gurugram", "gurgaon", "noida", "pune", "hyderabad", "chennai", "kolkata",
    "ahmedabad", "jaipur", "kochi",
}


def _demo_company(headline: str) -> str | None:
    # The capitalised name right before a funding verb: "... brand Brewkart Cafe raises ₹12 crore"
    m = re.search(rf"((?:[A-Z][\w&.'-]*\s){{0,4}}[A-Z][\w&.'-]*)\s+{FUNDING_VERBS}\b", headline)
    if not m:
        return None
    words = m.group(1).split()
    while words and (words[0].lower() in DESCRIPTORS or re.search(r"-(backed|based|led)$", words[0], re.I)):
        words.pop(0)
    return " ".join(words) or None


# --- is this company actually a buyer? --------------------------------------------

BuyerKind = Literal["buyer", "job_board", "data_vendor", "enterprise", "unclear"]


class CompanyVerdict(BaseModel):
    index: int
    kind: BuyerKind = Field(
        description="buyer: an SMB or startup that could plausibly buy a ₹1,500–₹10,000 / $49–$500 business "
        "list; job_board: job site, staffing, recruitment or job aggregator; data_vendor: itself sells "
        "market research, data or lead generation (a competitor); enterprise: large multinational or "
        "listed company unlikely to buy a small list; unclear: not enough information"
    )
    reason: str = Field(description="Under 12 words, grounded in the name and evidence")


class CompanyVerdicts(BaseModel):
    items: list[CompanyVerdict]


def vet_companies(candidates: list[dict]) -> list[tuple[str, str]]:
    """Classifies candidates ({"name", "evidence"}) in batches of 15. Returns (kind, reason) each;
    anything the LLM leaves unanswered is "unclear" (kept for human review), never "buyer"."""
    if not candidates:
        return []
    answers = _batched(candidates, 15, _vet_prompt, CompanyVerdicts, effort="medium")
    if not answers:
        return [_demo_vet(c["name"]) for c in candidates]
    return [(answers[i].kind, answers[i].reason) if i in answers else ("unclear", "not classified by the AI; review")
            for i in range(len(candidates))]


def _vet_prompt(batch: list[dict]) -> str:
    listing = "\n".join(f"{i}. {c['name']} — {c['evidence']}" for i, c in enumerate(batch))
    return (
        "We sell a research service on local businesses (shops, agencies, clinics, competitors) "
        "built on public search results, priced ₹1,500–₹10,000 in India or $49–$500 in the US. For each "
        "company below, classify it. Judge the company itself, not the job it is hiring for:\n"
        "- job_board: recruitment, staffing, HR services or job sites, even when the posting is a sales role "
        "(they hire on behalf of other companies)\n"
        "- data_vendor: the company itself sells market research, data, analytics or lead generation\n"
        "- enterprise: well-known multinationals, listed companies, banks, universities, government bodies "
        "and investors/VC firms; they don't buy ₹1,500 research jobs\n"
        "- buyer: a small or mid-sized business or startup whose own sales or growth would use our research\n"
        "- unclear: you can't tell from the name and evidence\n"
        f"Answer every one of the {len(batch)} companies, using its number as the index.\n\n" + listing
    )


def _demo_vet(name: str) -> tuple[str, str]:
    n = name.lower()
    if re.search(r"\b(jobs?|hire|hiring|staff\w*|recruit\w*|careers?|talent|placements?)\b|hire|jobmatch", n):
        return "job_board", "name suggests a job board or staffing firm"
    if re.search(r"\b(research|data|analytics|insights?|leads?)\b", n):
        return "data_vendor", "name suggests it sells research or data itself"
    return "unclear", "AI check unavailable; review before pitching"
