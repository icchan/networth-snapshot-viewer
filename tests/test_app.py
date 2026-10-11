"""Checks for the Streamlit page.

Streamlit re-runs the whole script on every interaction, so there is no function
to import and unit-test directly. :class:`AppTest` runs the real script against
the real data and hands back the widgets and the rendered tables, which is
enough to catch the failures that actually matter: a crash on load, a total that
disagrees with the model, or a column that stopped being a date.
"""

import datetime as dt
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pytest
import streamlit as st
from helpers import check
from streamlit.proto.WidgetStates_pb2 import WidgetStates
from streamlit.testing.v1 import AppTest

import market
import prices
from fx import FxError, FxRates
from labels import TYPE_EMOJI
from labels import flag as flag_of
from labels import row_label as label_of
from snapshots import load

APP = Path(__file__).resolve().parent.parent / "app.py"


# The page's "Today" column is the one thing that would reach the internet. These
# tests must not depend on Yahoo or the ECB being up, or on what they say today,
# so the fetch is replaced. Prices come back doubled, which makes a re-marked
# holding distinguishable from one carried at face value.
MARKS: dict = {"fail": False, "drop": set()}

#: Rates the fake hands back, standing in for the ECB. Deliberately hardcoded
#: rather than read from the real portfolio, so the suite does not depend on the
#: file under test or on the network.
FAKE_FX = {
    "AUD": {"AUD": 1.0, "JPY": 100.0, "USD": 0.65},
    "JPY": {"JPY": 1.0, "AUD": 0.01, "USD": 0.0065},
    "USD": {"USD": 1.0, "AUD": 1.5385, "JPY": 153.85},
}
FAKE_CLOSE = 20.0


def fake_fetch_marks(currencies, tickers, on, **kwargs):
    """Stand in for Yahoo and the ECB.

    Doubling is not the point -- an implausible but fixed close is enough to make
    a re-marked holding distinguishable from one carried at face value. Any
    currency the page asks for is covered, so a fixture with an unusual currency
    still renders instead of failing the conversion.
    """
    if MARKS["fail"]:
        raise FxError(f"no rates for {on}")

    codes = list(currencies)
    fx = {}
    for base in codes:
        row = {quote: FAKE_FX.get(base, {}).get(quote, 1.0) for quote in codes}
        row[base] = 1.0
        fx[base] = row

    closes = {t: (FAKE_CLOSE, on) for t in tickers if t not in MARKS["drop"]}
    return market.TodayMarks(
        on=on,
        fx=fx,
        fx_date=on,
        closes=closes,
        failures={t: f"{t}: no close in the window" for t in MARKS["drop"]},
    )


@pytest.fixture(autouse=True)
def fake_network(monkeypatch):
    """Replace the one network seam, restoring it after every test."""
    monkeypatch.setattr(market, "fetch_marks", fake_fetch_marks)


@pytest.fixture(autouse=True)
def clean_marks():
    """Restore the mark toggles a test may set, so nothing leaks between tests."""
    yield
    MARKS["fail"] = False
    MARKS["drop"] = set()


@pytest.fixture
def personal():
    """The committed personal portfolio, as the page loads it."""
    return load("personal")


@pytest.fixture
def dates(personal):
    """Snapshot dates, newest first -- the table's own ordering."""
    return sorted((s.date for s in personal.snapshots), reverse=True)


@pytest.fixture
def ordered(personal):
    """The catalogue in the page's currency-then-type order."""
    return sorted(personal.specs, key=lambda spec: (spec.currency, spec.type))


@pytest.fixture
def latest(personal, dates):
    """The newest snapshot, which the live column re-marks."""
    return personal.by_date(dates[0])


@pytest.fixture
def fresh(personal):
    """One clean run of the page over ``personal`` and both rendered tables."""
    at = run()
    return SimpleNamespace(
        at=at,
        portfolio=personal,
        assets=table_records(asset_frame(at)),
        currency=table_records(currency_frame(at)),
        currency_styles=currency_frame(at).proto.arrow_data.styler.styles,
    )


def run():
    # The page memoizes today's fetch, so the cache is dropped between runs --
    # otherwise a test that changes what the fake returns would be served the
    # previous test's answer.
    st.cache_data.clear()
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    return at


def table_records(frame):
    """The rendered table as a list of dicts, whatever the input shape was."""
    value = frame.value
    return value.to_dict("records") if hasattr(value, "to_dict") else list(value)


def asset_frame(test):
    return next(
        frame
        for frame in test.dataframe
        if {"In total", "Asset", "How"} <= set(table_records(frame)[0])
    )


def snapshot_entry_frame(test):
    return next(
        frame
        for frame in test.dataframe
        if {"Asset", "Held", "Entry", "Value", "Units", "Price override"}
        <= set(table_records(frame)[0])
    )


def currency_frame(test):
    return next(frame for frame in test.dataframe if "Date" in table_records(frame)[0])


def workings_frame(test):
    return next(frame for frame in test.dataframe if "Snapshot" in table_records(frame)[0])


def workings_records(test) -> list[dict]:
    """The open workings table's rows, in render order."""
    return table_records(workings_frame(test))


def press(test, row: int) -> None:
    """Click one row's button, the way the frontend would.

    AppTest has no API for it: the button's widget is registered inside the
    editor and rendered nowhere in the element tree, so there is no node to
    poke -- and a plain ``session_state`` write does nothing, because the
    callback only fires for fresh widget state. What the frontend actually
    sends is exactly that: the id the editor published for the column plus
    the row's JSON. This builds that and hands it to the runner, the same
    path ``AppTest.run()`` takes with the states it collects from visible
    widgets.
    """
    widget_id = dict(asset_frame(test).proto.button_click_widgets)["How"]
    states = WidgetStates()
    state = states.widgets.add()
    state.id = widget_id
    state.string_trigger_value.data = json.dumps({"row": row, "label": ":material/info:"})
    test._run(widget_state=states)


def styled_rows(styles: str, grey_only: bool = True) -> set[int]:
    """Row positions the page has applied styling to."""
    # By default, only match rows that have grey styling (for backwards compat)
    # But for live row tinting, we need any styling
    rows = set()
    for block in styles.split("}"):
        if grey_only and "rgba(128,128,128,0.12)" not in block:
            continue
        for rm in re.findall(r"_row(\d+)_col", block):
            rows.add(int(rm))
    return rows


def is_blank(value):
    """True for a blank cell: ``None`` or NaN."""
    return value is None or (isinstance(value, float) and value != value)


