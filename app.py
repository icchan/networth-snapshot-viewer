"""Portfolio overview: assets down, snapshot dates across.

Run with ``uv run streamlit run app.py``.

Presentation only. Every number comes from the model -- :meth:`Portfolio.total_in`
and :meth:`Portfolio.value_in` in ``snapshots.py`` -- because an FX conversion
whose direction is wrong still renders as a perfectly plausible-looking table.
"""

import datetime as dt
import os
import sys
from pathlib import Path

import pandas as pd  # comes with streamlit
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

import market
from fx import FxError
from labels import TYPE_EMOJI, row_label
from portfolio_view import (
    build_asset_rows,
    build_currency_rows,
    build_pie_frames,
    build_snapshot_editor_rows,
    build_snapshot_type_rows,
    build_workings_rows,
    holdings_from_editor,
    snapshot_month_options,
    whole,
)
from snapshots import (
    PORTFOLIO_DIR,
    Portfolio,
    PortfolioError,
    Snapshot,
    SnapshotError,
    create_snapshot,
    list_portfolios,
    load,
    save,
)

TODAY_COL = "Today"
GREY = "color: #8a8f98; background-color: rgba(128,128,128,0.12)"
LIVE = "background-color: rgba(62,116,253,0.1)"
TOTAL = "background-color: rgba(255,193,7,0.2)"
TOTAL_TODAY = f"{TOTAL}; font-weight: bold"


def render_portfolio_title(title: str) -> None:
    st.title(title)


def render_snapshot_entry(portfolio: Portfolio, handle: str, today: str) -> None:
    saved_date = st.session_state.pop("snapshot_saved_date", None)
    if saved_date:
        st.success(f"Snapshot saved for {saved_date}.")

    months = snapshot_month_options(portfolio.snapshots, today)
    with st.expander("Enter or edit snapshot", expanded=False):
        selected_month = st.selectbox(
            "Snapshot month",
            months,
            key=f"snapshot_month_{handle}",
        )
        matches = [
            snapshot for snapshot in portfolio.snapshots if snapshot.date[:7] == selected_month
        ]
        if len(matches) > 1:
            snapshot_dates = [
                snapshot.date for snapshot in sorted(matches, key=lambda item: item.date)
            ]
            selected_date = st.selectbox(
                "Snapshot date",
                snapshot_dates,
                index=len(snapshot_dates) - 1,
                key=f"snapshot_date_{handle}_{selected_month}",
            )
            existing = next(snapshot for snapshot in matches if snapshot.date == selected_date)
        elif matches:
            existing = matches[0]
            selected_date = existing.date
        else:
            existing = None
            selected_date = f"{selected_month}-01"

        st.caption(
            "Enter a value or units and price. For tickered assets, leave Price override blank "
            "to fetch the historical close when saving."
        )
        editor_key = f"snapshot_entries_{handle}_{selected_date}"
        editor_data = pd.DataFrame(
            build_snapshot_editor_rows(portfolio.specs, existing, row_label)
        ).set_index("key")
        with st.form(f"snapshot_form_{handle}_{selected_date}"):
            edited = st.data_editor(
                editor_data,
                hide_index=True,
                width="stretch",
                key=editor_key,
                column_order=["Asset", "Held", "Entry", "Value", "Units", "Price override"],
                disabled=["Asset"],
                column_config={
                    "Asset": st.column_config.TextColumn("Asset", width="large"),
                    "Held": st.column_config.CheckboxColumn("Held", width="small"),
                    "Entry": st.column_config.SelectboxColumn(
                        "Entry",
                        options=["Value", "Units & price"],
                        width="medium",
                    ),
                    "Value": st.column_config.NumberColumn("Value", format="localized"),
                    "Units": st.column_config.NumberColumn("Units", format="localized"),
                    "Price override": st.column_config.NumberColumn(
                        "Price override",
                        help="Optional for tickered assets; required for units without a ticker.",
                        format="localized",
                    ),
                },
            )
            submitted = st.form_submit_button(
                "Update snapshot" if existing is not None else "Save snapshot",
                type="primary",
            )

        if not submitted:
            return

        holdings, validation_errors = holdings_from_editor(edited, portfolio.specs)
        if validation_errors:
            st.error("Please fix these entries: " + "; ".join(validation_errors))
            return

        previous_snapshots = list(portfolio.snapshots)
        try:
            with st.spinner("Fetching snapshot rates and ticker prices..."):
                if existing is None:
                    portfolio.add_snapshot(selected_date, holdings)
                else:
                    replacement = create_snapshot(
                        selected_date,
                        holdings,
                        portfolio.currencies,
                        portfolio.specs,
                        fx=existing.fx,
                    )
                    portfolio.snapshots = sorted(
                        [
                            snapshot
                            for snapshot in portfolio.snapshots
                            if snapshot.date != selected_date
                        ]
                        + [replacement],
                        key=lambda snapshot: snapshot.date,
                    )
                save(portfolio)
        except (SnapshotError, FxError, OSError) as exc:
            portfolio.snapshots = previous_snapshots
            st.error(f"Could not save {selected_date}: {exc}")
            return

        st.session_state["snapshot_saved_date"] = selected_date
        st.rerun()


