"""Tests for the virtual "today" revaluation, offline.

Every fetch is injected. Nothing here touches the network or the price cache,
which is the whole point: the live view is the one part of the app that *would*
reach out, so it needs to be the most thoroughly checked and the least allowed
to depend on a third party being up.
"""

import datetime as dt

import pytest
from helpers import check

import market
from fx import FxError, FxRates
from prices import PriceError
from snapshots import Asset, AssetSpec, Portfolio, Snapshot

ON = "2026-10-05"
DAY = dt.date.fromisoformat(ON)

# --- fixtures ---------------------------------------------------------------

SPECS = [
    AssetSpec(key="bank", type="cash", currency="AUD", name="Bank"),
    AssetSpec(key="loan", type="cash", currency="AUD", name="Loan"),
    AssetSpec(key="us-shares", type="stocks", currency="USD", name="Shares", ticker="AAA"),
    AssetSpec(key="asx-shares", type="stocks", currency="AUD", name="ASX", ticker="BBB"),
    AssetSpec(key="house", type="property", currency="AUD", name="House"),
]
LATEST = Snapshot(
    date="2026-10-01",
    fx={"AUD": {"AUD": 1.0, "USD": 0.65}, "USD": {"AUD": 1.5, "USD": 1.0}},
    assets=[
        Asset(ref="bank", value=1000.0),
        Asset(ref="loan", value=-400.0),
        Asset(ref="us-shares", units=10.0, price=20.0),  # 200 USD
        Asset(ref="asx-shares", units=5.0, price=8.0),  # 40 AUD
        Asset(ref="house", value=500000.0),
    ],
)
PORTFOLIO = Portfolio(
    currencies=["AUD", "USD"], specs=SPECS, snapshots=[LATEST], name="Test", handle="test"
)

RATES = FxRates(
    date=ON,
    rate_date=ON,
    rates={"AUD": {"AUD": 1.0, "USD": 0.70}, "USD": {"AUD": 1.4, "USD": 1.0}},
)


def rates_ok(day, currencies, **kwargs):
    assert isinstance(day, dt.date), f"fetch_rates got {type(day).__name__}, want a date"
    assert list(currencies) == ["AUD", "USD"], currencies
    return RATES


class Close:
    def __init__(self, ticker, price, price_date, **kwargs):
        self.ticker, self.price, self.price_date = ticker, price, price_date


@pytest.fixture
def asked():
    return []


@pytest.fixture
def closes_ok(asked):
    def fetch(ticker, day, **kwargs):
        asked.append((ticker, day))
        return Close(ticker, 30.0 if ticker == "AAA" else 9.0, ON)

    return fetch


# One bad ticker must not take the others with it.
def flaky(ticker, day, **kwargs):
    if ticker == "AAA":
        raise PriceError("AAA: could not reach the price source (Timeout)")
    return Close(ticker, 9.0, ON)


@pytest.fixture
def tries():
    return []


@pytest.fixture
def flaky_once(tries):
    def fetch(ticker, day, **kwargs):
        tries.append(ticker)
        if len(tries) == 1:
            raise PriceError("AAA: could not reach the price source (Timeout)")
        return Close(ticker, 30.0, ON)

    return fetch


@pytest.fixture
def always_fails(tries):
    def fetch(ticker, day, **kwargs):
        tries.append(ticker)
        raise PriceError(f"{ticker}: no close in the window")

    return fetch


def no_rates(day, currencies, **kwargs):
    raise FxError("no rates for 2026-10-05")


@pytest.fixture
def revalued(closes_ok):
    return market.virtual_snapshot(
        PORTFOLIO,
        LATEST,
        market.fetch_marks(
            ["AUD", "USD"], ["AAA", "BBB"], ON, fetch_rates=rates_ok, fetch_close=closes_ok
        ),
    )


# --- tickers_to_mark --------------------------------------------------------


