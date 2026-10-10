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

import pandas as pd  # comes with streamlit
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

import market
from fx import FxError
from labels import TYPE_EMOJI, row_label
from snapshots import (
    PORTFOLIO_DIR,
    AssetSpec,
    PortfolioError,
    SnapshotError,
    list_portfolios,
    load,
)

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
    # The workings panel remembers a row position too, and means nothing in the
    # other file for exactly the same reason.
    st.session_state.pop("inspect", None)
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
        f"Today's column is unavailable ({marks_error}). The snapshots below are unaffected."
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


def _col_chart(df: pd.DataFrame, title: str):
    import streamlit as st

    # Using built-in streamlit chart
    st.bar_chart(df.set_index(df.columns[0]), use_container_width=True)
    st.caption(title)


# Add small pie charts above the assets table
alt = None
try:
    # Use latest snapshot's converted values in selected currency for included assets
    latest_snap = latest
    rows_pie = []
    for spec in ordered:
        if spec.key in excluded:
            continue
        v = portfolio.value_in(latest_snap, spec.key, currency)
        if v is not None and abs(v) > 0:  # only non-zero
            rows_pie.append({"asset": row_label(spec), "value": whole(v) or 0})
    if rows_pie:
        pie_df = pd.DataFrame(rows_pie)
        # Group by currency and type for the two charts
        # Build df with groupings
        # Currency grouping
        cgroups = []
        for spec in ordered:
            if spec.key in excluded:
                continue
            v = portfolio.value_in(latest_snap, spec.key, currency)
            if v is not None and abs(v) > 0:
                cgroups.append({"group": spec.currency, "value": whole(v) or 0})
        tgroups = []
        for spec in ordered:
            if spec.key in excluded:
                continue
            v = portfolio.value_in(latest_snap, spec.key, currency)
            if v is not None and abs(v) > 0:
                type_label = "Stock" if spec.type == "stocks" else spec.type.title()
                tgroups.append(
                    {
                        "group": f"{spec.currency} {type_label}",
                        "value": whole(v) or 0,
                    }
                )
        try:
            import altair as alt
        except Exception:
            alt = None
        col1, col2 = st.columns(2)
        cdf = pd.DataFrame(cgroups).groupby("group", as_index=False)["value"].sum()
        tdf = pd.DataFrame(tgroups).groupby("group", as_index=False)["value"].sum()
        if alt is not None:
            # Add percentage and formatted display
            if cdf["value"].sum() > 0:
                cdf["pct"] = cdf["value"] / cdf["value"].sum() * 100
                cdf["pct_lbl"] = cdf["pct"].map(lambda x: f"{x:.1f}%")
            else:
                cdf["pct"] = 0
                cdf["pct_lbl"] = "0%"
            if tdf["value"].sum() > 0:
                tdf["pct"] = tdf["value"] / tdf["value"].sum() * 100
                tdf["pct_lbl"] = tdf["pct"].map(lambda x: f"{x:.1f}%")
            else:
                tdf["pct"] = 0
                tdf["pct_lbl"] = "0%"
            cdf["label"] = cdf["group"].astype(str) + "\n" + cdf["pct_lbl"]
            tdf["label"] = tdf["group"].astype(str) + "\n" + tdf["pct_lbl"]
            with col2:
                st.subheader("By currency")
                base = alt.Chart(cdf).encode(
                    theta=alt.Theta("value:Q", stack=True),
                    color=alt.Color("group:N", legend=None, scale=alt.Scale(scheme="tableau10")),
                    tooltip=[
                        alt.Tooltip("group:N", title="Currency"),
                        alt.Tooltip("value:Q", format=",d", title="Value"),
                    ],
                )
                pie = base.mark_arc(innerRadius=45)
                label_base = alt.Chart(cdf).encode(theta=alt.Theta("value:Q", stack=True))
                text_lbl = label_base.mark_text(
                    radius=80,
                    align="center",
                    baseline="middle",
                    fill="#000000",
                    lineBreak="\n",
                    fontSize=13,
                ).encode(text=alt.Text("label:N"))
                st.altair_chart((pie + text_lbl).properties(width=240, height=240))
            with col1:
                st.subheader("By currency and asset type")
                base = alt.Chart(tdf).encode(
                    theta=alt.Theta("value:Q", stack=True),
                    color=alt.Color("group:N", legend=None, scale=alt.Scale(scheme="set2")),
                    tooltip=[
                        alt.Tooltip("group:N", title="Type"),
                        alt.Tooltip("value:Q", format=",d", title="Value"),
                    ],
                )
                pie = base.mark_arc(innerRadius=45)
                label_base = alt.Chart(tdf).encode(theta=alt.Theta("value:Q", stack=True))
                text_lbl = label_base.mark_text(
                    radius=80,
                    align="center",
                    baseline="middle",
                    fill="#000000",
                    lineBreak="\n",
                    fontSize=13,
                ).encode(text=alt.Text("label:N"))
                st.altair_chart((pie + text_lbl).properties(width=240, height=240))
        else:
            # Fallback: just show the tables as pie-like summaries? Or skip
            pass
