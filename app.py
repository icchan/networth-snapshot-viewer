"""Portfolio overview: assets down, snapshot dates across.

Run with ``streamlit run app.py``.

Presentation only. Every number comes from the model -- :meth:`Portfolio.total_in`
and :meth:`Portfolio.value_in` in ``snapshots.py`` -- because an FX conversion
whose direction is wrong still renders as a perfectly plausible-looking table.
"""

import datetime as dt
import math
import os
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

import market
from fx import FxError
from labels import TYPE_EMOJI, row_label
from snapshots import PORTFOLIO_DIR, PortfolioError, SnapshotError, list_portfolios, load

st.set_page_config(page_title="Portfolio", layout="wide")

# Overridable so the page can be pointed at another folder -- and so the tests
# can exercise the broken-file path against a throwaway directory instead of
# the real one.
DATA_DIR = Path(os.environ.get("PORTFOLIO_DIR") or PORTFOLIO_DIR)

# Whole units only. Nothing is shown to the cent, so the JPY special case that
# existed when other currencies showed 2dp is gone -- every currency is the same.
def whole(amount: float | None) -> int | None:
    """Round to a whole unit for display, keeping ``None`` so the cell stays blank.

    Rounding happens here rather than in a format string because ``localized``
    follows the value's own precision, which for a converted total is all 17
    significant digits of a float. Returning an ``int`` rather than a
    ``float`` keeps Streamlit from appending a trailing ``.0``.

    Rounded half away from zero rather than with :func:`round`, which is
    half-to-even and would make ``2.5`` display as ``2`` but ``3.5`` as ``4``.
    """
    if amount is None:
        return None
    magnitude = math.floor(abs(amount) + 0.5)
    return magnitude if amount >= 0 else -magnitude





st.sidebar.title("Portfolio")

handles = list_portfolios(DATA_DIR)
if not handles:
    st.error(f"No portfolio files in `{DATA_DIR}`.")
    st.stop()

handle = st.sidebar.selectbox("File", handles)
st.sidebar.caption(f"Reading from `{DATA_DIR}`")

try:
    portfolio = load(handle, directory=DATA_DIR)
except (PortfolioError, SnapshotError) as exc:
    st.error(f"`{handle}.yaml` did not load: {exc}")
    st.stop()

if not portfolio.snapshots:
    st.warning(f"`{handle}` has no snapshots yet.")
    st.stop()

currency = st.sidebar.radio("Show amounts in", portfolio.currencies)

# Newest first: the question this page answers is "what does it look like now".
snaps = sorted(portfolio.snapshots, key=lambda s: s.date, reverse=True)
latest = snaps[0]

st.title(portfolio.label or handle)


# --- what counts towards the totals ------------------------------------------

# Every total on this page can leave assets out: untick "In total" on an asset's
# row in the table below and that holding stops counting, tick it again and it
# counts as it did before. Streamlit re-runs the whole script on every
# interaction, so the set lives in session state -- held per file, because two
# portfolios can share asset keys and one file's ticks must not follow the user
# into another.
if st.session_state.get("excluded_for", handle) != handle:
    st.session_state["excluded"] = set()
    # The table remembers its ticks by row position, so ticks left over from the
    # other file would land on whatever asset now happens to sit in that row.
    st.session_state.pop("assets_editor", None)
st.session_state["excluded_for"] = handle
excluded: set[str] = st.session_state.setdefault("excluded", set())
# An asset taken out of the file is no longer an asset, ticked out or not.
excluded.intersection_update({spec.key for spec in portfolio.specs})

# Currency, then type. Sorted on the spec fields rather than on the rendered label:
# a string sort would order by the leading emoji's code point, which groups nothing.
# Ties keep catalogue order, so related entries stay where the file put them.
ordered = sorted(portfolio.specs, key=lambda spec: (spec.currency, spec.type))

# What every total below is summed over: the whole catalogue, less whatever has
# been ticked out of the table.
included = {spec.key for spec in ordered} - excluded


# --- today's revaluation, which is not a snapshot -----------------------------

TODAY = dt.date.today().isoformat()
TODAY_COL = "Today"

# Cached because the currency selector re-runs the script on every click, and
# refetching every close each time would be slow and rude to the price source.
# Keyed on the day, so it refreshes itself as dates roll over.
@st.cache_data(ttl=900, show_spinner=False)
def today_marks(currencies: tuple, tickers: tuple, on: str):
    try:
        return market.fetch_marks(list(currencies), list(tickers), on), ""
    except (FxError, market.PriceError) as exc:
        return None, str(exc)


