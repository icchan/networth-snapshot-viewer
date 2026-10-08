"""Checks for the Streamlit page.

Streamlit re-runs the whole script on every interaction, so there is no function
to import and unit-test directly. :class:`AppTest` runs the real script against
the real data and hands back the widgets and the rendered tables, which is
enough to catch the failures that actually matter: a crash on load, a total that
disagrees with the model, or a column that stopped being a date.
"""

import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, "/workspaces/hellocode")

import streamlit as st
from streamlit.testing.v1 import AppTest

import market
from fx import FxError
from snapshots import load

failures: list[str] = []


def check(label, condition, detail=""):
    print(("PASS  " if condition else "FAIL  ") + label + (f"  {detail}" if detail else ""))
    if not condition:
        failures.append(label)


APP = Path(__file__).resolve().parent / "app.py"


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


market.fetch_marks = fake_fetch_marks


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


at = run()
check("the app runs without raising", not at.exception, str([e.value for e in at.exception]))
check("and reports no errors", not at.error, str([e.value for e in at.error]))
check("and no warnings on clean data", not at.warning, str([w.value for w in at.warning]))

check("two tables are rendered", len(at.dataframe) == 2, str(len(at.dataframe)))
assets_table, currency_table = table_records(at.dataframe[0]), table_records(at.dataframe[1])

portfolio = load("personal")
snapshots_newest_first = sorted((s.date for s in portfolio.snapshots), reverse=True)
# The page's own ordering, restated. Sorting on (currency, type) rather than on the
# rendered label matters: a string sort would order by the emoji code point.
ordered = sorted(portfolio.specs, key=lambda spec: (spec.currency, spec.type))

# Imported, not restated. Which emoji stands for "cash" is a taste call that
# changes; what these tests pin is the *rule* -- an emoji, then a flag, then the
# name -- and the flags themselves, which are a correctness matter. Pinning the
# glyphs here too only meant every change to the page's taste broke the suite.
from labels import TYPE_EMOJI, flag as flag_of  # noqa: E402

# --- the assets table ---------------------------------------------------------
check("the total is the first row",
      assets_table[0]["Asset"].startswith("Total"), assets_table[0]["Asset"])
check("the total names the selected currency",
      assets_table[0]["Asset"].endswith("(AUD)"), assets_table[0]["Asset"])
TODAY = dt.date.today().isoformat()
asset_cols = [c for c in assets_table[0] if c not in ("Asset", "In total")]
check("the tick column leads, so the toggle sits beside the assets",
      list(assets_table[0])[0] == "In total", str(list(assets_table[0])[:2]))
check("the live column leads the amounts, then snapshots run newest first",
      asset_cols == ["Today", *snapshots_newest_first], str(asset_cols))
check("one row per catalogue entry, in currency-then-type order",
      [r["Asset"] for r in assets_table[1:]]
      == [f"{TYPE_EMOJI[s.type]} {flag_of(s.currency)} {s.label}"
          for s in ordered],
      str([r["Asset"] for r in assets_table[1:]][:3]))

# --- the ordering itself -----------------------------------------------------
row_specs = ordered
currencies = [spec.currency for spec in row_specs]
check("currencies are grouped, not interleaved",
      currencies == sorted(currencies), str(currencies))
runs = [c for i, c in enumerate(currencies) if i == 0 or currencies[i - 1] != c]
check("each currency forms one contiguous block",
      len(runs) == len(set(runs)), str(runs))
for ccy in runs:
    types_in = [spec.type for spec in row_specs if spec.currency == ccy]
    check(f"types within {ccy} are grouped and ordered",
          types_in == sorted(types_in), str(types_in))
# Ties must fall back to catalogue order, or the same two accounts could swap
# places between runs and quietly lose their grouping.
for ccy in runs:
    for kind in sorted({spec.type for spec in row_specs if spec.currency == ccy}):
        in_file = [s.key for s in portfolio.specs
                   if s.currency == ccy and s.type == kind]
        on_page = [spec.key for spec in row_specs
                   if spec.currency == ccy and spec.type == kind]
        check(f"{ccy}/{kind} keeps catalogue order",
              in_file == on_page, f"{on_page} vs {in_file}")
check("the total row is still first",
      assets_table[0]["Asset"].startswith("Total"), assets_table[0]["Asset"])
check("the row count still matches the catalogue",
      len(assets_table) == len(portfolio.specs) + 1, str(len(assets_table)))