def test_page_loads_cleanly(fresh):
    """The page renders the real portfolio without error or warning."""
    at = fresh.at
    check("the app runs without raising", not at.exception, str([e.value for e in at.exception]))
    check("and reports no errors", not at.error, str([e.value for e in at.error]))
    check("and no warnings on clean data", not at.warning, str([w.value for w in at.warning]))
    check(
        "the estimated net worth metric is under the portfolio title",
        len(at.metric) == 1 and at.metric[0].label == "Estimated Current Net Worth",
        str([(metric.label, metric.value) for metric in at.metric]),
    )
    check(
        "the table explanation starts collapsed",
        {item.label: item.proto.expanded for item in at.expander}
        == {"Enter or edit snapshot": False, "About this table": False},
        str([(item.label, item.proto.expanded) for item in at.expander]),
    )
    check(
        "the snapshot form includes the full asset catalogue",
        len(table_records(snapshot_entry_frame(at))) == len(fresh.portfolio.specs),
        str(table_records(snapshot_entry_frame(at))),
    )


# --- the assets table ---------------------------------------------------------


def test_assets_table(fresh, dates, ordered):
    """The assets table's shape, ordering and whole-unit formatting."""
    at = fresh.at
    assets_table = fresh.assets
    snapshots_newest_first = dates

    check(
        "form and two summary tables are rendered", len(at.dataframe) == 3, str(len(at.dataframe))
    )
    check(
        "the total is the first row",
        assets_table[0]["Asset"].startswith("Total"),
        assets_table[0]["Asset"],
    )
    check(
        "the total names the selected currency",
        assets_table[0]["Asset"].endswith("(AUD)"),
        assets_table[0]["Asset"],
    )
    asset_cols = [c for c in assets_table[0] if c not in ("Asset", "In total", "How")]
    check(
        "the tick column leads, so the toggle sits beside the assets",
        next(iter(assets_table[0])) == "In total",
        str(list(assets_table[0])[:2]),
    )
    columns = list(assets_table[0])
    check(
        "the workings button follows the asset name",
        columns.index("How") == columns.index("Asset") + 1,
        str(columns),
    )
    check(
        "the live column leads the amounts, then snapshots run newest first",
        asset_cols == ["Today", *snapshots_newest_first],
        str(asset_cols),
    )
    check(
        "the estimated current net worth matches the Today total",
        at.metric[0].value == f"AUD {assets_table[0]['Today']:,}",
        f"{at.metric[0].value} vs {assets_table[0]['Today']}",
    )
    check(
        "one row per catalogue entry, in currency-then-type order",
        [r["Asset"] for r in assets_table[1:]]
        == [f"{TYPE_EMOJI[s.type]} {flag_of(s.currency)} {s.label}" for s in ordered],
        str([r["Asset"] for r in assets_table[1:]][:3]),
    )

    # The table is handed a pandas Styler, which both greys excluded rows and takes
    # over display from the grid's number formatting -- so the strings in
    # `display_values` are what actually reaches the screen. Whole units with
    # thousands separators are the page's contract (see the README); a "5.0" or a
    # "None" leaking through would mean the Styler's formatter is wrong.
    displayed = (
        pa.ipc.open_stream(asset_frame(at).proto.arrow_data.styler.display_values)
        .read_all()
        .to_pydict()
    )
    amount_cols = [c for c in displayed if c not in ("Asset", "In total", "How")]
    odd = [
        v for col in amount_cols for v in displayed[col] if v and not re.fullmatch(r"-?[\d,]+", v)
    ]
    check(
        "amounts reach the screen whole, with separators, and blanks stay blank",
        not odd,
        str(odd[:6]),
    )


# --- the workings column ------------------------------------------------------


def test_workings_column(fresh):
    """The workings button follows the asset name, before the amount columns."""
    at = fresh.at
    assets_table = fresh.assets

    # The total has no workings of its own, and every asset row offers the panel.
    check(
        "every asset row offers it, and only the total goes without",
        assets_table[0]["How"] == ""
        and all(r["How"] == ":material/info:" for r in assets_table[1:]),
        str([r["How"] for r in assets_table[:3]]),
    )
    check(
        "the button is registered as a clickable widget",
        "How" in dict(asset_frame(at).proto.button_click_widgets),
        str(dict(asset_frame(at).proto.button_click_widgets)),
    )
    check(
        "a fresh run opens no workings panel",
        not any("was valued" in m.value for m in at.markdown),
        str([m.value for m in at.markdown]),
    )
    check(
        "the page points at the button",
        any("one line per date" in c.value for c in at.caption),
        str([c.value for c in at.caption][-3:]),
    )

    check(
        "no row is greyed out while nothing is excluded",
        not styled_rows(asset_frame(at).proto.arrow_data.styler.styles),
        asset_frame(at).proto.arrow_data.styler.styles,
    )
    check(
        "the total row has a distinct highlighted style",
        {
            int(row)
            for block in asset_frame(at).proto.arrow_data.styler.styles.split("}")
            if "rgba(255,193,7,0.2)" in block
            for row in re.findall(r"_row(\d+)_col", block)
        }
        == {0},
        asset_frame(at).proto.arrow_data.styler.styles,
    )
    bold_cells = {
        (int(row), int(column))
        for block in asset_frame(at).proto.arrow_data.styler.styles.split("}")
        if "font-weight: bold" in block
        for row, column in re.findall(r"_row(\d+)_col(\d+)", block)
    }
    check(
        "only the total row's Today cell is bold",
        bold_cells == {(0, list(fresh.assets[0]).index("Today"))},
        str(bold_cells),
    )


# --- the ordering itself ------------------------------------------------------


def test_ordering(fresh, ordered):
    """Rows group by currency then type, keeping catalogue order within."""
    assets_table = fresh.assets
    portfolio = fresh.portfolio
    row_specs = ordered
    currencies = [spec.currency for spec in row_specs]
    check(
        "currencies are grouped, not interleaved", currencies == sorted(currencies), str(currencies)
    )
    runs = [c for i, c in enumerate(currencies) if i == 0 or currencies[i - 1] != c]
    check("each currency forms one contiguous block", len(runs) == len(set(runs)), str(runs))
    for ccy in runs:
        types_in = [spec.type for spec in row_specs if spec.currency == ccy]
        check(
            f"types within {ccy} are grouped and ordered",
            types_in == sorted(types_in),
            str(types_in),
        )
    # Ties must fall back to catalogue order, or the same two accounts could swap
    # places between runs and quietly lose their grouping.
    for ccy in runs:
        for kind in sorted({spec.type for spec in row_specs if spec.currency == ccy}):
            in_file = [s.key for s in portfolio.specs if s.currency == ccy and s.type == kind]
            on_page = [spec.key for spec in row_specs if spec.currency == ccy and spec.type == kind]
            check(
                f"{ccy}/{kind} keeps catalogue order", in_file == on_page, f"{on_page} vs {in_file}"
            )
    check(
        "the total row is still first",
        assets_table[0]["Asset"].startswith("Total"),
        assets_table[0]["Asset"],
    )
    check(
        "the row count still matches the catalogue",
        len(assets_table) == len(portfolio.specs) + 1,
        str(len(assets_table)),
    )


