"""Pure data builders for the Streamlit portfolio page."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

import pandas as pd

import market
from snapshots import AssetSpec, Portfolio, Snapshot


def whole(amount: float | None) -> int | None:
    """Round half away from zero for whole-unit table display."""
    if amount is None:
        return None
    magnitude = math.floor(abs(amount) + 0.5)
    return magnitude if amount >= 0 else -magnitude


def build_pie_frames(
    portfolio: Portfolio,
    snapshot: Snapshot,
    specs: Sequence[AssetSpec],
    included: set[str],
    currency: str,
) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """Build latest-snapshot totals by currency and currency/type for the pies."""
    currency_groups: dict[str, int] = {}
    type_groups: dict[str, int] = {}
    for spec in specs:
        if spec.key not in included:
            continue
        value = portfolio.value_in(snapshot, spec.key, currency)
        if value is None or abs(value) == 0:
            continue
        display_value = whole(value) or 0
        currency_groups[spec.currency] = currency_groups.get(spec.currency, 0) + display_value
        type_label = "Stock" if spec.type == "stocks" else spec.type.title()
        group = f"{spec.currency} {type_label}"
        type_groups[group] = type_groups.get(group, 0) + display_value

    if not currency_groups:
        return None

    def make_frame(groups: dict[str, int]) -> pd.DataFrame:
        frame = pd.DataFrame([{"group": group, "value": value} for group, value in groups.items()])
        total = frame["value"].sum()
        frame["pct"] = frame["value"] / total * 100 if total > 0 else 0
        frame["pct_lbl"] = frame["pct"].map(lambda value: f"{value:.1f}%")
        frame["label"] = frame["group"].astype(str) + "\n" + frame["pct_lbl"]
        return frame

    return make_frame(currency_groups), make_frame(type_groups)


def build_asset_rows(
    portfolio: Portfolio,
    specs: Sequence[AssetSpec],
    snapshots: Sequence[Snapshot],
    target: str,
    live: Snapshot | None,
    totals: dict[str, float | None],
    included: set[str],
    total_label: str,
    label_for: Callable[[AssetSpec], str],
    today_column: str,
    how_label: str,
) -> list[dict]:
    """Build the asset editor rows in display order, without rendering them."""
    total_row: dict = {"In total": None, "Asset": total_label, "How": ""}
    if live is not None:
        total_row[today_column] = whole(totals.get(today_column))
    for snapshot in snapshots:
        total_row[snapshot.date] = whole(totals[snapshot.date])

    rows = [total_row]
    for spec in specs:
        row = {
            "In total": spec.key in included,
            "Asset": label_for(spec),
            "How": how_label,
        }
        if live is not None:
            row[today_column] = whole(portfolio.value_in(live, spec.key, target))
        for snapshot in snapshots:
            row[snapshot.date] = whole(portfolio.value_in(snapshot, spec.key, target))
        rows.append(row)
    return rows


def build_workings_rows(
    portfolio: Portfolio,
    spec: AssetSpec,
    snapshots: Sequence[Snapshot],
    target: str,
    live: Snapshot | None,
    live_notes: Sequence[market.HoldingNote],
    fx_date: str | None,
    today_column: str,
) -> list[dict]:
    """Build one calculation row per snapshot, with Today first when available."""
    columns: list[tuple[str, Snapshot]] = [] if live is None else [(today_column, live)]
    columns += [(snapshot.date, snapshot) for snapshot in snapshots]
    notes = {note.ref: note for note in live_notes}
    rows = []
    source = spec.currency
    price_column = f"{spec.ticker} price" if spec.ticker else "Price"
    total_column = f"Total in {source}"
    rate_column = f"{source}→{target} rate"
    final_column = f"Final value in {target}"

    for when, snapshot in columns:
        holding = next((asset for asset in snapshot.assets if asset.ref == spec.key), None)
        live_view = snapshot is live
        note = notes.get(spec.key) if live_view else None
        row = {
            "Snapshot": when,
            "Units": None,
            price_column: None,
            total_column: None,
            rate_column: None,
            final_column: None,
            "Shown in table": None,
            "Note": "",
        }
        if holding is None:
            row["Note"] = "Not held"
            rows.append(row)
            continue

        row[rate_column] = snapshot.fx[source][target]
        amount = holding.resolved_value
        if holding.value is not None:
            row[total_column] = holding.value
            row["Note"] = (
                "Flat amount, carried at face value" if live_view else "Flat amount in file"
            )
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
            rows.append(row)
            continue

        result = portfolio.value_in(snapshot, spec.key, target)
        row[final_column] = result
        row["Shown in table"] = whole(result)
        if live_view:
            live_details = []
            if note is not None and " on " in note.detail:
                live_details.append(f"{spec.ticker}'s close on {note.detail.rsplit(' on ', 1)[1]}")
            if source != target and fx_date is not None:
                live_details.append(f"rates as at {fx_date}")
            if live_details:
                row["Note"] = "; ".join(filter(None, [row["Note"], *live_details]))
        rows.append(row)

    return rows


def build_snapshot_type_rows(
    portfolio: Portfolio,
    snapshots: Sequence[Snapshot],
    specs: Sequence[AssetSpec],
    included: set[str],
    currency: str,
) -> list[dict]:
    """Build chronological snapshot totals grouped by asset type."""
    rows = []
    for snapshot in sorted(snapshots, key=lambda item: item.date):
        values_by_type: dict[str, float] = {}
        for spec in specs:
            if spec.key not in included:
                continue
            value = portfolio.value_in(snapshot, spec.key, currency)
            if value is not None:
                values_by_type[spec.type] = values_by_type.get(spec.type, 0) + value
        rows.extend(
            {"date": snapshot.date, "type": asset_type, "value": value}
            for asset_type, value in values_by_type.items()
        )
    return rows


def build_currency_rows(
    currencies: Sequence[str],
    totals_by_currency: dict[str, dict[str, float | None]],
    snapshots: Sequence[Snapshot],
    live: Snapshot | None,
    today: str,
    today_column: str,
) -> list[dict]:
    """Build the all-currency total table, with its live row first."""
    rows = (
        [
            {
                "Date": f"{today} (live)",
                **{code: whole(totals_by_currency[code].get(today_column)) for code in currencies},
            }
        ]
        if live is not None
        else []
    )
    rows.extend(
        {
            "Date": snapshot.date,
            **{code: whole(totals_by_currency[code][snapshot.date]) for code in currencies},
        }
        for snapshot in snapshots
    )
    return rows
