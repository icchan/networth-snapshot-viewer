"""Exchange rate lookup for a given date.

Rates come from Frankfurter, a free public API serving European Central Bank
reference rates. No API key, no account.

The one wrinkle worth knowing: the ECB publishes on working days only, so asking
for a Saturday returns that Friday's rates. We don't pretend otherwise --
:class:`FxRates` records the date the source actually used, so a snapshot can say
which day its rates came from.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import requests

from snapshots import DEFAULT_FX_URL, SnapshotError, check_currencies, check_fx

DEFAULT_URL = DEFAULT_FX_URL
TIMEOUT_SECONDS = 15
USER_AGENT = "Mozilla/5.0 (compatible; finance-model/1.0)"
EARLIEST_DATE = "1999-01-04"  # the ECB reference series starts here


class FxError(SnapshotError):
    """A rate lookup failed.

    Subclasses :class:`SnapshotError` so that one ``except`` catches rate
    problems, price problems and bad arguments together, the same way
    :class:`PortfolioError` does.
    """


class FxNotFound(FxError):
    """The source had nothing for that request.

    Only this failure is ambiguous -- it means either "no such currency" or "no
    such date" -- so it is the one worth gathering across every source before
    deciding what to say. Everything else is reported as it happens.
    """


@dataclass(frozen=True)
class FxRates:
    """A complete matrix for one date.

    ``date`` is what you asked for; ``rate_date`` is the day the source actually
    used. They differ only when the requested date had no rates of its own.
    """

    date: str
    rate_date: str
    rates: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def stale(self) -> bool:
        return self.rate_date != self.date


def _as_date(value: object) -> str:
    if not isinstance(value, dt.date):
        raise FxError(f"date must be a date object, got {type(value).__name__}")
    return value.isoformat()


def _fetch_row(base: str, targets: list[str], date: str, url: str) -> tuple[dict[str, float], str]:
    """Fetch one source's row. Returns the row and the date the source used."""
    try:
        response = requests.get(
            f"{url.rstrip('/')}/{date}",
            params={"from": base, "to": ",".join(targets)},
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise FxError(f"could not reach the rate source for {base}->{date}: {exc}") from exc

    if response.status_code != 200:
        # 404 covers both an unknown currency and a date outside the series.
        # Which one it is gets worked out by the caller, across all bases.
        raise FxNotFound(f"{base}->{date}: {response.status_code} {response.text.strip()[:80]}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise FxError(f"{base}->{date}: rate source sent something that isn't JSON") from exc

    rates = payload.get("rates")
    used = payload.get("date")
    if not isinstance(rates, dict) or not isinstance(used, str):
        raise FxError(f"{base}->{date}: unexpected reply {payload!r:.120}")
    if not rates:
        raise FxError(f"{base}->{date}: rate source returned no rates")

    missing = [code for code in targets if code not in rates]
    if missing:
        raise FxError(f"{base}->{date}: rate source omitted {', '.join(missing)}")

    row = {base: 1.0}
    row.update({code: float(rates[code]) for code in targets})
    return row, used


_SUPPORTED: dict[str, str] | None = None


def supported_currencies(*, url: str = DEFAULT_URL) -> dict[str, str] | None:
    """The source's currency list, fetched once per process, or None if unavailable.

    Only used to give a precise error for an unsupported currency. An unknown
    code poisons a whole request, so without this the failure looks like a bad
    date. Never fatal: if the list can't be fetched we carry on and let the
    per-row errors speak.
    """
    global _SUPPORTED
    if _SUPPORTED is not None:
        return _SUPPORTED
    try:
        response = requests.get(
            f"{url.rstrip('/')}/currencies",
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            payload = response.json()
            if isinstance(payload, dict) and payload:
                _SUPPORTED = {str(k).upper(): str(v) for k, v in payload.items()}
    except (requests.RequestException, ValueError):
        pass
    return _SUPPORTED


def fetch_fx(date: object, currencies: list[str], *, url: str = DEFAULT_URL) -> FxRates:
    """Fetch every ordered pair for ``date``.

    Each currency is fetched as its own source, so the matrix holds the rate as
    the source published it rather than an inverted guess. The result is checked
    against the same rules a hand-written matrix must pass, so anything this
    returns is always safe to store in a snapshot.
    """
    target = _as_date(date)
    try:
        codes = check_currencies(currencies)
    except SnapshotError as exc:
        raise FxError(str(exc)) from exc

    known = supported_currencies(url=url)
    if known is not None:
        unknown = [code for code in codes if code not in known]
        if unknown:
            raise FxError(
                f"the rate source does not support {', '.join(unknown)} "
                f"({target}). It carries ECB currencies only: {_currency_names(known)}."
            )

    if len(codes) == 1:
        # Nothing to look up, and asking would only risk a 404.
        return FxRates(date=target, rate_date=target, rates={codes[0]: {codes[0]: 1.0}})

    matrix: dict[str, dict[str, float]] = {}
    used_dates: list[str] = []
    refused: dict[str, str] = {}

    for base in codes:
        targets = [code for code in codes if code != base]
        try:
            row, used = _fetch_row(base, targets, target, url)
        except FxNotFound as exc:
            refused[base] = str(exc)
            continue
        matrix[base] = row
        used_dates.append(used)

    if refused:
        _raise_for(refused, len(codes), target)

    # Identity is 1.0 by definition; the source leaves the base out of its reply.
    for code in codes:
        matrix[code][code] = 1.0

    # If any source landed on an older day, report the oldest so `stale` is honest.
    check_fx(matrix, codes, f"fetch_fx({target})")
    return FxRates(date=target, rate_date=min(used_dates), rates=matrix)


def _raise_for(refused: dict[str, str], total: int, target: str) -> None:
    """Work out whether the date or the currencies were the problem.

    Every base failing points at the date; only some failing points at those
    specific currencies. Either way, say which.
    """
    if len(refused) == total:
        raise FxError(
            f"no rates for {target}. The source covers working days from "
            f"{EARLIEST_DATE} onwards, so check the date is not in the future "
            f"or before that."
        )
    codes = ", ".join(sorted(refused))
    raise FxError(
        f"the rate source does not support {codes} (asked for {target}). "
        f"It carries ECB currencies only."
    )


def _currency_names(known: dict[str, str], limit: int = 10) -> str:
    """A short, readable summary of what the source does carry."""
    codes = sorted(known)
    if len(codes) <= limit:
        return ", ".join(codes)
    return f"{', '.join(codes[:limit])} and {len(codes) - limit} more"