marks, marks_error = today_marks(
    tuple(portfolio.currencies),
    tuple(market.tickers_to_mark(portfolio, latest)),
    TODAY,
)

live = None
live_notes: list[tuple[str, str]] = []
if marks is not None:
    live, live_notes = market.virtual_snapshot(portfolio, latest, marks)

if marks_error:
    st.warning(
        f"Today's column is unavailable ({marks_error}). The snapshots below are "
        "unaffected."
    )

def totals_for(target: str) -> dict[str, float | None]:
    """Total per snapshot in ``target``, blank where the model refuses to sum."""
    out: dict[str, float | None] = {}
    for snap in snaps:
        try:
            out[snap.date] = portfolio.total_in(snap, target, only=included)
        except SnapshotError as exc:
            out[snap.date] = None
            st.warning(f"{snap.date}: {exc}")
    if live is not None:
        # Strict, like every other total: one unpriced holding means the total
        # is short by an unknown amount, so it is blank rather than a lie.
        try:
            out[TODAY_COL] = portfolio.total_in(live, target, only=included)
        except SnapshotError:
            out[TODAY_COL] = None
    return out


def date_columns(target: str) -> dict:
    out: dict = {}
    if live is not None:
        out[TODAY_COL] = st.column_config.NumberColumn(
            TODAY_COL,
            format="localized",
            help=(
                f"Not a saved snapshot: {latest.date}'s holdings, re-marked and "
                f"re-converted for {TODAY} and converted to {target}."
            ),
        )
    for snap in snaps:
        out[snap.date] = st.column_config.NumberColumn(
            snap.date,
            format="localized",
            help=f"Value on {snap.date}, converted to {target}",
        )
    return out


# --- assets down, dates across -----------------------------------------------

st.subheader(f"Assets over time ({currency})")


def cell(spec_key: str, snap, target: str) -> int | None:
    """One displayed amount from a snapshot, rounded for display."""
    return whole(portfolio.value_in(snap, spec_key, target))


def build_row(heading: str, spec_key: str | None) -> dict:
    """A table row: Today first when there is a live view, then the snapshots.

    The key is omitted rather than filled with a blank when there is nothing to
    show, so the column disappears outright. An empty column would read as
    "no value today", which is not what "there were no rates today" means.
    """
    if spec_key is None:
        row: dict = {}
        if live is not None:
            row[TODAY_COL] = whole(selected.get(TODAY_COL))
        for snap in snaps:
            row[snap.date] = whole(selected[snap.date])
        return {"Asset": heading, **row}
    row = {}
    if live is not None:
        row[TODAY_COL] = cell(spec_key, live, currency)
    for snap in snaps:
        row[snap.date] = cell(spec_key, snap, currency)
    return {"Asset": heading, **row}


def shown_label(spec) -> str:
    """The row's label, marked in place when the asset is not being counted."""
    label = row_label(spec)
    return label if spec.key not in excluded else f"🚫 {label} (excluded)"


def total_heading() -> str:
    """``Total (AUD)``, naming how much has been ticked out when something has been."""
    if not excluded:
        return f"Total ({currency})"
    n = len(excluded)
    return f"Total excluding {n} asset{'' if n == 1 else 's'} ({currency})"


def apply_toggles() -> None:
    """Fold the ticks just clicked in the table into :data:`excluded`.

    Streamlit runs a widget's callback before the script body, so ``excluded``
    already holds the click that triggered this run by the time the rows are
    built below -- the total on screen never lags a click behind.

    The table reports edits as row position -> column -> value, and its rows are
    ``ordered`` with the total first, so row ``i`` is ``ordered[i - 1]``.
    """
    state = st.session_state.get("assets_editor")
    if not state:
        return
    for row, change in state.get("edited_rows", {}).items():
        tick = change.get("In total")
        if tick is None or not 0 < row <= len(ordered):
            continue  # row 0 is the total, which is not an asset
        key = ordered[row - 1].key
        if tick:
            excluded.discard(key)
        else:
            excluded.add(key)