def render_pie_charts(frames, alt) -> None:
    if frames is None or alt is None:
        return
    currency_frame, type_frame = frames
    col1, col2 = st.columns(2)
    with col2:
        st.subheader("By currency")
        base = alt.Chart(currency_frame).encode(
            theta=alt.Theta("value:Q", stack=True),
            color=alt.Color("group:N", legend=None, scale=alt.Scale(scheme="tableau10")),
            tooltip=[
                alt.Tooltip("group:N", title="Currency"),
                alt.Tooltip("value:Q", format=",d", title="Value"),
            ],
        )
        pie = base.mark_arc(innerRadius=45)
        label_base = alt.Chart(currency_frame).encode(theta=alt.Theta("value:Q", stack=True))
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
        base = alt.Chart(type_frame).encode(
            theta=alt.Theta("value:Q", stack=True),
            color=alt.Color("group:N", legend=None, scale=alt.Scale(scheme="set2")),
            tooltip=[
                alt.Tooltip("group:N", title="Type"),
                alt.Tooltip("value:Q", format=",d", title="Value"),
            ],
        )
        pie = base.mark_arc(innerRadius=45)
        label_base = alt.Chart(type_frame).encode(theta=alt.Theta("value:Q", stack=True))
        text_lbl = label_base.mark_text(
            radius=80,
            align="center",
            baseline="middle",
            fill="#000000",
            lineBreak="\n",
            fontSize=13,
        ).encode(text=alt.Text("label:N"))
        st.altair_chart((pie + text_lbl).properties(width=240, height=240))


def render_asset_table(
    rows: list[dict],
    currency: str,
    live_present: bool,
    date_config: dict,
    excluded_labels: list[str],
    asset_keys: tuple[str, ...],
) -> None:
    table = pd.DataFrame(rows)
    style = table.style.apply(grey_out, axis=1).format(show, na_rep="")
    if live_present:
        style = style.apply(
            lambda row: [TOTAL_TODAY if row.name == 0 else LIVE] * len(row),
            axis=1,
            subset=[TODAY_COL],
        )
    st.data_editor(
        style,
        hide_index=True,
        height=580,
        width="stretch",
        key="assets_editor",
        on_change=apply_toggles,
        args=(asset_keys,),
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
                args=(asset_keys,),
                key="inspect_btn",
            ),
            **date_config,
        },
    )
    if excluded_labels:
        st.info(
            "Not in the totals: "
            + ", ".join(excluded_labels)
            + ". Tick 'In total' on the row to count it again."
        )