def test_tickers_to_mark():
    """Only quantity holdings contribute tickers, and only once each."""
    check(
        "a quantity needs its ticker",
        market.tickers_to_mark(PORTFOLIO, LATEST) == ["AAA", "BBB"],
        str(market.tickers_to_mark(PORTFOLIO, LATEST)),
    )
    check(
        "...and nothing else does",
        market.tickers_to_mark(PORTFOLIO, LATEST) != ["AAA", "BBB", "bank", "house"],
    )

    bare = Snapshot(
        date=ON,
        fx=RATES.rates,
        assets=[Asset(ref="bank", value=1.0), Asset(ref="house", value=2.0)],
    )
    check(
        "an amount-only snapshot needs no prices at all",
        market.tickers_to_mark(PORTFOLIO, bare) == [],
    )

    dup = Snapshot(
        date=ON,
        fx=RATES.rates,
        assets=[
            Asset(ref="us-shares", units=1.0, price=20.0),
            Asset(ref="us-shares", units=2.0, price=20.0),
        ],
    )
    check(
        "the same shares in two accounts are fetched once",
        market.tickers_to_mark(PORTFOLIO, dup) == ["AAA"],
    )


# --- fetch_marks ------------------------------------------------------------


def test_fetch_marks_normalises_and_records(asked, closes_ok):
    """Closes are fetched once per normalised ticker and kept with their day."""
    marks = market.fetch_marks(
        ["AUD", "USD"], ["aaa", " AAA "], ON, fetch_rates=rates_ok, fetch_close=closes_ok
    )
    check(
        "tickers are normalised and deduplicated before fetching",
        [t for t, _ in asked] == ["AAA"],
        str(asked),
    )
    check(
        "the close is asked for as a date object, not a string",
        all(isinstance(d, dt.date) for _, d in asked),
        str(asked),
    )
    check(
        "a close is stored with the day it came from",
        marks.closes["AAA"] == (30.0, ON),
        str(marks.closes),
    )
    check(
        "rates are carried through with the day the source used",
        marks.fx == RATES.rates and marks.fx_date == ON,
    )
    check("rates from the requested day are not stale", marks.fx_stale is False)


def test_fetch_marks_reports_stale_weekend_rates(closes_ok):
    """A source rate date older than the requested day is reported as stale."""
    stale = market.fetch_marks(
        ["AUD", "USD"],
        [],
        "2026-10-04",
        fetch_rates=lambda *a, **k: FxRates(
            date="2026-10-04", rate_date="2026-10-02", rates=RATES.rates
        ),
        fetch_close=closes_ok,
    )
    check(
        "a weekend's rates are reported as stale, not as today",
        stale.fx_stale is True and stale.fx_date == "2026-10-02",
        str(stale.fx_date),
    )


def test_fetch_marks_survives_one_bad_ticker():
    """One unreachable ticker does not lose the other's price."""
    partial = market.fetch_marks(
        ["AUD", "USD"], ["AAA", "BBB"], ON, fetch_rates=rates_ok, fetch_close=flaky
    )
    check(
        "one unreachable ticker does not lose the other's price",
        partial.closes == {"BBB": (9.0, ON)},
        str(partial.closes),
    )
    check("...and is recorded as a failure", "AAA" in partial.failures, str(partial.failures))


def test_fetch_marks_retries_transient_failure(tries, flaky_once):
    """A transient failure is retried once before being believed."""
    retried = market.fetch_marks(
        ["AUD", "USD"], ["AAA"], ON, fetch_rates=rates_ok, fetch_close=flaky_once
    )
    check(
        "a transient failure is retried",
        retried.closes == {"AAA": (30.0, ON)} and not retried.failures,
        str(tries),
    )
    check("...and costs one extra call, not an unbounded loop", len(tries) == 2, str(tries))


def test_fetch_marks_gives_up_after_configured_tries(tries, always_fails):
    """A ticker that never succeeds gives up after the configured attempts."""
    dead = market.fetch_marks(
        ["AUD", "USD"], ["AAA"], ON, fetch_rates=rates_ok, fetch_close=always_fails, attempts=3
    )
    check(
        "a ticker that is genuinely gone gives up after the configured tries",
        dead.failures and len(tries) == 3,
        str(tries),
    )
    check("...and has no close", dead.closes == {})