# --- the marks themselves -----------------------------------------------------


def test_marks(fresh, ordered, dates, latest):
    """Rows carry type emoji and currency flag; totals come from the model."""
    assets_table = fresh.assets
    currency_table = fresh.currency
    portfolio = fresh.portfolio
    row_specs = ordered
    snapshots_newest_first = dates
    labels = [r["Asset"] for r in assets_table[1:]]
    marked = dict(zip((s.key for s in row_specs), labels, strict=True))

    # The flags are the part that can be wrong rather than merely unattractive.
    check(
        "a currency that names one country gets that flag",
        flag_of("AUD") == "🇦🇺" and flag_of("JPY") == "🇯🇵" and flag_of("USD") == "🇺🇸",
        f"{flag_of('AUD')} {flag_of('JPY')} {flag_of('USD')}",
    )
    check(
        "a currency that names no country gets no flag",
        flag_of("XAU") == "" and flag_of("") == "" and flag_of("  ") == "",
        repr(flag_of("XAU")),
    )
    check(
        "the lookup is case- and space-insensitive",
        flag_of(" aud ") == flag_of("AUD"),
        repr(flag_of(" aud ")),
    )

    check(
        "every type in the catalogue has a mark",
        all(s.type in TYPE_EMOJI for s in portfolio.specs),
        str(sorted({s.type for s in portfolio.specs} - set(TYPE_EMOJI))),
    )
    check(
        "every asset row is <emoji> <flag> <name>",
        all(
            r["Asset"] == f"{TYPE_EMOJI[s.type]} {flag_of(s.currency)} {s.label}"
            for r, s in zip(assets_table[1:], row_specs, strict=True)
        ),
        str(labels[:2]),
    )
    check(
        "...and still ends in the plain name",
        all(r["Asset"].endswith(s.label) for r, s in zip(assets_table[1:], row_specs, strict=True)),
    )
    check(
        "no row is left unflagged when its currency is known",
        all(
            flag_of(s.currency) in marked[s.key]
            for s in row_specs
            if s.currency in ("AUD", "JPY", "USD")
        ),
    )
    check(
        "the total row carries no marks -- it is not an asset",
        not any(ch in assets_table[0]["Asset"] for ch in "".join(TYPE_EMOJI.values())),
        assets_table[0]["Asset"],
    )
    check(
        "the second table is not marked up, only the first",
        all(not row["Date"].startswith(tuple(TYPE_EMOJI.values())) for row in currency_table),
    )
    check(
        "a held asset has a number, not a blank",
        assets_table[1][snapshots_newest_first[0]] is not None,
    )

    # The important one: the rendered total must be the model's total, not something
    # the page computed on its own.
    shown = assets_table[0][snapshots_newest_first[0]]
    model_total = portfolio.total_in(latest, "AUD")
    check(
        "the shown total is the model's total, to the nearest unit",
        abs(shown - model_total) < 0.5,
        f"page {shown}, model {model_total}",
    )

    # Deliberately not asserting `shown == round(model_total)`: that would just
    # restate the page's own rounding rule. Assert the property instead -- a whole
    # number that rounds back onto the model.
    check(
        "every amount is a whole number, never a float",
        all(
            isinstance(v, int) or v is None
            for row in assets_table
            for k, v in row.items()
            if k not in ("Asset", "In total", "How")
        ),
        str(
            [
                v
                for row in assets_table
                for k, v in row.items()
                if k not in ("Asset", "In total", "How")
                and v is not None
                and not isinstance(v, int)
            ]
        ),
    )
    check(
        "no total is off by more than half a unit",
        abs(shown - model_total) <= 0.5,
        f"{shown} vs {model_total}",
    )

    # Displayed cells are rounded, so the column reconciles with the model to within
    # half a unit per row rather than exactly.
    model_sum = sum(portfolio.value_in(latest, s.key, "AUD") or 0.0 for s in portfolio.specs)
    rows_shown = sum(
        assets_table[i + 1][snapshots_newest_first[0]] or 0 for i in range(len(portfolio.specs))
    )
    check(
        "the asset column sums to the shown total, within rounding",
        abs(model_sum - rows_shown) < len(portfolio.specs) * 0.5,
        f"model {model_sum:.2f}, rows shown {rows_shown}",
    )

    # A spot check computed independently of the page's helper.
    check(
        "a known holding rounds correctly",
        assets_table[1][snapshots_newest_first[0]]
        == round(portfolio.value_in(latest, "ubank-tomomi", "AUD")),
        str(assets_table[1][snapshots_newest_first[0]]),
    )
    check(
        "a negative holding rounds away from zero symmetrically",
        assets_table[3][snapshots_newest_first[0]] == -639852,
        str(assets_table[3][snapshots_newest_first[0]]),
    )


# --- the live column ----------------------------------------------------------


