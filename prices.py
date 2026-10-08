"""Share price lookup.

Prices come from Yahoo Finance's public chart endpoint. No API key, and it
returns a daily close per date plus the currency the instrument trades in.

A snapshot needs the close on (or before) its own date, because a snapshot is a
fixed historical record: once written, its numbers must not change. Closes for
past dates are therefore cached forever, while the last couple of days are
always re-fetched since those closes are still settling.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

import requests

DEFAULT_CACHE_PATH = Path(__file__).resolve().parent / "data" / "prices_cache.json"
DEFAULT_URL = "https://query1.finance.yahoo.com/v8/finance/chart"

TIMEOUT_SECONDS = 15
USER_AGENT = "Mozilla/5.0 (compatible; finance-tracker/1.0)"

#: How far back to search for a close when the exact date has none, which is
#: what happens on weekends and public holidays.
LOOKBACK_DAYS = 14

#: Closes this recent are still settling, so don't trust the cache for them.
VOLATILE_DAYS = 2

#: Decimal places kept for a price. Enough for any real close, without carrying
#: the float noise that comes back from the source (e.g. 512.9000244140625).
PRICE_DP = 4


class PriceError(RuntimeError):
    """A price could not be resolved."""


@dataclass(frozen=True)
class Quote:
    """A resolved close price."""

    ticker: str
    price: float
    price_date: str
    currency: str | None
    source: str = "api"

    @property
    def stale(self) -> bool:
        """True when the close we used is older than the date we asked for."""
        return self.price_date < self.as_of

    as_of: str = ""


def _cache_read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _cache_write(cache: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=1, sort_keys=True)
    except OSError:
        pass  # a cache we can't write is not fatal


def _covered(ranges: list[tuple[str, str]], start_iso: str, end_iso: str) -> bool:
    """True when [start_iso, end_iso] lies wholly inside an already-fetched span."""
    return any(r_start <= start_iso and end_iso <= r_end for r_start, r_end in ranges)


def _add_range(
    ranges: list[tuple[str, str]], start_iso: str, end_iso: str
) -> list[tuple[str, str]]:
    """Record a fetched window, merging it into any span it touches."""
    if start_iso > end_iso:
        return ranges
    spans = sorted([tuple(r) for r in ranges] + [(start_iso, end_iso)])
    merged: list[list[str]] = []
    for span_start, span_end in spans:
        if merged and _next_day(merged[-1][1]) >= span_start:
            merged[-1][1] = max(merged[-1][1], span_end)
        else:
            merged.append([span_start, span_end])
    return [(a, b) for a, b in merged]


def _next_day(day_iso: str) -> str:
    return (dt.date.fromisoformat(day_iso) + dt.timedelta(days=1)).isoformat()


def _fetch_window(
    ticker: str, start: dt.date, end: dt.date, url: str
) -> tuple[dict[str, float], str | None]:
    """Daily closes within [start, end], plus the trading currency.

    Yahoo can hand back rows just outside the requested range, so anything
    outside is dropped rather than stored.
    """
    # period2 is exclusive, so ask for the day after the last date we want.
    period1 = dt.datetime.combine(start, dt.time(), tzinfo=dt.UTC)
    period2 = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(), tzinfo=dt.UTC)

    try:
        response = requests.get(
            f"{url.rstrip('/')}/{ticker}",
            params={
                "period1": int(period1.timestamp()),
                "period2": int(period2.timestamp()),
                "interval": "1d",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise PriceError(
            f"{ticker}: could not reach the price source ({exc.__class__.__name__})"
        ) from None

    try:
        payload = response.json()
    except ValueError:
        raise PriceError(
            f"{ticker}: price source returned HTTP {response.status_code}, not JSON"
        ) from None

    chart = payload.get("chart") or {}
    if chart.get("error"):
        description = (chart["error"] or {}).get("description", "unknown error")
        raise PriceError(f"{ticker}: {description}")
    if response.status_code != 200:
        raise PriceError(f"{ticker}: price source returned HTTP {response.status_code}")

    results = chart.get("result") or []
    if not results:
        raise PriceError(f"{ticker}: no price data returned")

    result = results[0]
    currency = (result.get("meta") or {}).get("currency")

    timestamps = result.get("timestamp") or []
    quotes = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = quotes.get("close") or []

    out: dict[str, float] = {}
    # The two arrays are the same series from one response; a holiday shows up
    # as a null close at its own index, not as a missing timestamp. Ragged
    # input is truncated rather than raised on, which is how it always behaved.
    for stamp, close in zip(timestamps, closes, strict=False):
        if close is None:
            continue  # a market holiday inside the window
        day = dt.datetime.fromtimestamp(stamp, dt.UTC).date()
        if not start <= day <= end:
            continue
        out[day.isoformat()] = round(float(close), PRICE_DP)
    return out, currency


def close_on(
    ticker: str,
    on: str | dt.date,
    url: str = DEFAULT_URL,
    cache_path: Path | str = DEFAULT_CACHE_PATH,
    lookback_days: int = LOOKBACK_DAYS,
) -> Quote:
    """The close for `ticker` on `on`, or on the most recent earlier date.

    The search never reaches further back than `lookback_days`, so a holiday
    stretch is an error rather than an arbitrarily old price.

    Cached closes are only reused for a date range that lies entirely in settled
    history. Recent closes are deliberately never cached, because they can still
    be revised -- so a cache entry cannot be trusted to hold the newest trading
    day, and reusing one for a recent date would quietly return an older price.
    Anything near the present is fetched fresh instead. Past snapshots, which is
    the common case, are served from cache.

    Settled is not on its own sufficient. The cache is a union of windows fetched
    on demand, so it can have holes: fetch the fortnight before 2026-09-01 and
    nothing for September, and a later query for 2026-09-01 would find an earlier
    cached day, decide it had an answer, and return a close that is days out of
    date. So each ticker records the spans it has actually fetched, and a query is
    served from cache only when one of those spans covers the whole window.

    Raises :class:`PriceError` when the ticker has no data at all in the window.
    """
    if not ticker or not ticker.strip():
        raise PriceError("ticker must be a non-empty string")
    ticker = ticker.strip().upper()

    target = _as_date(on, ticker)
    start = target - dt.timedelta(days=lookback_days)
    target_iso = target.isoformat()
    window_start = start.isoformat()
    cache = _cache_read(Path(cache_path))
    entry = cache.setdefault(ticker, {})
    currency = entry.get("currency")
    ranges = [tuple(r) for r in entry.get("_ranges", [])]

    volatile_from = (dt.date.today() - dt.timedelta(days=VOLATILE_DAYS)).isoformat()
    # Only settled days are written, so only settled days may be claimed as covered.
    cacheable_end = min(
        target_iso, (dt.date.today() - dt.timedelta(days=VOLATILE_DAYS + 1)).isoformat()
    )

    candidates: list[tuple[str, float]] = []
    if target_iso < volatile_from and _covered(ranges, window_start, cacheable_end):
        # Settled history and a window we have actually fetched end to end, so
        # the cache is a complete answer here.
        candidates = [
            (day, price)
            for day, price in entry.items()
            if isinstance(price, (int, float)) and window_start <= day <= target_iso
        ]

    if not candidates:
        fetched, currency = _fetch_window(ticker, start, target, url)
        for day, price in fetched.items():
            if day < volatile_from:
                entry[day] = price
        if cacheable_end >= window_start:
            ranges = _add_range(ranges, window_start, cacheable_end)
            entry["_ranges"] = [list(r) for r in ranges]
        if currency:
            entry["currency"] = currency
        _cache_write(cache, Path(cache_path))
        candidates = [(day, price) for day, price in fetched.items() if day <= target_iso]

    if not candidates:
        raise PriceError(
            f"{ticker}: no close on or before {target_iso} "
            f"(searched back {lookback_days} days from {window_start})"
        )

    day, price = max(candidates)
    return Quote(
        ticker=ticker,
        price=round(float(price), PRICE_DP),
        price_date=day,
        currency=currency,
        source="api",
        as_of=target_iso,
    )


def _as_date(on: str | dt.date, ticker: str) -> dt.date:
    if isinstance(on, dt.datetime):
        return on.date()
    if isinstance(on, dt.date):
        return on
    if isinstance(on, str):
        try:
            return dt.date.fromisoformat(on)
        except ValueError:
            raise PriceError(f"{ticker}: {on!r} is not a valid ISO date") from None
    raise PriceError(f"{ticker}: cannot use {type(on).__name__} as a date")


def close_on_many(tickers: list[str], on: str | dt.date, **kwargs) -> dict[str, Quote]:
    """Look up several tickers, collecting failures instead of stopping at the first."""
    quotes: dict[str, Quote] = {}
    errors: dict[str, str] = {}
    for ticker in tickers:
        try:
            quotes[ticker.strip().upper()] = close_on(ticker, on, **kwargs)
        except PriceError as exc:
            errors[ticker.strip().upper()] = str(exc)
    if errors:
        raise PriceError("; ".join(errors.values()))
    return quotes