def render_workings_panel(
    title: str, rows: list[dict], ticker: str | None, source: str, target: str
):
    with st.container(border=True):
        st.markdown(f"**How {title} was valued (in {target})**")
        price_column = f"{ticker} price" if ticker else "Price"
        numeric_columns = [
            "Units",
            price_column,
            f"Total in {source}",
            f"{source}→{target} rate",
            f"Final value in {target}",
            "Shown in table",
        ]
        st.dataframe(
            pd.DataFrame(rows),
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


def render_table_explanation() -> None:
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
            + ", ".join(f"{emoji} {asset_type}" for asset_type, emoji in sorted(TYPE_EMOJI.items()))
            + ". The flag comes from the currency, so it is an approximation."
        )


def render_live_status(
    live, marks, live_notes, marks_error: str, *, latest_date: str, today: str
) -> None:
    if marks_error:
        st.warning(
            f"Today's column is unavailable ({marks_error}). The snapshots below are unaffected."
        )
    if live is None:
        return

    marked_dates = sorted(
        {
            note.detail.rsplit(" on ", 1)[1]
            for note in live_notes
            if note.kind == market.MARKED and " on " in note.detail
        }
    )
    n_marked = sum(1 for note in live_notes if note.kind == market.MARKED)
    n_face = sum(1 for note in live_notes if note.kind == market.FACE)
    n_blank = sum(1 for note in live_notes if note.kind == market.UNRESOLVED)

    def held(count: int, noun: str = "holding") -> str:
        return f"{count} {noun}{'' if count == 1 else 's'}"

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
        else f"no rates published for {today} (weekend or holiday), so it uses {marks.fx_date}"
    )
    st.caption(
        f"**{TODAY_COL}** is not a snapshot and is never written to the file. It holds "
        f"{latest_date}'s holdings -- the same quantities, unchanged -- but re-marked at "
        f"today's closes ({', '.join(marked_dates) or 'none'}) and converted at {fx_note}. "
        f"Of the {held(len(live_notes))}: " + "; ".join(breakdown) + "."
    )
    if marks.failures:
        st.warning(
            "No close today for "
            + ", ".join(f"`{ticker}` ({why})" for ticker, why in marks.failures.items())
            + ". Those cells and the total are blank rather than valued at the last "
            "snapshot's price, which would mix today's rates with an older close."
        )