def test_live_column(fresh, ordered, dates):
    """The live re-marking: what moves, what is carried, and the caption."""
    at = fresh.at
    assets_table = fresh.assets
    snapshots_newest_first = dates
    row_specs = ordered

    # The fake marks double every close, so anything re-marked must move and anything
    # carried must not. That is what separates the two kinds of number on this row.
    TODAY_TOTAL = assets_table[0]["Today"]
    LATEST_TOTAL = assets_table[0][snapshots_newest_first[0]]

    check("the live column has its own total", TODAY_TOTAL is not None, str(TODAY_TOTAL))
    check(
        "revaluation actually moved the total",
        TODAY_TOTAL != LATEST_TOTAL,
        f"{TODAY_TOTAL} vs {LATEST_TOTAL}",
    )

    cash_row = next(r for r in assets_table[1:] if "UBank" in r["Asset"])
    check(
        "a cash balance is carried at face value, so it does not move",
        cash_row["Today"] == cash_row[snapshots_newest_first[0]],
        str(cash_row["Today"]),
    )

    # A "stocks" asset is not necessarily a quantity: Commsec is an amount, so it
    # sits this out. Only the ticker-backed holdings are re-marked.
    tickered = {s.key for s in row_specs if s.type == "stocks" and s.ticker}
    share_rows = [r for r, s in zip(assets_table[1:], row_specs, strict=True) if s.key in tickered]
    check(
        "every share holding with a ticker was re-marked, so it moves",
        all(r["Today"] != r[snapshots_newest_first[0]] for r in share_rows),
        str([(r["Asset"], r["Today"], r[snapshots_newest_first[0]]) for r in share_rows]),
    )
    check("...and none of them is blank", all(not is_blank(r["Today"]) for r in share_rows))
    carried = next(r for r in assets_table[1:] if "Commsec" in r["Asset"])
    check(
        "a share account held as an amount is carried, like cash",
        carried["Today"] == carried[snapshots_newest_first[0]],
        str(carried["Today"]),
    )

    cells = [r["Today"] for r in assets_table[1:]]
    check(
        "the live total is the sum of its own cells, within rounding",
        abs(sum(v for v in cells if not is_blank(v)) - TODAY_TOTAL) <= len(cells),
        f"model {TODAY_TOTAL}, rows shown {sum(v for v in cells if not is_blank(v))}",
    )
    check(
        "and no live cell is zeroed when it should be a number",
        all(not is_blank(v) for v in cells),
        str([c for c in cells if is_blank(c)]),
    )

    live_caption = next((c.value for c in at.caption if "never written to the file" in c.value), "")
    check(
        "the live column says it is not a snapshot",
        live_caption and "not a snapshot" in live_caption,
    )
    check(
        "...and says it is never written to the file",
        live_caption and "never written to the file" in live_caption,
    )
    check(
        "...and explains what was carried at face value",
        live_caption and "face value" in live_caption,
    )
    check(
        "...and names the dates the numbers came from",
        (live_caption and "rates as at" in live_caption) or "no rates published" in live_caption,
    )


# --- the live column, when a price will not come -----------------------------


def test_live_column_missing_price(ordered, dates):
    """A ticker with no close leaves its cells and the total blank."""
    MARKS["drop"] = {"MSFT"}
    at = run()
    check(
        "a missing price does not take the page down",
        not at.exception,
        str([e.value for e in at.exception]),
    )
    recs = table_records(asset_frame(at))
    msft_rows = [
        r
        for r in recs[1:]
        if "Microsoft" in r["Asset"] or "Fidelity" in r["Asset"] or "Morgan" in r["Asset"]
    ]
    check(
        "a holding with no close today is blank, not carried forward",
        msft_rows and all(is_blank(r["Today"]) for r in msft_rows),
        str([(r["Asset"], r["Today"]) for r in msft_rows]),
    )
    check(
        "...while the snapshot columns are untouched",
        msft_rows and all(not is_blank(r[dates[0]]) for r in msft_rows),
        str([(r["Asset"], r[dates[0]]) for r in msft_rows]),
    )
    check(
        "...and the total is blank rather than short by an unknown amount",
        is_blank(recs[0]["Today"]),
        str(recs[0]["Today"]),
    )
    check(
        "the page says which prices did not come back",
        any("No close today" in w.value for w in at.warning),
        str([w.value[:90] for w in at.warning]),
    )
    # The panel still explains the blank: the live view knows its own reason.
    msft = next(s for s in ordered if s.key == "msft-ian")
    at.session_state["inspect_key"] = msft.key
    at.run()
    check(
        "the workings panel says why today's cell is blank",
        not at.exception
        and any("no close for MSFT" in row["Note"] for row in workings_records(at)),
        str([e.value for e in at.exception] or workings_records(at)),
    )


# --- the live column, when there are no rates at all -------------------------


def test_live_column_no_rates(dates, ordered):
    """No rates at all drops the live column rather than showing it empty."""
    MARKS["fail"] = True
    at = run()
    check(
        "missing rates do not take the page down",
        not at.exception,
        str([e.value for e in at.exception]),
    )
    cols = [c for c in table_records(asset_frame(at))[0] if c not in ("Asset", "In total", "How")]
    check("the live column is dropped rather than shown empty", cols == dates, str(cols))
    check(
        "the snapshots still render",
        len(table_records(asset_frame(at))) == 17,
        str(len(table_records(asset_frame(at)))),
    )
    check(
        "the page reports why the live column is gone",
        any("Today's column is unavailable" in w.value for w in at.warning),
        str([w.value[:90] for w in at.warning]),
    )
    check(
        "...and the live row is dropped from the totals table too",
        [r["Date"] for r in table_records(currency_frame(at))] == dates,
        str([r["Date"] for r in table_records(currency_frame(at))]),
    )
    check(
        "...so no row is tinted as live",
        not styled_rows(currency_frame(at).proto.arrow_data.styler.styles),
        currency_frame(at).proto.arrow_data.styler.styles,
    )
    # With no live column there is no row for it: the panel mirrors the table.
    at.session_state["inspect_key"] = ordered[0].key
    at.run()
    check(
        "the panel's rows are the snapshots, and only those",
        not at.exception and [row["Snapshot"] for row in workings_records(at)] == dates,
        str([e.value for e in at.exception] or workings_records(at)),
    )


# --- the per-currency table ---------------------------------------------------


def test_per_currency_table(fresh, dates):
    """The per-currency totals table, live row included."""
    assets_table = fresh.assets
    currency_table = fresh.currency
    currency_styles = fresh.currency_styles
    portfolio = fresh.portfolio
    snapshots_newest_first = dates
    TODAY = dt.date.today().isoformat()

    check(
        "the live row leads, then the snapshots run newest first",
        [r["Date"] for r in currency_table] == [f"{TODAY} (live)", *snapshots_newest_first],
        str([r["Date"] for r in currency_table]),
    )
    check(
        "the live row is tinted, and only it",
        styled_rows(currency_styles, grey_only=False) == {0}
        and "background-color:" in currency_styles,
        currency_styles,
    )
    check(
        "one column per currency in the file",
        [c for c in currency_table[0] if c != "Date"] == portfolio.currencies,
        str([c for c in currency_table[0] if c != "Date"]),
    )
    # The live row is checked separately: it is not in portfolio.snapshots, so
    # by_date cannot find it and a stored snapshot's total is not what it should be.
    for record in currency_table:
        snap = portfolio.by_date(record["Date"])
        if snap is None:
            check(
                f"{record['Date']} is not a stored snapshot",
                record["Date"].endswith(" (live)"),
                record["Date"],
            )
            continue
        for ccy in portfolio.currencies:
            want = portfolio.total_in(snap, ccy)
            check(
                f"total on {record['Date']} in {ccy} matches the model",
                record[ccy] is not None and abs(record[ccy] - want) <= 0.5,
                f"page {record[ccy]}, model {want}",
            )

    live_total = next((r for r in currency_table if r["Date"].endswith(" (live)")), None)
    if live_total is not None:
        # Cross-checking the two tables against each other: both call total_in, but
        # on independently assembled rows, so a disagreement means one is wrong.
        check(
            "the live row agrees with the live column above it",
            live_total["AUD"] is not None
            and assets_table[0]["Today"] is not None
            and abs(live_total["AUD"] - assets_table[0]["Today"]) <= 0.5,
            f"{live_total['AUD']} vs {assets_table[0]['Today']}",
        )
        check(
            "...in every currency, not just AUD",
            all(live_total[c] is not None for c in portfolio.currencies),
            str(live_total),
        )

    check(
        "the JPY column is whole units too, not just the others",
        all(isinstance(r["JPY"], int) for r in currency_table),
        str([type(r["JPY"]).__name__ for r in currency_table]),
    )


