"""Deterministic negotiation ladder. The LLM never picks a number; it only words these.

Each price objection moves one step down the ladder:
  list price -> step% off -> ... -> max% off -> same max discount on a smaller scope
so the agent can never go below the floor you configure. Every quote is in the lead's
currency (INR or USD), with its own per-row price and minimum order.
"""

from pydantic import BaseModel

from app.config import Settings, settings as default_settings

SYMBOLS = {"INR": "₹", "USD": "$"}


class Quote(BaseModel):
    currency: str
    rows: int
    list_price: int
    discount_pct: int
    price: int
    note: str  # how this quote differs from the previous one, for the email and audit trail


def money(amount: int | float, currency: str) -> str:
    """₹3,200 / $120 — Indian digit grouping for INR."""
    n = round(amount)
    if currency == "INR":
        s = str(abs(n))
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        body = ",".join(([head] if head else []) + groups + [tail]) if head or groups else tail
        return f"₹{body}"
    return f"{SYMBOLS.get(currency, currency + ' ')}{n:,}"


def quote(rows: int | None, objections: int, currency: str = "INR", cfg: Settings = default_settings) -> Quote:
    rule = cfg.pricing.get(currency) or cfg.pricing["USD"]
    per_row, min_order = rule["per_row"], rule["min_order"]
    rows = rows or cfg.default_rows
    discount = min(objections * cfg.discount_step_pct, cfg.max_discount_pct)
    steps_at_max = cfg.max_discount_pct // cfg.discount_step_pct if cfg.discount_step_pct else 0
    note = "list price" if discount == 0 else f"{discount}% off after negotiation"

    if objections > steps_at_max:
        # Out of discount room: keep the best price per row, offer fewer rows instead.
        rows = max(rows // 2, int(min_order // per_row))
        note = f"{discount}% off is our floor, so this is a smaller starter scope"

    list_price = round(max(min_order, rows * per_row))
    floor = round(min_order * (100 - cfg.max_discount_pct) / 100)
    price = max(round(list_price * (100 - discount) / 100), floor)
    return Quote(currency=currency, rows=rows, list_price=list_price, discount_pct=discount, price=price, note=note)