def test_fetch_marks_raises_on_missing_rates(closes_ok):
    """A failed rate lookup raises rather than yielding a half-built column."""
    try:
        market.fetch_marks(["AUD", "USD"], ["AAA"], ON, fetch_rates=no_rates, fetch_close=closes_ok)
        check("missing rates raise rather than yield a half-built column", False)
    except FxError:
        check("missing rates raise rather than yield a half-built column", True)


# --- virtual_snapshot -------------------------------------------------------


def test_virtual_snapshot_revalues_holdings(revalued):
    """The live view is dated today and re-marks quantities at today's close."""
    good = revalued[0]
    check("the live view is dated today, not the snapshot's date", good.date == ON)
    check("it carries today's rates", good.fx["AUD"]["USD"] == 0.70)

    by_ref = {a.ref: a for a in good.assets}
    check(
        "quantities are kept exactly",
        by_ref["us-shares"].units == 10.0 and by_ref["asx-shares"].units == 5.0,
    )
    check(
        "and re-marked at today's close",
        by_ref["us-shares"].price == 30.0 and by_ref["asx-shares"].price == 9.0,
    )
    check(
        "an amount is carried at face value",
        by_ref["bank"].value == 1000.0 and by_ref["bank"].units is None,
    )
    check("a liability stays a liability", by_ref["loan"].value == -400.0)
    check("property is carried at face value too", by_ref["house"].value == 500000.0)

    check(
        "the USD holding is worth today's dollars",
        PORTFOLIO.value_in(good, "us-shares", "USD") == 300.0,
    )
    check("...and today's AUD", PORTFOLIO.value_in(good, "us-shares", "AUD") == 420.0)
    check(
        "the total adds up across currencies and signs",
        PORTFOLIO.total_in(good, "AUD") == 420.0 + 45.0 + 1000.0 - 400.0 + 500000.0,
        str(PORTFOLIO.total_in(good, "AUD")),
    )


def test_virtual_snapshot_records_notes(revalued):
    """Every holding gets a note explaining how it was valued."""
    notes = revalued[1]
    kinds = {n.ref: n.kind for n in notes}
    check("re-marked holdings say so", kinds["us-shares"] == market.MARKED, str(kinds))
    check("amounts say so", kinds["bank"] == market.FACE, str(kinds))
    check("every holding is accounted for", len(notes) == 5, str(len(notes)))
    check(
        "a re-marked note names the close it used",
        any(n.detail == "30.0 on 2026-10-05" for n in notes if n.kind == market.MARKED),
    )


def test_virtual_snapshot_leaves_missing_close_unresolved():
    """A holding with no close today is left blank, not re-priced from stale data."""
    missing = market.fetch_marks(
        ["AUD", "USD"], ["AAA", "BBB"], ON, fetch_rates=rates_ok, fetch_close=flaky
    )
    holed, hole_notes = market.virtual_snapshot(PORTFOLIO, LATEST, missing)
    held = {a.ref: a for a in holed.assets}
    check(
        "a holding with no close today is left unresolved",
        held["us-shares"].price is None and held["us-shares"].units == 10.0,
    )
    check(
        "...and reports itself unresolved",
        {n.ref: n.kind for n in hole_notes}["us-shares"] == market.UNRESOLVED,
    )
    check(
        "the strict total refuses rather than dropping the holding", "us-shares" in holed.unresolved
    )
    check("...while the other ticker is unaffected", held["asx-shares"].price == 9.0)
    try:
        PORTFOLIO.total_in(holed, "AUD")
        check("...so the total raises rather than being short by an unknown amount", False)
    except Exception as exc:  # noqa: BLE001
        check(
            "...so the total raises rather than being short by an unknown amount",
            "us-shares" in str(exc),
            str(exc),
        )


def test_virtual_snapshot_never_touches_real_snapshot(revalued):
    """Building the live view leaves the portfolio's real snapshots alone."""
    check(
        "the live view is not in the portfolio's snapshots",
        all(s.date != ON for s in PORTFOLIO.snapshots),
    )
    check(
        "the real snapshot is untouched by building a live view",
        LATEST.assets[2].price == 20.0,
        str(LATEST.assets[2].price),
    )