# --- the marks themselves ----------------------------------------------------
labels = [r["Asset"] for r in assets_table[1:]]
marked = dict(zip((s.key for s in row_specs), labels))

# The flags are the part that can be wrong rather than merely unattractive.
check("a currency that names one country gets that flag",
      flag_of("AUD") == "🇦🇺"
      and flag_of("JPY") == "🇯🇵"
      and flag_of("USD") == "🇺🇸",
      f"{flag_of('AUD')} {flag_of('JPY')} {flag_of('USD')}")
check("a currency that names no country gets no flag",
      flag_of("XAU") == "" and flag_of("") == "" and flag_of("  ") == "",
      repr(flag_of("XAU")))
check("the lookup is case- and space-insensitive",
      flag_of(" aud ") == flag_of("AUD"), repr(flag_of(" aud ")))

check("every type in the catalogue has a mark",
      all(s.type in TYPE_EMOJI for s in portfolio.specs),
      str(sorted({s.type for s in portfolio.specs} - set(TYPE_EMOJI))))
check("every asset row is <emoji> <flag> <name>",
      all(r["Asset"] == f"{TYPE_EMOJI[s.type]} {flag_of(s.currency)} {s.label}"
          for r, s in zip(assets_table[1:], row_specs)),
      str(labels[:2]))
check("...and still ends in the plain name",
      all(r["Asset"].endswith(s.label)
          for r, s in zip(assets_table[1:], row_specs)))
check("no row is left unflagged when its currency is known",
      all(flag_of(s.currency) in marked[s.key]
          for s in row_specs if s.currency in ("AUD", "JPY", "USD")))
check("the total row carries no marks -- it is not an asset",
      not any(ch in assets_table[0]["Asset"] for ch in
              "".join(TYPE_EMOJI.values())), assets_table[0]["Asset"])
check("the second table is not marked up, only the first",
      all(not row["Date"].startswith(tuple(TYPE_EMOJI.values()))
          for row in currency_table))
check("a held asset has a number, not a blank",
      assets_table[1][snapshots_newest_first[0]] is not None)

# The important one: the rendered total must be the model's total, not something
# the page computed on its own.
shown = assets_table[0][snapshots_newest_first[0]]
latest = portfolio.by_date(snapshots_newest_first[0])
model_total = portfolio.total_in(latest, "AUD")
check("the shown total is the model's total, to the nearest unit",
      abs(shown - model_total) < 0.5, f"page {shown}, model {model_total}")

# Deliberately not asserting `shown == round(model_total)`: that would just
# restate the page's own rounding rule. Assert the property instead -- a whole
# number that rounds back onto the model.
check("every amount is a whole number, never a float",
      all(isinstance(v, int) or v is None
          for row in assets_table
          for k, v in row.items() if k not in ("Asset", "In total")),
      str([v for row in assets_table for k, v in row.items()
           if k not in ("Asset", "In total") and v is not None
           and not isinstance(v, int)]))
check("no total is off by more than half a unit",
      abs(shown - model_total) <= 0.5, f"{shown} vs {model_total}")

# Displayed cells are rounded, so the column reconciles with the model to within
# half a unit per row rather than exactly.
model_sum = sum(portfolio.value_in(latest, s.key, "AUD") or 0.0 for s in portfolio.specs)
rows_shown = sum(assets_table[i + 1][snapshots_newest_first[0]] or 0
                 for i in range(len(portfolio.specs)))
check("the asset column sums to the shown total, within rounding",
      abs(model_sum - rows_shown) < len(portfolio.specs) * 0.5,
      f"model {model_sum:.2f}, rows shown {rows_shown}")

# A spot check computed independently of the page's helper.
check("a known holding rounds correctly",
      assets_table[1][snapshots_newest_first[0]]
      == round(portfolio.value_in(latest, "ubank-tomomi", "AUD")),
      str(assets_table[1][snapshots_newest_first[0]]))
check("a negative holding rounds away from zero symmetrically",
      assets_table[3][snapshots_newest_first[0]] == -639852,
      str(assets_table[3][snapshots_newest_first[0]]))

# --- the live column ----------------------------------------------------------
# The fake marks double every close, so anything re-marked must move and anything
# carried must not. That is what separates the two kinds of number on this row.
TODAY_TOTAL = assets_table[0]["Today"]
LATEST_TOTAL = assets_table[0][snapshots_newest_first[0]]