# --- ticking an asset out of the totals --------------------------------------


def test_ticking_assets_out(personal, ordered, dates, latest):
    """Ticking an asset out of the totals: tick, grey, heading and totals."""
    snapshots_newest_first = dates
    model_total = personal.total_in(latest, "AUD")
    base = personal.currencies[0]
    tick_spec = next(s for s in ordered if personal.value_in(latest, s.key, base) not in (None, 0))
    tick_row = ordered.index(tick_spec) + 1  # the total row leads the table

    ticked = run()
    check(
        "nothing is ticked out on a fresh run", not ticked.info, str([i.value for i in ticked.info])
    )
    check(
        "so the heading counts nothing out",
        table_records(asset_frame(ticked))[0]["Asset"] == f"Total ({base})",
        table_records(asset_frame(ticked))[0]["Asset"],
    )

    ticked.session_state["excluded"] = {tick_spec.key}
    ticked.run()
    check(
        "ticking an asset out does not take the page down",
        not ticked.exception,
        str([e.value for e in ticked.exception]),
    )
    rows = table_records(asset_frame(ticked))
    check(
        "the asset shows as unticked",
        rows[tick_row]["In total"] is False,
        str(rows[tick_row]["In total"]),
    )
    check(
        "...while every other asset stays ticked",
        all(r["In total"] is True for i, r in enumerate(rows) if i not in (0, tick_row)),
        str([(r["Asset"], r["In total"]) for r in rows if r["In total"] is not True]),
    )
    check(
        "the total row offers no tick to make",
        rows[0]["In total"] is None,
        str(rows[0]["In total"]),
    )
    css = asset_frame(ticked).proto.arrow_data.styler.styles
    check(
        "the row keeps its plain label -- the exclusion is not spelled out",
        rows[tick_row]["Asset"] == label_of(tick_spec),
        rows[tick_row]["Asset"],
    )
    check("the row is greyed out in place, and only that row", styled_rows(css) == {tick_row}, css)
    check(
        "and it still shows its own amounts",
        rows[tick_row][snapshots_newest_first[0]]
        == round(personal.value_in(latest, tick_spec.key, base)),
        str(rows[tick_row][snapshots_newest_first[0]]),
    )
    check(
        "the heading counts what is left out",
        rows[0]["Asset"] == f"Total excluding 1 asset ({base})",
        rows[0]["Asset"],
    )

    left_out = personal.value_in(latest, tick_spec.key, base)
    want = model_total - (left_out or 0.0)
    tick_shown = rows[0][snapshots_newest_first[0]]
    check(
        "the total drops the ticked-out holding",
        abs(tick_shown - want) <= 0.5,
        f"page {tick_shown}, model {want}",
    )
    check(
        "...so it is no longer the whole-portfolio total",
        abs(tick_shown - model_total) > 1,
        f"{tick_shown} vs {model_total}",
    )
    check(
        "the per-currency table drops it too",
        abs(
            next(
                r
                for r in table_records(currency_frame(ticked))
                if r["Date"] == snapshots_newest_first[0]
            )[base]
            - want
        )
        <= 0.5,
        str(
            next(
                r
                for r in table_records(currency_frame(ticked))
                if r["Date"] == snapshots_newest_first[0]
            )[base]
        ),
    )
    check(
        "the page names what is out of the totals",
        any("Not in the totals" in i.value and tick_spec.label in i.value for i in ticked.info),
        str([i.value for i in ticked.info]),
    )
    check(
        "...and the table below says so too",
        any("left out of these totals too" in c.value for c in ticked.caption),
        str([c.value[-60:] for c in ticked.caption]),
    )

    # Two out, to pin the plural of the heading rather than the singular.
    other = next(
        s
        for s in ordered
        if s is not tick_spec and personal.value_in(latest, s.key, base) not in (None, 0)
    )
    other_row = ordered.index(other) + 1
    ticked.session_state["excluded"] = {tick_spec.key, other.key}
    ticked.run()
    check(
        "two assets left out are counted as two",
        table_records(asset_frame(ticked))[0]["Asset"] == f"Total excluding 2 assets ({base})",
        table_records(asset_frame(ticked))[0]["Asset"],
    )
    check(
        "each of them is greyed out on its own row",
        styled_rows(asset_frame(ticked).proto.arrow_data.styler.styles) == {tick_row, other_row},
        asset_frame(ticked).proto.arrow_data.styler.styles,
    )

    # Ticking it back has to undo all of it.
    ticked.session_state["excluded"] = set()
    ticked.run()
    check(
        "ticking it back in does not take the page down",
        not ticked.exception,
        str([e.value for e in ticked.exception]),
    )
    rows = table_records(asset_frame(ticked))
    check(
        "the tick comes back", rows[tick_row]["In total"] is True, str(rows[tick_row]["In total"])
    )
    css = asset_frame(ticked).proto.arrow_data.styler.styles
    check("the greying comes off with it", not styled_rows(css), css)
    check(
        "the label was never anything but plain",
        rows[tick_row]["Asset"] == label_of(tick_spec),
        rows[tick_row]["Asset"],
    )
    check("the heading is plain again", rows[0]["Asset"] == f"Total ({base})", rows[0]["Asset"])
    check(
        "the total counts it again",
        abs(rows[0][snapshots_newest_first[0]] - model_total) <= 0.5,
        f"{rows[0][snapshots_newest_first[0]]} vs {model_total}",
    )
    check("the notice goes away", not ticked.info, str([i.value for i in ticked.info]))