except Exception:
    pass


# --- assets down, dates across -----------------------------------------------

st.subheader(f"Assets over time ({currency})")


def cell(spec_key: str, snap, target: str) -> int | None:
    """One displayed amount from a snapshot, rounded for display."""
    return whole(portfolio.value_in(snap, spec_key, target))


def build_row(heading: str, spec_key: str | None, how: str) -> dict:
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
        return {"Asset": heading, "How": how, **row}
    row = {}
    if live is not None:
        row[TODAY_COL] = cell(spec_key, live, currency)
    for snap in snaps:
        row[snap.date] = cell(spec_key, snap, currency)
    return {"Asset": heading, "How": how, **row}


def workings_row(when: str, snap, spec: AssetSpec) -> dict:
    """One snapshot's inputs and result as a row in the workings table."""
    holding = next((a for a in snap.assets if a.ref == spec.key), None)
    source = spec.currency
    live_view = snap is live
    note = next((n for n in live_notes if n.ref == spec.key), None)
    price_column = f"{spec.ticker} price" if spec.ticker else "Price"
    total_column = f"Total in {source}"
    rate_column = f"{source}→{currency} rate"
    final_column = f"Final value in {currency}"
    shown_column = "Shown in table"
    row = {
        "Snapshot": when,
        "Units": None,
        price_column: None,
        total_column: None,
        rate_column: None,
        final_column: None,
        shown_column: None,
        "Note": "",
    }
    if holding is None:
        row["Note"] = "Not held"
        return row
    row[rate_column] = snap.fx[source][currency]

    amount = holding.resolved_value
    if holding.value is not None:
        row[total_column] = holding.value
        row["Note"] = "Flat amount, carried at face value" if live_view else "Flat amount in file"
    else:
        row["Units"] = holding.units
        row[price_column] = holding.price
        row[total_column] = amount

    if amount is None:
        row["Note"] = (
            note.detail
            if live_view and note is not None and note.kind == market.UNRESOLVED
            else "Units with no price"
        )
        return row

    result = portfolio.value_in(snap, spec.key, currency)
    row[final_column] = result
    row[shown_column] = whole(result)
    if live_view:
        live_details = []
        if note is not None and " on " in note.detail:
            live_details.append(f"{spec.ticker}'s close on {note.detail.rsplit(' on ', 1)[1]}")
        if source != currency:
            live_details.append(f"rates as at {marks.fx_date}")
        if live_details:
            row["Note"] = "; ".join(filter(None, [row["Note"], *live_details]))
    return row


def workings(spec: AssetSpec) -> list[dict]:
    """One table row per saved snapshot, with Today first when available."""
    columns: list[tuple[str, object]] = [] if live is None else [(TODAY_COL, live)]
    columns += [(snap.date, snap) for snap in snaps]
    return [workings_row(when, snap, spec) for when, snap in columns]


# A row ticked out of the totals is greyed where it stands. A pandas Styler is
# the only row-level styling Streamlit accepts, and the grid honours it only on
# non-editable cells -- so the tick column below keeps its normal look and the
# toggle stays clickable while the rest of the row dims.
GREY = "color: #8a8f98; background-color: rgba(128,128,128,0.12)"
LIVE = "background-color: rgba(62,116,253,0.1)"
TOTAL = "background-color: rgba(255,193,7,0.2)"
TOTAL_TODAY = f"{TOTAL}; font-weight: bold"


def grey_out(row) -> list[str]:
    """Grey every cell of a row that has been ticked out of the totals.

    The tick column holds ``True``/``False`` per asset and ``None`` on the
    total row, so only an explicit ``False`` matches -- row 0 never greys.
    """
    if row["In total"] is False:
        return [GREY] * len(row)
    if row.name == 0:
        return [TOTAL] * len(row)
    return [""] * len(row)