check("the live column has its own total", TODAY_TOTAL is not None, str(TODAY_TOTAL))
check("revaluation actually moved the total", TODAY_TOTAL != LATEST_TOTAL,
      f"{TODAY_TOTAL} vs {LATEST_TOTAL}")

row_specs = ordered
today_by_label = {r["Asset"]: r["Today"] for r in assets_table[1:]}


def is_blank(value):
    return value is None or (isinstance(value, float) and value != value)


cash_row = next(r for r in assets_table[1:] if "UBank" in r["Asset"])
check("a cash balance is carried at face value, so it does not move",
      cash_row["Today"] == cash_row[snapshots_newest_first[0]],
      str(cash_row["Today"]))

# A "stocks" asset is not necessarily a quantity: Commsec is an amount, so it
# sits this out. Only the ticker-backed holdings are re-marked.
tickered = {s.key for s in row_specs if s.type == "stocks" and s.ticker}
share_rows = [r for r, s in zip(assets_table[1:], row_specs) if s.key in tickered]
check("every share holding with a ticker was re-marked, so it moves",
      all(r["Today"] != r[snapshots_newest_first[0]] for r in share_rows),
      str([(r["Asset"], r["Today"], r[snapshots_newest_first[0]]) for r in share_rows]))
check("...and none of them is blank",
      all(not is_blank(r["Today"]) for r in share_rows))
carried = next(r for r in assets_table[1:] if "Commsec" in r["Asset"])
check("a share account held as an amount is carried, like cash",
      carried["Today"] == carried[snapshots_newest_first[0]],
      str(carried["Today"]))

cells = [r["Today"] for r in assets_table[1:]]
check("the live total is the sum of its own cells, within rounding",
      abs(sum(v for v in cells if not is_blank(v)) - TODAY_TOTAL) <= len(cells),
      f"model {TODAY_TOTAL}, rows shown {sum(v for v in cells if not is_blank(v))}")
check("and no live cell is zeroed when it should be a number",
      all(not is_blank(v) for v in cells), str([c for c in cells if is_blank(c)]))

live_caption = next((c.value for c in at.caption if "never written to the file" in c.value), "")
check("the live column says it is not a snapshot",
      live_caption and "not a snapshot" in live_caption)
check("...and says it is never written to the file",
      live_caption and "never written to the file" in live_caption)
check("...and explains what was carried at face value",
      live_caption and "face value" in live_caption)
check("...and names the dates the numbers came from",
      live_caption and "rates as at" in live_caption or "no rates published" in live_caption)

# --- the live column, when a price will not come -----------------------------
MARKS["drop"] = {"MSFT"}
try:
    at = run()
    check("a missing price does not take the page down",
          not at.exception, str([e.value for e in at.exception]))
    recs = table_records(at.dataframe[0])
    msft_rows = [r for r in recs[1:] if "Microsoft" in r["Asset"] or "Fidelity" in r["Asset"]
                 or "Morgan" in r["Asset"]]
    check("a holding with no close today is blank, not carried forward",
          msft_rows and all(is_blank(r["Today"]) for r in msft_rows),
          str([(r["Asset"], r["Today"]) for r in msft_rows]))
    check("...while the snapshot columns are untouched",
          msft_rows and all(not is_blank(r[snapshots_newest_first[0]]) for r in msft_rows),
          str([(r["Asset"], r[snapshots_newest_first[0]]) for r in msft_rows]))
    check("...and the total is blank rather than short by an unknown amount",
          is_blank(recs[0]["Today"]), str(recs[0]["Today"]))
    check("the page says which prices did not come back",
          any("No close today" in w.value for w in at.warning),
          str([w.value[:90] for w in at.warning]))
finally:
    MARKS["drop"] = set()

# --- the live column, when there are no rates at all -------------------------
MARKS["fail"] = True
try:
    at = run()
    check("missing rates do not take the page down",
          not at.exception, str([e.value for e in at.exception]))
    cols = [c for c in table_records(at.dataframe[0])[0]
            if c not in ("Asset", "In total")]
    check("the live column is dropped rather than shown empty",
          cols == snapshots_newest_first, str(cols))
    check("the snapshots still render", len(table_records(at.dataframe[0])) == 17,
          str(len(table_records(at.dataframe[0]))))
    check("the page reports why the live column is gone",
          any("Today's column is unavailable" in w.value for w in at.warning),
          str([w.value[:90] for w in at.warning]))
    check("...and the live row is dropped from the totals table too",
          [r["Date"] for r in table_records(at.dataframe[1])] == snapshots_newest_first,
          str([r["Date"] for r in table_records(at.dataframe[1])]))
