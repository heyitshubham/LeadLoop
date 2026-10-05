"""Runtime settings, read once from the environment (.env supported)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    serpapi_key: str | None
    anthropic_key: str | None
    gemini_key: str | None
    groq_key: str | None
    force_demo: bool
    credit_budget: int
    discovery_refresh_hours: float  # discovery re-searches after this; cached results older than it are stale
    data_dir: Path
    fixtures_dir: Path
    send_mode: str  # sandbox (Mailpit) | live (real SMTP, e.g. Zoho)
    sandbox_smtp_port: int
    smtp_host: str
    smtp_port: int
    smtp_security: str  # ssl | starttls
    smtp_user: str | None
    smtp_password: str | None
    imap_host: str | None
    imap_port: int
    imap_user: str | None
    imap_password: str | None
    daily_send_cap: int
    sender: str
    signature: str
    markets: list[dict]  # [{"name", "gl", "currency"}], searched in this order
    default_currency: str  # for leads with no country (e.g. Reddit posters)
    pricing: dict  # currency -> {"per_row", "min_order"}
    default_rows: int
    max_discount_pct: int
    discount_step_pct: int
    max_followups: int
    followup_days: float
    fulfil_max_searches: int  # SerpApi searches one order may spend (pages + enrichment)
    fulfil_enrich_rows: int  # top rows that get review topics and a hiring check (2 searches each)
    serp_cost_per_search_usd: float  # what one search costs you, for the margin on each order
    usd_to_inr: float
    model: str
    gemini_model: str
    gemini_fallback_model: str | None
    groq_model: str
    groq_fallback_model: str | None

    @property
    def serp_live(self) -> bool:
        return bool(self.serpapi_key) and not self.force_demo

    @property
    def live_mail(self) -> bool:
        return self.send_mode == "live"

    @property
    def reads_inbox(self) -> bool:
        """Live mode reads replies over IMAP only when an IMAP host is set; otherwise you paste them in."""
        return self.live_mail and bool(self.imap_host)

    @property
    def llm_providers(self) -> list[str]:
        """Every LLM with a key, in the order they're tried: Groq, then Gemini, then Claude."""
        if self.force_demo:
            return []
        keys = {"groq": self.groq_key, "gemini": self.gemini_key, "claude": self.anthropic_key}
        return [name for name, key in keys.items() if key]

    @property
    def llm_provider(self) -> str | None:
        """The first LLM tried, or None (DEMO: rules and templates only)."""
        return (self.llm_providers or [None])[0]


# Google region code and currency for each market you sell into.
KNOWN_MARKETS = {
    "india": ("in", "INR"),
    "united states": ("us", "USD"),
    "united kingdom": ("uk", "USD"),
    "canada": ("ca", "USD"),
    "australia": ("au", "USD"),
    "singapore": ("sg", "USD"),
    "united arab emirates": ("ae", "USD"),
}


def _markets(value: str) -> list[dict]:
    out = []
    for name in (n.strip() for n in value.split(",") if n.strip()):
        gl, currency = KNOWN_MARKETS.get(name.lower(), (None, "USD"))
        out.append({"name": name, "gl": gl, "currency": currency})
    return out


def load_settings() -> Settings:
    data_dir = Path(os.getenv("LEADLOOP_DATA_DIR", BACKEND_DIR / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        serpapi_key=os.getenv("SERPAPI_API_KEY") or None,
        anthropic_key=os.getenv("ANTHROPIC_API_KEY") or None,
        gemini_key=os.getenv("GEMINI_API_KEY") or None,
        groq_key=os.getenv("GROQ_API_KEY") or None,
        force_demo=os.getenv("LEADLOOP_DEMO", "0") == "1",
        # Free SerpApi plan is 250 searches/month; keep headroom for the demo recording.
        credit_budget=int(os.getenv("LEADLOOP_CREDIT_BUDGET", "200")),
        # Discovery runs the same queries every time, so a never-expiring cache returns the same leads forever.
        discovery_refresh_hours=float(os.getenv("LEADLOOP_DISCOVERY_REFRESH_HOURS", "24")),
        data_dir=data_dir,
        fixtures_dir=BACKEND_DIR / "fixtures",
        send_mode=os.getenv("LEADLOOP_SEND_MODE", "sandbox"),
        sandbox_smtp_port=int(os.getenv("SANDBOX_SMTP_PORT", "1025")),
        smtp_host=os.getenv("SMTP_HOST", "smtp-relay.brevo.com"),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_security=os.getenv("SMTP_SECURITY", "starttls"),
        smtp_user=os.getenv("SMTP_USER") or None,
        smtp_password=os.getenv("SMTP_PASSWORD") or None,
        imap_host=os.getenv("IMAP_HOST", "") or None,
        imap_port=int(os.getenv("IMAP_PORT", "993")),
        # The inbox can differ from the sending account (e.g. send via Brevo, receive in Zoho).
        imap_user=os.getenv("IMAP_USER") or os.getenv("SMTP_USER") or None,
        imap_password=os.getenv("IMAP_PASSWORD") or os.getenv("SMTP_PASSWORD") or None,
        # New domains get flagged as spam if they send in bulk; ramp this up slowly.
        daily_send_cap=int(os.getenv("LEADLOOP_DAILY_SEND_CAP", "25")),
        sender=os.getenv("LEADLOOP_SENDER", "LeadLoop Data Desk <desk@leadloop.local>"),
        signature=os.getenv("LEADLOOP_SIGNATURE", "LeadLoop Data Desk"),
        markets=_markets(os.getenv("LEADLOOP_MARKETS", "India,United States")),
        default_currency=os.getenv("DEFAULT_CURRENCY", "USD"),
        # Pricing rules. Code computes every quote; the LLM only words it.
        pricing={
            "INR": {"per_row": float(os.getenv("PRICE_PER_ROW_INR", "4")), "min_order": int(os.getenv("MIN_ORDER_INR", "1500"))},
            "USD": {"per_row": float(os.getenv("PRICE_PER_ROW_USD", "0.10")), "min_order": int(os.getenv("MIN_ORDER_USD", "49"))},
        },
        default_rows=int(os.getenv("DEFAULT_ROWS", "500")),
        max_discount_pct=int(os.getenv("MAX_DISCOUNT_PCT", "20")),
        discount_step_pct=int(os.getenv("DISCOUNT_STEP_PCT", "10")),
        max_followups=int(os.getenv("MAX_FOLLOWUPS", "2")),
        followup_days=float(os.getenv("FOLLOWUP_AFTER_DAYS", "3")),
        fulfil_max_searches=int(os.getenv("FULFIL_MAX_SEARCHES", "50")),
        fulfil_enrich_rows=int(os.getenv("FULFIL_ENRICH_ROWS", "10")),
        # SerpApi Developer plan: $75 for 5,000 searches.
        serp_cost_per_search_usd=float(os.getenv("SERP_COST_PER_SEARCH_USD", "0.015")),
        usd_to_inr=float(os.getenv("USD_TO_INR", "88")),
        model=os.getenv("LEADLOOP_MODEL", "claude-opus-5-5"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        gemini_fallback_model=os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3-flash-preview") or None,
        # Free plan: 1,000 requests/day, 200K tokens/day, 8K tokens/minute — per model.
        groq_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
        groq_fallback_model=os.getenv("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b") or None,
    )


settings = load_settings()
