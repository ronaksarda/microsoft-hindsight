"""Currency conversion using European Central Bank reference rates (via frankfurter.dev).

* ``GET {base}/{YYYY-MM-DD}?base=INR&symbols=USD`` returns the rate published for
  that day, or the closest earlier working day (weekends, holidays).
* ``GET {base}/latest`` is used for today and future dates.
* Rates are cached in SQLite, so a pair/day is fetched once and the app keeps
  working offline with the most recent cached rate (flagged as ``cached``).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import httpx

from app import clock, local_store
from app.config import settings

logger = logging.getLogger("grantanchor.fx")

_transport: httpx.AsyncBaseTransport | None = None  # tests inject a MockTransport
_currencies: tuple[float, dict[str, str]] | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS fx_rates (
    day TEXT NOT NULL, base TEXT NOT NULL, quote TEXT NOT NULL, rate REAL NOT NULL,
    rate_date TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY (day, base, quote)
);
"""


class FxError(Exception):
    pass


@dataclass
class FxQuote:
    base: str
    quote: str
    rate: float
    rate_date: str
    source: str  # ecb | cached | manual | same

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def set_transport(t: httpx.AsyncBaseTransport | None) -> None:
    global _transport, _currencies
    _transport = t
    _currencies = None


def _db():
    c = local_store.conn()
    c.executescript(SCHEMA)
    return c


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.fx_base_url, timeout=8.0, transport=_transport, follow_redirects=True)


async def get_rate(base: str, quote: str, day: date | None = None) -> FxQuote:
    base, quote = base.upper(), quote.upper()
    if base == quote:
        return FxQuote(base, quote, 1.0, (day or clock.now().date()).isoformat(), "same")
    today = clock.now().date()
    use_latest = day is None or day >= today
    key = today.isoformat() if use_latest else day.isoformat()
    c = _db()
    row = c.execute(
        "SELECT rate, rate_date FROM fx_rates WHERE day=? AND base=? AND quote=?", (key, base, quote)
    ).fetchone()
    if row:
        return FxQuote(base, quote, row["rate"], row["rate_date"], "ecb")
    try:
        async with _client() as client:
            resp = await client.get("/latest" if use_latest else f"/{key}", params={"base": base, "symbols": quote})
        if resp.status_code == 404 or resp.status_code == 422:
            raise FxError(f"No exchange rate available for {base} to {quote}.")
        resp.raise_for_status()
        data = resp.json()
        rate = float(data["rates"][quote])
        rate_date = str(data.get("date") or key)
    except FxError:
        raise
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        fallback = c.execute(
            "SELECT rate, rate_date FROM fx_rates WHERE base=? AND quote=? ORDER BY rate_date DESC LIMIT 1",
            (base, quote),
        ).fetchone()
        if fallback:
            logger.warning("fx fetch failed (%s); using cached %s->%s", exc, base, quote)
            return FxQuote(base, quote, fallback["rate"], fallback["rate_date"], "cached")
        raise FxError(
            f"Couldn't get the {base} to {quote} exchange rate right now. Enter the rate yourself or try again."
        ) from exc
    c.execute(
        "INSERT OR REPLACE INTO fx_rates(day, base, quote, rate, rate_date, fetched_at) VALUES(?, ?, ?, ?, ?, ?)",
        (key, base, quote, rate, rate_date, clock.now_iso()),
    )
    return FxQuote(base, quote, rate, rate_date, "ecb")


async def convert(
    amount: float, from_cur: str, to_cur: str, day: date | None, manual_rate: float | None = None
) -> tuple[float, FxQuote]:
    if manual_rate is not None:
        q = FxQuote(
            from_cur.upper(), to_cur.upper(), float(manual_rate), (day or clock.now().date()).isoformat(), "manual"
        )
    else:
        q = await get_rate(from_cur, to_cur, day)
    return round(amount * q.rate, 2), q


async def currencies() -> dict[str, str]:
    """ISO code -> name for every currency the rate source supports (cached for a day)."""
    global _currencies
    if _currencies and time.time() - _currencies[0] < 86400:
        return _currencies[1]
    try:
        async with _client() as client:
            resp = await client.get("/currencies")
        resp.raise_for_status()
        data = {str(k).upper(): str(v) for k, v in resp.json().items()}
        local_store._set_meta("fx_currencies", json.dumps(data))
    except (httpx.HTTPError, ValueError) as exc:
        cached = local_store._meta("fx_currencies")
        if not cached:
            raise FxError("Couldn't load the currency list.") from exc
        data = json.loads(cached)
    _currencies = (time.time(), data)
    return data