# --- reacting to the sidebar --------------------------------------------------


def test_sidebar_currency(fresh, dates):
    """Switching the sidebar currency re-totals without touching the rest."""
    currency_table = fresh.currency
    snapshots_newest_first = dates
    shown = fresh.assets[0][snapshots_newest_first[0]]
    usd = run()
    usd.sidebar.radio[0].set_value("USD").run()
    check(
        "switching currency does not crash",
        not usd.exception,
        str([e.value for e in usd.exception]),
    )
    usd_rows = table_records(asset_frame(usd))
    check(
        "the total row follows the selected currency",
        usd_rows[0]["Asset"].endswith("(USD)"),
        usd_rows[0]["Asset"],
    )
    check(
        "switching currency actually changes the total",
        usd_rows[0][snapshots_newest_first[0]] != shown,
        f"{usd_rows[0][snapshots_newest_first[0]]} vs {shown}",
    )
    usd_cur = table_records(currency_frame(usd))
    check(
        "the per-currency table is unaffected by the selector",
        usd_cur[0]["USD"] == currency_table[0]["USD"],
        str(usd_cur[0]),
    )


# --- a broken portfolio must not take the page down --------------------------


def test_broken_portfolio(tmp_path, monkeypatch):
    """A portfolio that fails to load is reported, not raised."""
    # A currency the file never declares: the kind of slip a hand edit makes.
    (tmp_path / "broken.yaml").write_text(
        "currencies: [AUD]\nassets:\n  - {key: a, type: cash, currency: GBP}\n"
    )
    monkeypatch.setenv("PORTFOLIO_DIR", str(tmp_path))
    at = run()
    check(
        "a portfolio that fails to load does not take the page down",
        not at.exception,
        str([e.value for e in at.exception]),
    )
    check(
        "the page names the file that failed",
        any("broken" in e.value for e in at.error),
        str([e.value for e in at.error]),
    )
    check("no table is rendered for it", not at.dataframe, str(len(at.dataframe)))


# --- marks degrade rather than lie -------------------------------------------


def test_unmapped_currency_marks(tmp_path, monkeypatch):
    """A currency with no country renders without a flag or stray space."""
    # A currency with no defensible country (XAU is gold, not a place) must produce no
    # flag, and the row must not end up with a double space where one would have been.
    (tmp_path / "odd.yaml").write_text(
        "currencies: [XAU]\n"
        "assets:\n"
        "  - {key: bullion, name: Gold, type: other, currency: XAU}\n"
        "  - {key: odd, name: Odd, type: property, currency: XAU}\n"
        "snapshots:\n"
        "  - date: '2026-09-30'\n"
        "    fx: {XAU: {XAU: 1.0}}\n"
        "    assets:\n"
        "      - {ref: bullion, value: 1000}\n"
        "      - {ref: odd, value: 500}\n"
    )
    monkeypatch.setenv("PORTFOLIO_DIR", str(tmp_path))
    at = run()
    check(
        "a currency with no country still renders",
        not at.exception,
        str([e.value for e in at.exception]),
    )
    rows = table_records(asset_frame(at))[1:]
    check(
        "an unmapped currency gets no flag, and no stray space",
        rows[0]["Asset"] == f"{TYPE_EMOJI['other']} Gold",
        repr(rows[0]["Asset"]),
    )
    check(
        "and the same for another type on that currency",
        rows[1]["Asset"] == f"{TYPE_EMOJI['property']} Odd",
        repr(rows[1]["Asset"]),
    )


# --- an empty folder is a message, not a crash --------------------------------


def test_empty_folder(tmp_path, monkeypatch):
    """An empty portfolio folder is a message, not a crash."""
    monkeypatch.setenv("PORTFOLIO_DIR", str(tmp_path))
    at = run()
    check(
        "an empty folder is reported, not raised",
        not at.exception,
        str([e.value for e in at.exception]),
    )
    check(
        "...with a message naming the folder",
        any(str(tmp_path) in e.value for e in at.error),
        str([e.value for e in at.error]),
    )


# --- switching files drops the other file's ticks -----------------------------


def test_switching_files(tmp_path, monkeypatch):
    """Switching files drops the other file's ticks and open panel."""
    # Two files with the same asset key: the tick belongs to the file it was made
    # in, and the table's own edits are remembered by row position, so they must not
    # survive the switch either.
    (tmp_path / "one.yaml").write_text(
        "currencies: [AUD]\nassets:\n  - {key: a, type: cash, currency: AUD}\n"
        "snapshots:\n  - date: '2026-09-01'\n    fx: {AUD: {AUD: 1.0}}\n"
        "    assets: [{ref: a, value: 100}]\n"
    )
    (tmp_path / "two.yaml").write_text(
        "currencies: [AUD]\nassets:\n  - {key: a, type: cash, currency: AUD}\n"
        "snapshots:\n  - date: '2026-09-01'\n    fx: {AUD: {AUD: 1.0}}\n"
        "    assets: [{ref: a, value: 500}]\n"
    )
    monkeypatch.setenv("PORTFOLIO_DIR", str(tmp_path))
    at = run()
    check(
        "both files are offered",
        at.sidebar.selectbox[0].options == ["one", "two"],
        str(at.sidebar.selectbox[0].options),
    )
    at.session_state["excluded"] = {"a"}
    at.run()
    rows = table_records(asset_frame(at))
    check(
        "the tick is applied in the first file",
        rows[1]["In total"] is False,
        str(rows[1]["In total"]),
    )
    check("...and the total says so", "excluding 1 asset" in rows[0]["Asset"], rows[0]["Asset"])
    at.session_state["inspect_key"] = "a"
    at.run()
    check(
        "the workings panel opens in the first file",
        any("was valued" in m.value for m in at.markdown),
        str([m.value for m in at.markdown]),
    )
    at.sidebar.selectbox[0].set_value("two").run()
    check(
        "switching file with a tick applied does not raise",
        not at.exception,
        str([e.value for e in at.exception]),
    )
    rows = table_records(asset_frame(at))
    check("the other file is the other file", rows[0]["2026-09-01"] == 500, str(rows[0]))
    check("...with its own tick left alone", rows[1]["In total"] is True, str(rows[1]["In total"]))
    check(
        "...so nothing is left out of its totals",
        rows[0]["Asset"] == "Total (AUD)",
        rows[0]["Asset"],
    )
    check(
        "...and it does not inherit the other file's notice",
        not at.info,
        str([i.value for i in at.info]),
    )
    check(
        "...nor the other file's open panel -- a row position means nothing here",
        not any("was valued" in m.value for m in at.markdown),
        str([m.value for m in at.markdown]),
    )