finally:
    MARKS["fail"] = False

# --- the per-currency table ---------------------------------------------------
check("one row per snapshot, then the live row",
      [r["Date"] for r in currency_table[:-1]] == snapshots_newest_first
      and currency_table[-1]["Date"].endswith(" (live)"),
      str([r["Date"] for r in currency_table]))
check("...and it is dated today",
      currency_table[-1]["Date"] == f"{TODAY} (live)",
      currency_table[-1]["Date"])
check("one column per currency in the file",
      [c for c in currency_table[0] if c != "Date"] == portfolio.currencies,
      str([c for c in currency_table[0] if c != "Date"]))
# The live row is checked separately: it is not in portfolio.snapshots, so
# by_date cannot find it and a stored snapshot's total is not what it should be.
for record in currency_table:
    snap = portfolio.by_date(record["Date"])
    if snap is None:
        check(f"{record['Date']} is not a stored snapshot",
              record["Date"].endswith(" (live)"), record["Date"])
        continue
    for ccy in portfolio.currencies:
        want = portfolio.total_in(snap, ccy)
        check(f"total on {record['Date']} in {ccy} matches the model",
              record[ccy] is not None and abs(record[ccy] - want) <= 0.5,
              f"page {record[ccy]}, model {want}")

live_total = next((r for r in currency_table if r["Date"].endswith(" (live)")), None)
if live_total is not None:
    # Cross-checking the two tables against each other: both call total_in, but
    # on independently assembled rows, so a disagreement means one is wrong.
    check("the live row agrees with the live column above it",
          live_total["AUD"] is not None
          and assets_table[0]["Today"] is not None
          and abs(live_total["AUD"] - assets_table[0]["Today"]) <= 0.5,
          f"{live_total['AUD']} vs {assets_table[0]['Today']}")
    check("...in every currency, not just AUD",
          all(live_total[c] is not None for c in portfolio.currencies),
          str(live_total))

check("the JPY column is whole units too, not just the others",
      all(isinstance(r["JPY"], int) for r in currency_table),
      str([type(r["JPY"]).__name__ for r in currency_table]))

# --- ticking an asset out of the totals --------------------------------------
# AppTest cannot click inside the data editor, so the toggled state is written
# the way the page itself stores it. What that state then drives -- the tick,
# the mark on the row, the heading, and every total -- is what is checked here.
from labels import row_label as label_of  # noqa: E402

base = portfolio.currencies[0]
tick_spec = next(s for s in ordered
                 if portfolio.value_in(latest, s.key, base) not in (None, 0))
tick_row = ordered.index(tick_spec) + 1  # the total row leads the table

ticked = run()
check("nothing is ticked out on a fresh run", not ticked.info,
      str([i.value for i in ticked.info]))
check("so the heading counts nothing out",
      table_records(ticked.dataframe[0])[0]["Asset"] == f"Total ({base})",
      table_records(ticked.dataframe[0])[0]["Asset"])

ticked.session_state["excluded"] = {tick_spec.key}
ticked.run()
check("ticking an asset out does not take the page down", not ticked.exception,
      str([e.value for e in ticked.exception]))
rows = table_records(ticked.dataframe[0])
check("the asset shows as unticked", rows[tick_row]["In total"] is False,
      str(rows[tick_row]["In total"]))
check("...while every other asset stays ticked",
      all(r["In total"] is True for i, r in enumerate(rows) if i not in (0, tick_row)),
      str([(r["Asset"], r["In total"]) for r in rows if r["In total"] is not True]))
check("the total row offers no tick to make", rows[0]["In total"] is None,
      str(rows[0]["In total"]))
check("the row is marked where it stands",
      rows[tick_row]["Asset"].startswith("🚫 ")
      and rows[tick_row]["Asset"].endswith("(excluded)"),
      rows[tick_row]["Asset"])
check("...with its plain name still readable",
      tick_spec.label in rows[tick_row]["Asset"], rows[tick_row]["Asset"])
check("and it still shows its own amounts",
      rows[tick_row][snapshots_newest_first[0]]
      == round(portfolio.value_in(latest, tick_spec.key, base)),
      str(rows[tick_row][snapshots_newest_first[0]]))
check("the heading counts what is left out",
      rows[0]["Asset"] == f"Total excluding 1 asset ({base})", rows[0]["Asset"])

