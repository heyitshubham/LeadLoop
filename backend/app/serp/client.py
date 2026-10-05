"""Single entry point for every SerpApi call.

- Every live response is cached on disk, keyed by engine + params, so a query is
  paid for once and the demo can be replayed offline.
- A ledger counts live searches and refuses calls past the configured budget.
- Without a SerpApi key (or with LEADLOOP_DEMO=1) responses come from fixtures/<engine>.json.
"""

import hashlib
import json
import threading
import time
from pathlib import Path

import serpapi

from app.config import Settings, settings as default_settings


class CreditBudgetExceeded(RuntimeError):
    pass


class SerpClient:
    def __init__(self, cfg: Settings = default_settings):
        self.cfg = cfg
        self.cache_dir = cfg.data_dir / "serp_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path = cfg.data_dir / "credits.json"
        self._lock = threading.Lock()
        self._client = serpapi.Client(api_key=cfg.serpapi_key) if cfg.serp_live else None

    @property
    def mode(self) -> str:
        return "LIVE" if self._client else "DEMO"

    def credits_used(self) -> int:
        if not self.ledger_path.exists():
            return 0
        return json.loads(self.ledger_path.read_text())["used"]

    def search(self, engine: str, **params) -> dict:
        return self.fetch(engine, **params)[0]

    def fetch(self, engine: str, max_age_hours: float | None = None, **params) -> tuple[dict, str]:
        """Like search(), plus where the result came from: "cache", "demo" or "live" (1 credit).

        With max_age_hours, an older cached result is searched again live. Without a key there is
        nothing to refresh from, so the stale copy is still served."""
        params = {"engine": engine, **params}
        key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:24]
        cached = self.cache_dir / f"{engine}-{key}.json"
        if cached.exists() and (
            max_age_hours is None or not self._client or time.time() - cached.stat().st_mtime < max_age_hours * 3600
        ):
            return json.loads(cached.read_text()), "cache"

        if not self._client:
            return self._fixture(engine), "demo"

        with self._lock:
            used = self.credits_used()
            if used >= self.cfg.credit_budget:
                raise CreditBudgetExceeded(
                    f"SerpApi budget of {self.cfg.credit_budget} searches reached"
                )
            result = dict(self._client.search(params))
            self.ledger_path.write_text(json.dumps({"used": used + 1}))
        cached.write_text(json.dumps(result))
        return result, "live"

    def _fixture(self, engine: str) -> dict:
        path: Path = self.cfg.fixtures_dir / f"{engine}.json"
        return json.loads(path.read_text()) if path.exists() else {}


serp = SerpClient()