# First row, as asked: the total in the selected currency.
selected = totals_for(currency)
rows = [
    # The total has nothing to tick, so its cell is left empty rather than
    # offered a tick that would mean nothing.
    {"In total": None, **build_row(total_heading(), None)},
    *[{"In total": spec.key not in excluded, **build_row(shown_label(spec), spec.key)}
      for spec in ordered],
]
st.data_editor(
    rows,
    hide_index=True,
    width="stretch",
    key="assets_editor",
    on_change=apply_toggles,
    # Everything but the tick column is for reading: the amounts come from the
    # file and the totals from the model, so there is nothing to type here.
    disabled=[column for column in rows[0] if column != "In total"],
    column_config={
        "In total": st.column_config.CheckboxColumn(
            "In total",
            help="Untick to leave this asset out of every total on the page; "
                 "tick it again to count it. Rows that are out of the totals are "
                 "marked 🚫 and named below the table.",
            width="small",
        ),
        "Asset": st.column_config.TextColumn("Asset", width="large"),
        **date_columns(currency)},
)
if excluded:
    st.info(
        "Not in the totals: "
        + ", ".join(f"🚫 {row_label(spec)}" for spec in ordered if spec.key in excluded)
        + ". Tick 'In total' on the row to count it again."
    )

st.caption(
    "Every column except the first converts at that date's own rates, not today's, so "
    "the snapshots stay comparable over time instead of being restated into the "
    "present. A blank means the asset was not held on that date, or is held with no "
    "amount yet -- not zero. "
    "Every amount is a whole unit, to keep the table readable at a glance; the "
    "file keeps full precision and nothing is rounded on disk. Totals are added "
    "up before rounding, so they can differ from the sum of the cells above.\n\n"
    "Untick 'In total' on a row to leave that asset out of every total on this "
    "page. The row stays visible, marked 🚫, and ticking it again puts the "
    "holding back into the totals.\n\n"
    "Each row is marked with its type and the country of its currency: "
    + ", ".join(f"{e} {t}" for t, e in sorted(TYPE_EMOJI.items()))
    + ". The flag comes from the currency, so it is an approximation."
)

if live is not None:
    marked_dates = sorted(
        {n.detail.rsplit(" on ", 1)[1] for n in live_notes
         if n.kind == market.MARKED and " on " in n.detail}
    )
    n_marked = sum(1 for n in live_notes if n.kind == market.MARKED)
    n_face = sum(1 for n in live_notes if n.kind == market.FACE)
    n_blank = sum(1 for n in live_notes if n.kind == market.UNRESOLVED)

    def held(n: int, noun: str = "holding") -> str:
        return f"{n} {noun}{'' if n == 1 else 's'}"

    breakdown = [f"{held(n_marked)} re-marked at today's closes"]
    if n_face:
        breakdown.append(
            f"{held(n_face)} carried at face value -- cash, loans and cards are "
            "amounts, not quantities, so there is nothing to mark to market"
        )
    if n_blank:
        breakdown.append(f"{held(n_blank)} left blank, because no close came back")
    fx_note = (
        f"rates as at {marks.fx_date}" if not marks.fx_stale
        else f"no rates published for {TODAY} (weekend or holiday), so it uses {marks.fx_date}"
    )
    st.caption(
        f"**{TODAY_COL}** is not a snapshot and is never written to the file. It holds "
        f"{latest.date}'s holdings -- the same quantities, unchanged -- but re-marked at "
        f"today's closes ({', '.join(marked_dates) or 'none'}) and converted at {fx_note}. "
        f"Of the {held(len(live_notes))}: " + "; ".join(breakdown) + "."
    )
    if marks.failures:
        st.warning(
            "No close today for "
            + ", ".join(f"`{t}` ({why})" for t, why in marks.failures.items())
            + ". Those cells and the total are blank rather than valued at the last "
              "snapshot's price, which would mix today's rates with an older close."
        )

# --- the same totals in every currency ----------------------------------------

st.subheader("Total in each currency")
# Computed once per currency, not once per cell: totals_for warns as it goes.
per_currency = {c: totals_for(c) for c in portfolio.currencies}
today_row = {"Date": f"{TODAY} (live)",
             **{c: whole(per_currency[c].get(TODAY_COL)) for c in portfolio.currencies}}
st.dataframe(
    [{"Date": s.date,
      **{c: whole(per_currency[c][s.date]) for c in portfolio.currencies}}
     for s in snaps] + ([today_row] if live is not None else []),
    hide_index=True,
    width="stretch",
    column_config={
        "Date": st.column_config.TextColumn("Date"),
        **{c: st.column_config.NumberColumn(
            c, format="localized",
            help=f"Total on that date, expressed in {c}") for c in portfolio.currencies},
    },
)

st.caption(
    "Liabilities (negative amounts) subtract, so a total is a net position. "
    "The columns will not agree with each other: they are separate conversions "
    "from the same date, and the rate matrix is only accurate to about 0.1%."
    + (" Assets ticked out of the table above are left out of these totals too."
       if excluded else "")
)