left_out = portfolio.value_in(latest, tick_spec.key, base)
want = model_total - (left_out or 0.0)
tick_shown = rows[0][snapshots_newest_first[0]]
check("the total drops the ticked-out holding",
      abs(tick_shown - want) <= 0.5, f"page {tick_shown}, model {want}")
check("...so it is no longer the whole-portfolio total",
      abs(tick_shown - model_total) > 1, f"{tick_shown} vs {model_total}")
check("the per-currency table drops it too",
      abs(next(r for r in table_records(ticked.dataframe[1])
               if r["Date"] == snapshots_newest_first[0])[base] - want) <= 0.5,
      str(next(r for r in table_records(ticked.dataframe[1])
               if r["Date"] == snapshots_newest_first[0])[base]))
check("the page names what is out of the totals",
      any("Not in the totals" in i.value and tick_spec.label in i.value
          for i in ticked.info),
      str([i.value for i in ticked.info]))
check("...and the table below says so too",
      any("left out of these totals too" in c.value for c in ticked.caption),
      str([c.value[-60:] for c in ticked.caption]))

# Two out, to pin the plural of the heading rather than the singular.
other = next(s for s in ordered if s is not tick_spec
             and portfolio.value_in(latest, s.key, base) not in (None, 0))
ticked.session_state["excluded"] = {tick_spec.key, other.key}
ticked.run()
check("two assets left out are counted as two",
      table_records(ticked.dataframe[0])[0]["Asset"]
      == f"Total excluding 2 assets ({base})",
      table_records(ticked.dataframe[0])[0]["Asset"])

# Ticking it back has to undo all of it.
ticked.session_state["excluded"] = set()
ticked.run()
check("ticking it back in does not take the page down", not ticked.exception,
      str([e.value for e in ticked.exception]))
rows = table_records(ticked.dataframe[0])
check("the tick comes back", rows[tick_row]["In total"] is True,
      str(rows[tick_row]["In total"]))
check("the mark comes off again", rows[tick_row]["Asset"] == label_of(tick_spec),
      rows[tick_row]["Asset"])
check("the heading is plain again", rows[0]["Asset"] == f"Total ({base})",
      rows[0]["Asset"])
check("the total counts it again",
      abs(rows[0][snapshots_newest_first[0]] - model_total) <= 0.5,
      f"{rows[0][snapshots_newest_first[0]]} vs {model_total}")
check("the notice goes away", not ticked.info, str([i.value for i in ticked.info]))

# --- reacting to the sidebar --------------------------------------------------
usd = run()
usd.sidebar.radio[0].set_value("USD").run()
check("switching currency does not crash", not usd.exception,
      str([e.value for e in usd.exception]))
usd_rows = table_records(usd.dataframe[0])
check("the total row follows the selected currency",
      usd_rows[0]["Asset"].endswith("(USD)"), usd_rows[0]["Asset"])
check("switching currency actually changes the total",
      usd_rows[0][snapshots_newest_first[0]] != shown,
      f"{usd_rows[0][snapshots_newest_first[0]]} vs {shown}")
usd_cur = table_records(usd.dataframe[1])
check("the per-currency table is unaffected by the selector",
      usd_cur[0]["USD"] == currency_table[0]["USD"], str(usd_cur[0]))

# --- a broken portfolio must not take the page down --------------------------
import tempfile  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    # A currency the file never declares: the kind of slip a hand edit makes.
    (Path(tmp) / "broken.yaml").write_text(
        "currencies: [AUD]\nassets:\n  - {key: a, type: cash, currency: GBP}\n"
    )
    previous = os.environ.get("PORTFOLIO_DIR")
    os.environ["PORTFOLIO_DIR"] = tmp
    try:
        at = run()
        check("a portfolio that fails to load does not take the page down",
              not at.exception, str([e.value for e in at.exception]))
        check("the page names the file that failed",
              any("broken" in e.value for e in at.error), str([e.value for e in at.error]))
        check("no table is rendered for it", not at.dataframe, str(len(at.dataframe)))
    finally:
        if previous is None:
            os.environ.pop("PORTFOLIO_DIR", None)
        else:
            os.environ["PORTFOLIO_DIR"] = previous