# --- the workings panel -------------------------------------------------------


def test_workings_panel(personal, ordered, dates, latest):
    """The workings table reproduces each cell's arithmetic."""
    base = personal.currencies[0]
    snapshots_newest_first = dates
    TODAY = dt.date.today().isoformat()
    # Most checks here write the panel's remembered position directly -- the state
    # the click callback produces -- because what matters is what the panel says.
    # That is checked against the model and, the point of the feature, against the
    # very cell it explains. The click itself is exercised further down, for real.
    panel = run()
    check(
        "a fresh run opens no workings panel",
        not any("was valued" in m.value for m in panel.markdown),
        str([m.value for m in panel.markdown]),
    )

    # A USD holding, so its table carries units, price, rate and result; an AUD
    # row exercises the identity-rate case further down.
    fx_spec = next(s for s in ordered if s.type == "stocks" and s.ticker and s.currency != base)
    fx_row = ordered.index(fx_spec) + 1
    panel.session_state["inspect_key"] = fx_spec.key
    panel.run()
    check(
        "asking for the panel does not take the page down",
        not panel.exception,
        str([e.value for e in panel.exception]),
    )
    headings = [m.value for m in panel.markdown if "was valued" in m.value]
    check(
        "the panel opens, once, for the row asked for",
        len(headings) == 1 and fx_spec.label in headings[0],
        str(headings),
    )
    records = workings_records(panel)
    row_for = {row["Snapshot"]: row for row in records}
    check(
        "one row per snapshot, in the table's own order",
        list(row_for) == ["Today", *snapshots_newest_first],
        str(list(row_for)),
    )

    panel_rows = table_records(asset_frame(panel))
    holding = next(a for a in latest.assets if a.ref == fx_spec.key)
    price_column = f"{fx_spec.ticker} price"
    total_column = f"Total in {fx_spec.currency}"
    rate_column = f"{fx_spec.currency}→{base} rate"
    final_column = f"Final value in {base}"
    snap_row = row_for[snapshots_newest_first[0]]
    check(
        "the table has a column for each calculation input and result",
        list(records[0])
        == [
            "Snapshot",
            "Units",
            price_column,
            total_column,
            rate_column,
            final_column,
            "Shown in table",
            "Note",
        ],
        str(list(records[0])),
    )
    check(
        "the snapshot row carries the units, price, and source total",
        snap_row["Units"] == holding.units
        and snap_row[price_column] == holding.price
        and snap_row[total_column] == holding.resolved_value,
        str(snap_row),
    )
    check(
        "the snapshot row carries its rate, final value, and displayed value",
        snap_row[rate_column] == latest.fx[fx_spec.currency][base]
        and snap_row[final_column] == personal.value_in(latest, fx_spec.key, base)
        and snap_row["Shown in table"] == panel_rows[fx_row][snapshots_newest_first[0]],
        str(snap_row),
    )
    today_row = row_for["Today"]
    check(
        "today's row names the close and rates it used",
        f"{fx_spec.ticker}'s close on {TODAY}" in today_row["Note"]
        and f"rates as at {TODAY}" in today_row["Note"],
        str(today_row),
    )
    check(
        "today's calculated value matches the asset table",
        today_row["Shown in table"] == panel_rows[fx_row]["Today"],
        str(today_row),
    )

    # A flat, same-currency holding: no units, no conversion -- and a negative one
    # after it, because a sign lost in an explanation is a wrong explanation.
    flat = next(s for s in ordered if s.key == "ubank-tomomi")
    flat_row = ordered.index(flat) + 1
    panel.session_state["inspect_key"] = flat.key
    panel.run()
    flat_for = {row["Snapshot"]: row for row in workings_records(panel)}
    flat_holding = next(
        holding
        for holding in personal.by_date(snapshots_newest_first[0]).assets
        if holding.ref == flat.key
    )
    flat_total = f"Total in {flat.currency}"
    flat_rate = f"{flat.currency}→{base} rate"
    flat_final = f"Final value in {base}"
    check(
        "today's row says a flat amount is carried, not priced",
        "carried at face value" in flat_for["Today"]["Note"],
        str(flat_for.get("Today", "")),
    )
    flat_snapshot = flat_for[snapshots_newest_first[0]]
    check(
        "a flat amount is shown as recorded with an identity conversion rate",
        "Flat amount in file" in flat_snapshot["Note"]
        and flat_snapshot["Units"] is None
        and flat_snapshot["Price"] is None
        and flat_snapshot[flat_total] == flat_holding.value,
        str(flat_snapshot),
    )
    check(
        "the flat amount's result and shown value match the asset table",
        flat_snapshot[flat_rate] == 1
        and flat_snapshot[flat_final]
        == personal.value_in(personal.by_date(snapshots_newest_first[0]), flat.key, base)
        and flat_snapshot["Shown in table"]
        == table_records(asset_frame(panel))[flat_row][snapshots_newest_first[0]],
        str(flat_snapshot),
    )

    neg = next(s for s in ordered if s.key == "mac-mortgage")
    panel.session_state["inspect_key"] = neg.key
    panel.run()
    neg_rows = {row["Snapshot"]: row for row in workings_records(panel)}
    neg_final = f"Final value in {base}"
    check(
        "a liability keeps its sign in the workings",
        neg_rows[snapshots_newest_first[0]][neg_final]
        == personal.value_in(latest, neg.key, base)
        < 0,
        str(neg_rows[snapshots_newest_first[0]]),
    )

    # A remembered position that no longer maps to a row is ignored, not opened for
    # whatever asset now sits there -- and the total is not a row it can open.
    panel.session_state["inspect_key"] = "missing-asset"
    panel.run()
    check(
        "a row that no longer exists is ignored, not misshown",
        not panel.exception and not any("was valued" in m.value for m in panel.markdown),
        str([e.value for e in panel.exception]),
    )
    panel.session_state["inspect_key"] = None
    panel.run()
    check(
        "the total gets no panel -- its workings are the rows above it",
        not panel.exception and not any("was valued" in m.value for m in panel.markdown),
        str([e.value for e in panel.exception]),
    )

    # The click itself -- the whole point of the button. `press` builds the fresh
    # widget state a real click delivers (see its docstring); the first click opens
    # the row that was clicked, the second one toggles it shut again.
    press(panel, fx_row)
    check(
        "clicking a row's button opens its panel",
        not panel.exception
        and any("was valued" in m.value and fx_spec.label in m.value for m in panel.markdown),
        str([e.value for e in panel.exception] or [m.value for m in panel.markdown]),
    )
    press(panel, fx_row)
    check(
        "clicking the same button again closes it",
        not panel.exception and not any("was valued" in m.value for m in panel.markdown),
        str([e.value for e in panel.exception] or [m.value for m in panel.markdown]),
    )