def show(value) -> str:
    """How one cell reads now that the table is handed a Styler.

    The Styler takes display away from the grid's own number formatting, so
    this restates what the page always showed: a whole unit with thousands
    separators. ``None`` and NaN never arrive -- ``na_rep=""`` below turns
    them into the blank that has always meant "not held, or no amount yet".
    """
    if isinstance(value, (bool, str)):
        return str(value)
    return f"{whole(value):,}"


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


def open_workings() -> None:
    """Note which row's button was clicked, so the panel below knows what to open.

    Runs before the script body, like :func:`apply_toggles`, and receives the
    same thing the editor's edits are: a position in the table, total first.
    The position is mapped back to an asset only when the panel renders, so a
    stale one is caught there rather than here. Clicking the same row again
    closes what that row opened.
    """
    click = st.session_state.get("inspect_btn")
    if not click:
        return
    row = click["row"]
    st.session_state["inspect"] = None if st.session_state.get("inspect") == row else row


# First row, as asked: the total in the selected currency.
selected = totals_for(currency)
rows = [
    # The total has nothing to tick and no workings of its own, so both action
    # columns leave its cell empty rather than offering a control that would
    # mean nothing.
    {"In total": None, **build_row(total_heading(), None, "")},
    *[
        {
            "In total": spec.key not in excluded,
            **build_row(row_label(spec), spec.key, ":material/info:"),
        }
        for spec in ordered
    ],
]
table = pd.DataFrame(rows)
style = table.style.apply(grey_out, axis=1).format(show, na_rep="")
if live is not None:
    # Style the Today column cells (pandas API compatible)
    style = style.apply(
        lambda row: [TOTAL_TODAY if row.name == 0 else LIVE] * len(row),
        axis=1,
        subset=[TODAY_COL],
    )
st.data_editor(
    # Styler on the way in: the greying rides on it, and so does how every
    # cell reads (see `show`). The raw values below it are untouched.
    style,
    hide_index=True,
    width="stretch",
    key="assets_editor",
    on_change=apply_toggles,
    # Everything but the tick and workings columns is for reading: the amounts
    # come from the file and the totals from the model, so there is nothing to
    # type here. The button stays out of the list because disabled columns are
    # not clickable, and clicking is the one thing it is for.
    disabled=[column for column in rows[0] if column not in ("In total", "How")],
    column_config={
        "In total": st.column_config.CheckboxColumn(
            "In total",
            help="Untick to leave this asset out of every total on the page; "
            "tick it again to count it. Rows that are out of the totals "
            "are greyed out and named below the table.",
            width="small",
        ),
        "Asset": st.column_config.TextColumn("Asset", width="large"),
        "How": st.column_config.ButtonColumn(
            "How",
            help="Open a panel below the table with the numbers behind this "
            "row: units, price and rate, one line per date.",
            width="small",
            alignment="center",
            type="tertiary",
            on_click=open_workings,
            key="inspect_btn",
        ),
        **date_columns(currency),
    },
)
if excluded:
    st.info(
        "Not in the totals: "
        + ", ".join(row_label(spec) for spec in ordered if spec.key in excluded)
        + ". Tick 'In total' on the row to count it again."
    )

# The panel the workings button opens: one calculation row per snapshot
# above. A remembered position that no longer maps to a row -- the file changed
# underneath between runs -- is ignored rather than opened for the wrong asset.
inspect_row = st.session_state.get("inspect")
if isinstance(inspect_row, int) and 0 < inspect_row <= len(ordered):
    inspected = ordered[inspect_row - 1]
    with st.container(border=True):
        st.markdown(f"**How {row_label(inspected)} was valued (in {currency})**")
        workings_rows = workings(inspected)
        price_column = f"{inspected.ticker} price" if inspected.ticker else "Price"
        numeric_columns = [
            "Units",
            price_column,
            f"Total in {inspected.currency}",
            f"{inspected.currency}→{currency} rate",
            f"Final value in {currency}",
            "Shown in table",
        ]
        st.dataframe(
            pd.DataFrame(workings_rows),
            hide_index=True,
            width="stretch",
            column_config={
                name: st.column_config.NumberColumn(name, format="localized")
                for name in numeric_columns
            },
        )
        st.caption(
            "One row per snapshot. Values retain their recorded precision; "
            "'Shown in table' is rounded to a whole unit. Click the same button "
            "again to close this."
        )