# --- marks degrade rather than lie -------------------------------------------
# A currency with no defensible country (XAU is gold, not a place) must produce no
# flag, and the row must not end up with a double space where one would have been.
with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / "odd.yaml").write_text(
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
    previous = os.environ.get("PORTFOLIO_DIR")
    os.environ["PORTFOLIO_DIR"] = tmp
    try:
        at = run()
        check("a currency with no country still renders",
              not at.exception, str([e.value for e in at.exception]))
        rows = table_records(at.dataframe[0])[1:]
        check("an unmapped currency gets no flag, and no stray space",
              rows[0]["Asset"] == f"{TYPE_EMOJI['other']} Gold", repr(rows[0]["Asset"]))
        check("and the same for another type on that currency",
              rows[1]["Asset"] == f"{TYPE_EMOJI['property']} Odd", repr(rows[1]["Asset"]))
    finally:
        if previous is None:
            os.environ.pop("PORTFOLIO_DIR", None)
        else:
            os.environ["PORTFOLIO_DIR"] = previous

# --- an empty folder is a message, not a crash --------------------------------
with tempfile.TemporaryDirectory() as tmp:
    previous = os.environ.get("PORTFOLIO_DIR")
    os.environ["PORTFOLIO_DIR"] = tmp
    try:
        at = run()
        check("an empty folder is reported, not raised",
              not at.exception, str([e.value for e in at.exception]))
        check("...with a message naming the folder",
              any(str(tmp) in e.value for e in at.error), str([e.value for e in at.error]))
    finally:
        if previous is None:
            os.environ.pop("PORTFOLIO_DIR", None)
        else:
            os.environ["PORTFOLIO_DIR"] = previous

# --- switching files drops the other file's ticks -----------------------------
# Two files with the same asset key: the tick belongs to the file it was made
# in, and the table's own edits are remembered by row position, so they must not
# survive the switch either.
with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / "one.yaml").write_text(
        "currencies: [AUD]\nassets:\n  - {key: a, type: cash, currency: AUD}\n"
        "snapshots:\n  - date: '2026-09-01'\n    fx: {AUD: {AUD: 1.0}}\n"
        "    assets: [{ref: a, value: 100}]\n"
    )
    (Path(tmp) / "two.yaml").write_text(
        "currencies: [AUD]\nassets:\n  - {key: a, type: cash, currency: AUD}\n"
        "snapshots:\n  - date: '2026-09-01'\n    fx: {AUD: {AUD: 1.0}}\n"
        "    assets: [{ref: a, value: 500}]\n"
    )
    previous = os.environ.get("PORTFOLIO_DIR")
    os.environ["PORTFOLIO_DIR"] = tmp
    try:
        at = run()
        check("both files are offered", at.sidebar.selectbox[0].options == ["one", "two"],
              str(at.sidebar.selectbox[0].options))
        at.session_state["excluded"] = {"a"}
        at.run()
        rows = table_records(at.dataframe[0])
        check("the tick is applied in the first file", rows[1]["In total"] is False,
              str(rows[1]["In total"]))
        check("...and the total says so", "excluding 1 asset" in rows[0]["Asset"],
              rows[0]["Asset"])
        at.sidebar.selectbox[0].set_value("two").run()
        check("switching file with a tick applied does not raise", not at.exception,
              str([e.value for e in at.exception]))
        rows = table_records(at.dataframe[0])
        check("the other file is the other file", rows[0]["2026-09-01"] == 500,
              str(rows[0]))
        check("...with its own tick left alone",
              rows[1]["In total"] is True, str(rows[1]["In total"]))
        check("...so nothing is left out of its totals",
              rows[0]["Asset"] == "Total (AUD)", rows[0]["Asset"])
        check("...and it does not inherit the other file's notice", not at.info,
              str([i.value for i in at.info]))
    finally:
        if previous is None:
            os.environ.pop("PORTFOLIO_DIR", None)
        else:
            os.environ["PORTFOLIO_DIR"] = previous

# --- a portfolio with no snapshots yet ----------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / "empty.yaml").write_text("currencies: [AUD]\nassets:\n"
                                          "  - {key: a, type: cash, currency: AUD}\n")
    previous = os.environ.get("PORTFOLIO_DIR")
    os.environ["PORTFOLIO_DIR"] = tmp
    try:
        at = run()
        check("a portfolio with no snapshots is reported, not raised",
              not at.exception, str([e.value for e in at.exception]))
        check("...as a warning", any("no snapshots" in w.value for w in at.warning),
              str([w.value for w in at.warning]))
    finally:
        if previous is None:
            os.environ.pop("PORTFOLIO_DIR", None)
        else:
            os.environ["PORTFOLIO_DIR"] = previous

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")