def test_workings_panel_not_held(tmp_path, monkeypatch):
    """Not held on a date is explained, not shown as zero."""
    # Not held on a date is a different fact from held with no amount, and the panel
    # has to keep them apart like the table does.
    (tmp_path / "late.yaml").write_text(
        "currencies: [AUD]\n"
        "assets:\n"
        "  - {key: old, name: Old, type: cash, currency: AUD}\n"
        "  - {key: new, name: New, type: cash, currency: AUD}\n"
        "snapshots:\n"
        "  - date: '2026-09-01'\n"
        "    fx: {AUD: {AUD: 1.0}}\n"
        "    assets: [{ref: old, value: 100}]\n"
        "  - date: '2026-10-01'\n"
        "    fx: {AUD: {AUD: 1.0}}\n"
        "    assets: [{ref: old, value: 100}, {ref: new, value: 200}]\n"
    )
    monkeypatch.setenv("PORTFOLIO_DIR", str(tmp_path))
    late = run()
    check(
        "the fixture portfolio renders", not late.exception, str([e.value for e in late.exception])
    )
    late.session_state["inspect_key"] = "new"  # held only in the later snapshot
    late.run()
    late_for = {row["Snapshot"]: row for row in workings_records(late)}
    check(
        "one row for each of that file's own columns",
        list(late_for) == ["Today", "2026-10-01", "2026-09-01"],
        str(list(late_for)),
    )
    check(
        "the date it was not held says so, rather than reading as zero",
        late_for["2026-09-01"]["Note"] == "Not held"
        and is_blank(late_for["2026-09-01"]["Final value in AUD"]),
        str(late_for),
    )
    check(
        "the date it was held shows the amount",
        late_for["2026-10-01"]["Final value in AUD"] == 200
        and late_for["2026-10-01"]["Shown in table"] == 200,
        str(late_for),
    )


# --- a portfolio with no snapshots yet ----------------------------------------


def test_no_snapshots(tmp_path, monkeypatch):
    """A portfolio with no snapshots offers the form for its first one."""
    (tmp_path / "empty.yaml").write_text(
        "currencies: [AUD]\nassets:\n  - {key: a, type: cash, currency: AUD}\n"
    )
    monkeypatch.setenv("PORTFOLIO_DIR", str(tmp_path))
    at = run()
    check(
        "an empty portfolio renders without raising",
        not at.exception,
        str([e.value for e in at.exception]),
    )
    check(
        "the first-snapshot prompt is shown",
        any("first snapshot" in info.value for info in at.info),
        str([info.value for info in at.info]),
    )
    check(
        "the empty portfolio still has its asset-entry row",
        len(table_records(snapshot_entry_frame(at))) == 1
        and table_records(snapshot_entry_frame(at))[0]["Asset"].endswith(" a"),
        str(table_records(snapshot_entry_frame(at))),
    )


def test_snapshot_entry_creates_and_updates_snapshot(tmp_path, monkeypatch):
    """The form creates a snapshot and edits it without changing its frozen FX."""
    (tmp_path / "entry.yaml").write_text(
        "currencies: [AUD, USD]\n"
        "assets:\n"
        "  - {key: cash, name: Cash, type: cash, currency: AUD}\n"
        "  - {key: shares, name: Shares, type: stocks, ticker: MSFT, currency: USD}\n"
    )
    monkeypatch.setenv("PORTFOLIO_DIR", str(tmp_path))

    def fake_fetch_fx(on, currencies, **kwargs):
        codes = list(currencies)
        rates = {
            source: {
                target: 1.0 if source == target else (0.65 if source == "AUD" else 1.54)
                for target in codes
            }
            for source in codes
        }
        return FxRates(on.isoformat(), on.isoformat(), rates)

    monkeypatch.setattr("fx.fetch_fx", fake_fetch_fx)
    monkeypatch.setattr(
        prices,
        "close_on",
        lambda ticker, on, **kwargs: prices.Quote(
            ticker=ticker,
            price=100.0,
            price_date=on,
            currency="USD",
            as_of=on,
        ),
    )

    at = run()
    month = at.selectbox[0].value
    editor = snapshot_entry_frame(at)
    row_for = {key: index for index, key in enumerate(editor.value.index)}
    at.session_state[editor.key] = {
        "edited_rows": {
            row_for["cash"]: {"Held": True, "Entry": "Value", "Value": 1200.0},
            row_for["shares"]: {"Held": True, "Entry": "Units & price", "Units": 2.0},
        },
        "added_rows": [],
        "deleted_rows": [],
    }
    at.button[0].click().run()

    check(
        "creating the first snapshot has no app error",
        not at.exception,
        str([e.value for e in at.exception]),
    )
    created = load("entry", directory=tmp_path)
    snapshot_date = f"{month}-01"
    snapshot = created.by_date(snapshot_date)
    check(
        "the new month is saved",
        snapshot is not None,
        str([item.date for item in created.snapshots]),
    )
    if snapshot is None:
        return
    cash = next(asset for asset in snapshot.assets if asset.ref == "cash")
    shares = next(asset for asset in snapshot.assets if asset.ref == "shares")
    check("flat values are stored", cash.value == 1200.0, str(cash))
    check(
        "units fetch and store the historical close",
        shares.units == 2 and shares.price == 100.0,
        str(shares),
    )

    frozen_fx = snapshot.fx
    editor = snapshot_entry_frame(at)
    row_for = {key: index for index, key in enumerate(editor.value.index)}
    at.session_state[editor.key] = {
        "edited_rows": {row_for["cash"]: {"Value": 2300.0}},
        "added_rows": [],
        "deleted_rows": [],
    }
    at.button[0].click().run()

    check(
        "editing the month has no app error", not at.exception, str([e.value for e in at.exception])
    )
    updated = load("entry", directory=tmp_path)
    snapshot = updated.by_date(snapshot_date)
    check("editing replaces rather than duplicates the snapshot", len(updated.snapshots) == 1)
    check(
        "the edited value is persisted", snapshot is not None and snapshot.assets[0].value == 2300.0
    )
    check(
        "editing keeps the snapshot's FX matrix", snapshot is not None and snapshot.fx == frozen_fx
    )
