"""A virtual "today" column: the latest snapshot revalued at today's numbers.

Nothing here is written to the portfolio, and nothing here is a snapshot. A real
snapshot is a fixed historical record -- once written, its numbers must not move
-- whereas this is a live view, recomputed on load and never stored. It is built
as a :class:`~snapshots.Snapshot` anyway, deliberately, so that the FX
conversion and the strict totalling are not re-implemented for it. Only the
*quantities* are carried over from the last real snapshot; the prices attached
to them and the whole rate matrix are fetched fresh for the day being asked for.

The consequence worth being honest about: today can only re-value what actually
has a market price. A bank balance, a loan and a credit card are amounts, not
quantities, so they pass through at face value. That is correct rather than a
limitation -- there is nothing to mark to market -- but it is recorded per
holding in :func:`virtual_snapshot`'s notes so the page can say which numbers are
live and which are simply carried forward.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from fx import FxRates, fetch_fx
from prices import PriceError, close_on
from snapshots import Asset, Portfolio, Snapshot

#: A holding was re-marked at today's close.
MARKED = "marked"
#: A holding is an amount, not a quantity, so it passed through at face value.
FACE = "face"
#: A holding is a quantity but has no close today, so the cell is blank.
UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class HoldingNote:
    """How one holding got its number in the live view."""

    ref: str
    kind: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.ref}: {self.kind} {self.detail}".strip()


@dataclass(frozen=True)
class TodayMarks:
    """Fresh rates and closes for one day, plus whatever could not be fetched."""

    on: str
    fx: dict[str, dict[str, float]]
    fx_date: str
    closes: dict[str, tuple[float, str]] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def fx_stale(self) -> bool:
        """True when the rate source had no rates of its own for ``on``.

        Routine, not exceptional: the ECB only publishes on working days, so a
        weekend ask lands on Friday's rates. Still worth knowing, because the
        total then mixes Friday's rates with today's closes.
        """
        return self.fx_date != self.on


def fetch_marks(
    currencies: list[str],
    tickers: list[str],
    on: str,
    *,
    fetch_rates=fetch_fx,
    fetch_close=close_on,
    attempts: int = 2,
) -> TodayMarks:
    """Fetch the closes and rates needed to revalue ``on``.

    Closes are fetched one ticker at a time rather than through
    :func:`prices.close_on_many`, which raises as soon as any ticker fails. Here
    a single delisted holding should cost its own row the number, not blank the
    column and the total with it.

    Each close gets ``attempts`` tries. One unreachable ticker otherwise costs
    the whole column its total, and the usual cause is a transient timeout or a
    rate limit rather than a ticker that genuinely has no price. Two tries is
    enough to ride out one blip without turning a dead ticker into a slow one.

    Raises :class:`fx.FxError` if the rates cannot be had: without a full matrix
    there is no column at all, as opposed to a column with holes in it.
    """
    day = dt.date.fromisoformat(on)
    rates: FxRates = fetch_rates(day, list(currencies))

    closes: dict[str, tuple[float, str]] = {}
    failures: dict[str, str] = {}
    for ticker in sorted({t.strip().upper() for t in tickers if t and t.strip()}):
        for attempt in range(max(1, attempts)):
            try:
                quote = fetch_close(ticker, day)
            except PriceError as exc:
                if attempt == max(1, attempts) - 1:
                    failures[ticker] = str(exc)
                continue
            closes[ticker] = (quote.price, quote.price_date)
            break

    return TodayMarks(
        on=on,
        fx=rates.rates,
        fx_date=rates.rate_date,
        closes=closes,
        failures=failures,
    )


def tickers_to_mark(portfolio: Portfolio, latest: Snapshot) -> list[str]:
    """Tickers whose close is needed to revalue ``latest``, deduplicated.

    Only holdings actually held as a quantity need a price, and several holdings
    are usually the same shares in different accounts -- hence deduplicated, so
    one price serves all three rows.
    """
    wanted = set()
    for holding in latest.assets:
        if holding.units is None:
            continue
        spec = portfolio.spec(holding.ref)
        if spec.ticker:
            wanted.add(spec.ticker)
    return sorted(wanted)


def virtual_snapshot(
    portfolio: Portfolio, latest: Snapshot, marks: TodayMarks
) -> tuple[Snapshot, list[HoldingNote]]:
    """``latest``'s holdings, re-marked and re-converted for ``marks.on``.

    Returns the synthetic snapshot plus a :class:`HoldingNote` per holding saying
    how its number was arrived at, because "today" cannot mark everything to
    market and the page should not imply otherwise.
    """
    assets: list[Asset] = []
    notes: list[HoldingNote] = []

    for holding in latest.assets:
        spec = portfolio.spec(holding.ref)

        if holding.units is not None and spec.ticker:
            close = marks.closes.get(spec.ticker)
            if close is None:
                # Deliberately left unresolved rather than pinned to the last
                # snapshot's price: today's rates against October's close would
                # be a number that looks computed and is not comparable to
                # anything. Blank, and flagged.
                assets.append(Asset(ref=holding.ref, units=holding.units))
                notes.append(HoldingNote(holding.ref, UNRESOLVED, f"no close for {spec.ticker}"))
            else:
                price, price_date = close
                assets.append(Asset(ref=holding.ref, units=holding.units, price=price))
                notes.append(HoldingNote(holding.ref, MARKED, f"{price} on {price_date}"))
            continue

        # An amount, not a quantity. Carried at face value, which is the only
        # thing there is to carry.
        assets.append(Asset(ref=holding.ref, value=holding.resolved_value))
        notes.append(HoldingNote(holding.ref, FACE, "an amount, not a quantity"))

    snapshot = Snapshot(date=marks.on, fx=marks.fx, assets=assets)
    return snapshot, notes