with st.expander("About this table", expanded=False):
    st.caption(
        "Every column except the first converts at that date's own rates, not today's, so "
        "the snapshots stay comparable over time instead of being restated into the "
        "present. A blank means the asset was not held on that date, or is held with no "
        "amount yet -- not zero. "
        "Every amount is a whole unit, to keep the table readable at a glance; the "
        "file keeps full precision and nothing is rounded on disk. Totals are added "
        "up before rounding, so they can differ from the sum of the cells above.\n\n"
        "Untick 'In total' on a row to leave that asset out of every total on this "
        "page. The row stays visible but greyed out, and ticking it again puts the "
        "holding back into the totals.\n\n"
        "Every asset row ends with a :material/info: button: click it to open a "
        "panel below the table with that row's arithmetic -- units, price and rate, "
        "one line per date, ending in the whole unit the table shows.\n\n"
        "Each row is marked with its type and the country of its currency: "
        + ", ".join(f"{e} {t}" for t, e in sorted(TYPE_EMOJI.items()))
        + ". The flag comes from the currency, so it is an approximation."
    )

if live is not None:
    marked_dates = sorted(
        {
            n.detail.rsplit(" on ", 1)[1]
            for n in live_notes
            if n.kind == market.MARKED and " on " in n.detail
        }
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
        f"rates as at {marks.fx_date}"
        if not marks.fx_stale
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

# The live row leads this table and is tinted, so "not a snapshot" reads at a
# glance -- the same Styler channel the greying above uses. The accent is
# translucent, so it lands on both the light and the dark theme.
# (moved up with GREY)


def tint_live(row) -> list[str]:
    """Tint the leading live row; the snapshot rows stay plain.

    Row 0 is the live row exactly when the construction below put it there --
    with no live view, row 0 is a snapshot and must not be tinted.
    """
    if live is not None and row.name == 0:
        return [LIVE] * len(row)
    return [""] * len(row)


type_snapshot_rows = []
snapshot_dates = sorted(snap.date for snap in portfolio.snapshots)
for snap in sorted(portfolio.snapshots, key=lambda item: item.date):
    values_by_type: dict[str, float] = {}
    for spec in ordered:
        if spec.key not in included:
            continue
        value = portfolio.value_in(snap, spec.key, currency)
        if value is not None:
            values_by_type[spec.type] = values_by_type.get(spec.type, 0) + value
    type_snapshot_rows.extend(
        {"date": snap.date, "type": asset_type, "value": value}
        for asset_type, value in values_by_type.items()
    )

if type_snapshot_rows and alt is not None:
    type_snapshot_chart = (
        alt.Chart(pd.DataFrame(type_snapshot_rows))
        .mark_bar()
        .encode(
            x=alt.X("date:O", sort=snapshot_dates, title="Snapshot"),
            y=alt.Y("value:Q", stack="zero", title=f"Value ({currency})"),
            color=alt.Color("type:N", title="Asset type", scale=alt.Scale(scheme="dark2")),
            tooltip=[
                alt.Tooltip("date:O", title="Snapshot"),
                alt.Tooltip("type:N", title="Asset type"),
                alt.Tooltip("value:Q", format=",.2f", title=f"Value ({currency})"),
            ],
        )
        .properties(title=f"Monthly snapshots by type ({currency})", height=320)
    )
    st.altair_chart(type_snapshot_chart, use_container_width=True)


st.subheader("Total in each currency")
# Computed once per currency, not once per cell: totals_for warns as it goes.
per_currency = {c: totals_for(c) for c in portfolio.currencies}
# What it looks like now comes first -- the same question the page answers up
# top -- then the snapshots run newest first.
currency_rows = (
    [
        {
            "Date": f"{TODAY} (live)",
            **{c: whole(per_currency[c].get(TODAY_COL)) for c in portfolio.currencies},
        }
    ]
    if live is not None
    else []
) + [
    {"Date": s.date, **{c: whole(per_currency[c][s.date]) for c in portfolio.currencies}}
    for s in snaps
]
st.dataframe(
    # Styler carries the tint and -- as on the assets table -- how cells read.
    pd.DataFrame(currency_rows).style.apply(tint_live, axis=1).format(show, na_rep=""),
    hide_index=True,
    width="stretch",
    column_config={
        "Date": st.column_config.TextColumn("Date"),
        **{
            c: st.column_config.NumberColumn(
                c, format="localized", help=f"Total on that date, expressed in {c}"
            )
            for c in portfolio.currencies
        },
    },
)

st.caption(
    "Liabilities (negative amounts) subtract, so a total is a net position. "
    "The columns will not agree with each other: they are separate conversions "
    "from the same date, and the rate matrix is only accurate to about 0.1%."
    + (
        " Assets ticked out of the table above are left out of these totals too."
        if excluded
        else ""
    )
)