def render_snapshot_type_chart(rows: list[dict], snapshot_dates: list[str], currency: str, alt):
    if not rows or alt is None:
        return
    chart = (
        alt.Chart(pd.DataFrame(rows))
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
    st.altair_chart(chart, use_container_width=True)


def render_currency_totals(
    rows: list[dict], currencies: list[str], live_present: bool, has_exclusions: bool
):
    st.subheader("Total in each currency")
    style = pd.DataFrame(rows).style.format(show, na_rep="")
    style = style.apply(
        lambda row: [LIVE] * len(row) if live_present and row.name == 0 else [""] * len(row),
        axis=1,
    )
    st.dataframe(
        style,
        hide_index=True,
        width="stretch",
        column_config={
            "Date": st.column_config.TextColumn("Date"),
            **{
                code: st.column_config.NumberColumn(
                    code, format="localized", help=f"Total on that date, expressed in {code}"
                )
                for code in currencies
            },
        },
    )
    st.caption(
        "Liabilities (negative amounts) subtract, so a total is a net position. "
        "The columns will not agree with each other: they are separate conversions "
        "from the same date, and the rate matrix is only accurate to about 0.1%."
        + (
            " Assets ticked out of the table above are left out of these totals too."
            if has_exclusions
            else ""
        )
    )


def render_sidebar(data_dir: Path) -> tuple[Portfolio, str, str]:
    st.sidebar.title("Portfolio")
    handles = list_portfolios(data_dir)
    if not handles:
        st.error(f"No portfolio files in `{data_dir}`.")
        st.stop()

    handle = st.sidebar.selectbox("File", handles)
    st.sidebar.caption(f"Reading from `{data_dir}`")
    try:
        portfolio = load(handle, directory=data_dir)
    except (PortfolioError, SnapshotError) as exc:
        st.error(f"`{handle}.yaml` did not load: {exc}")
        st.stop()

    currency = st.sidebar.radio("Show amounts in", portfolio.currencies)
    return portfolio, handle, currency


def excluded_assets(portfolio: Portfolio, handle: str) -> set[str]:
    """Keep exclusions and row selections scoped to the selected portfolio."""
    if st.session_state.get("excluded_for", handle) != handle:
        st.session_state["excluded"] = set()
        st.session_state.pop("assets_editor", None)
        st.session_state.pop("inspect_key", None)
    st.session_state["excluded_for"] = handle
    excluded: set[str] = st.session_state.setdefault("excluded", set())
    excluded.intersection_update({spec.key for spec in portfolio.specs})
    return excluded


@st.cache_data(ttl=900, show_spinner=False)
def today_marks(currencies: tuple, tickers: tuple, on: str):
    """Cache live marks for 15 minutes, keyed by currencies, tickers and day."""
    try:
        return market.fetch_marks(list(currencies), list(tickers), on), ""
    except (FxError, market.PriceError) as exc:
        return None, str(exc)


def totals_for(
    portfolio: Portfolio,
    snapshots: list[Snapshot],
    target: str,
    included: set[str],
    live: Snapshot | None,
) -> dict[str, float | None]:
    """Total per snapshot in ``target``, blank where the model refuses to sum."""
    out: dict[str, float | None] = {}
    for snap in snapshots:
        try:
            out[snap.date] = portfolio.total_in(snap, target, only=included)
        except SnapshotError as exc:
            out[snap.date] = None
            st.warning(f"{snap.date}: {exc}")
    if live is not None:
        try:
            out[TODAY_COL] = portfolio.total_in(live, target, only=included)
        except SnapshotError:
            out[TODAY_COL] = None
    return out


def date_columns(
    snapshots: list[Snapshot],
    target: str,
    *,
    latest_date: str,
    today: str,
    live_present: bool,
) -> dict:
    out: dict = {}
    if live_present:
        out[TODAY_COL] = st.column_config.NumberColumn(
            TODAY_COL,
            format="localized",
            help=(
                f"Not a saved snapshot: {latest_date}'s holdings, re-marked and "
                f"re-converted for {today} and converted to {target}."
            ),
        )
    for snap in snapshots:
        out[snap.date] = st.column_config.NumberColumn(
            snap.date,
            format="localized",
            help=f"Value on {snap.date}, converted to {target}",
        )
    return out


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


def total_heading(currency: str, excluded: set[str]) -> str:
    """``Total (AUD)``, naming how much has been ticked out when something has been."""
    if not excluded:
        return f"Total ({currency})"
    count = len(excluded)
    return f"Total excluding {count} asset{'' if count == 1 else 's'} ({currency})"


def apply_toggles(asset_keys: tuple[str, ...]) -> None:
    """Fold edited row toggles into the excluded keys in session state.

    Streamlit runs a widget's callback before the script body, so ``excluded``
    already holds the click that triggered this run by the time rows are built.

    The table reports edits by row position with the total first; ``asset_keys``
    maps those positions to stable asset identities.
    """
    state = st.session_state.get("assets_editor")
    if not state:
        return
    excluded_keys: set[str] = st.session_state.setdefault("excluded", set())
    for row, change in state.get("edited_rows", {}).items():
        tick = change.get("In total")
        if tick is None or not 0 < row <= len(asset_keys):
            continue  # row 0 is the total, which is not an asset
        key = asset_keys[row - 1]
        if tick:
            excluded_keys.discard(key)
        else:
            excluded_keys.add(key)


def open_workings(asset_keys: tuple[str, ...]) -> None:
    """Toggle the clicked asset's stable key as the open workings panel.

    ButtonColumn reports a row number, so the current key sequence is passed in
    as callback args rather than consulting the module's ordered catalogue.
    """
    click = st.session_state.get("inspect_btn")
    if not click:
        return
    row = click["row"]
    if not 0 < row <= len(asset_keys):
        return
    asset_key = asset_keys[row - 1]
    st.session_state["inspect_key"] = (
        None if st.session_state.get("inspect_key") == asset_key else asset_key
    )


def main() -> None:
    st.set_page_config(page_title="Portfolio", layout="wide")
    data_dir = Path(os.environ.get("PORTFOLIO_DIR") or PORTFOLIO_DIR)
    today = dt.date.today().isoformat()
    portfolio, handle, currency = render_sidebar(data_dir)
    render_portfolio_title(portfolio.label or handle)
    current_net_worth_slot = st.empty()

    if not portfolio.snapshots:
        st.subheader(f"Assets over time ({currency})")
        render_snapshot_entry(portfolio, handle, today)
        st.info("Save your first snapshot above to start tracking this portfolio.")
        return

    snapshots = sorted(portfolio.snapshots, key=lambda snapshot: snapshot.date, reverse=True)
    latest = snapshots[0]
    ordered = sorted(portfolio.specs, key=lambda spec: (spec.currency, spec.type))
    excluded = excluded_assets(portfolio, handle)
    included = {spec.key for spec in ordered} - excluded
    marks, marks_error = today_marks(
        tuple(portfolio.currencies),
        tuple(market.tickers_to_mark(portfolio, latest)),
        today,
    )
    live = None
    live_notes: list[market.HoldingNote] = []
    if marks is not None:
        live, live_notes = market.virtual_snapshot(portfolio, latest, marks)

    pie_frames = build_pie_frames(portfolio, latest, ordered, included, currency)
    try:
        import altair as alt
    except ImportError:
        alt = None
    render_pie_charts(pie_frames, alt)

    selected = totals_for(portfolio, snapshots, currency, included, live)
    current_net_worth = selected.get(TODAY_COL)
    current_net_worth_slot.metric(
        "Estimated Current Net Worth",
        f"{currency} {whole(current_net_worth):,}"
        if current_net_worth is not None
        else "Unavailable",
    )
    rows = build_asset_rows(
        portfolio,
        ordered,
        snapshots,
        currency,
        live,
        selected,
        included,
        total_heading(currency, excluded),
        row_label,
        TODAY_COL,
        ":material/info:",
    )
    st.subheader(f"Assets over time ({currency})")
    render_snapshot_entry(portfolio, handle, today)
    render_asset_table(
        rows,
        currency,
        live is not None,
        date_columns(
            snapshots, currency, latest_date=latest.date, today=today, live_present=live is not None
        ),
        [row_label(spec) for spec in ordered if spec.key in excluded],
        tuple(spec.key for spec in ordered),
    )

    inspect_key = st.session_state.get("inspect_key")
    inspected = next((spec for spec in ordered if spec.key == inspect_key), None)
    if inspected is not None:
        workings_rows = build_workings_rows(
            portfolio,
            inspected,
            snapshots,
            currency,
            live,
            live_notes,
            marks.fx_date if marks is not None else None,
            TODAY_COL,
        )
        render_workings_panel(
            row_label(inspected), workings_rows, inspected.ticker, inspected.currency, currency
        )

    render_table_explanation()
    render_live_status(live, marks, live_notes, marks_error, latest_date=latest.date, today=today)

    type_snapshot_rows = build_snapshot_type_rows(portfolio, snapshots, ordered, included, currency)
    snapshot_dates = sorted(snapshot.date for snapshot in portfolio.snapshots)
    render_snapshot_type_chart(type_snapshot_rows, snapshot_dates, currency, alt)

    per_currency = {
        code: totals_for(portfolio, snapshots, code, included, live)
        for code in portfolio.currencies
    }
    currency_rows = build_currency_rows(
        portfolio.currencies, per_currency, snapshots, live, today, TODAY_COL
    )
    render_currency_totals(currency_rows, portfolio.currencies, live is not None, bool(excluded))


if __name__ == "__main__":
    main()
