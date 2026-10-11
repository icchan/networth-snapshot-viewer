"""Checks for the Streamlit page's pure view-data builders."""

import pytest
from helpers import check

from portfolio_view import (
    build_pie_frames,
    build_snapshot_editor_rows,
    build_snapshot_type_rows,
    holdings_from_editor,
    snapshot_month_options,
    whole,
)
from snapshots import load


@pytest.fixture
def personal():
    return load("personal")


def test_whole_rounds_half_away_from_zero():
    check("positive halves round up", whole(2.5) == 3)
    check("negative halves round away from zero", whole(-2.5) == -3)
    check("None stays blank", whole(None) is None)


def test_snapshot_month_options_cover_history_through_today(personal):
    months = snapshot_month_options(personal.snapshots, "2026-10-10")
    check("months are newest first", months == sorted(months, reverse=True), str(months))
    check("saved months and the current month are available", {"2026-05", "2026-10"} <= set(months))


def test_snapshot_editor_rows_prefill_existing_holdings(personal):
    from dataclasses import replace

    latest = max(personal.snapshots, key=lambda snapshot: snapshot.date)
    absent_spec = personal.specs[-1]
    partial = replace(
        latest,
        assets=[asset for asset in latest.assets if asset.ref != absent_spec.key],
    )
    rows = build_snapshot_editor_rows(personal.specs, partial, lambda spec: spec.label)
    by_key = {row["key"]: row for row in rows}
    stock = by_key["msft-ian"]
    absent = by_key[absent_spec.key]
    check("held stocks default to units mode", stock["Held"] and stock["Entry"] == "Units & price")
    check(
        "stock units are prefilled and price defaults to fetch",
        stock["Units"] is not None and stock["Price override"] is None,
    )
    check("assets absent from the snapshot are unchecked", not absent["Held"])


def test_holdings_from_editor_builds_values_and_unit_rows():
    import pandas as pd

    editor = pd.DataFrame(
        [
            {
                "key": "cash",
                "Asset": "Cash",
                "Held": True,
                "Entry": "Value",
                "Value": 1250,
                "Units": None,
                "Price override": None,
            },
            {
                "key": "shares",
                "Asset": "Shares",
                "Held": True,
                "Entry": "Units & price",
                "Value": None,
                "Units": 2.5,
                "Price override": None,
            },
            {
                "key": "absent",
                "Asset": "Absent",
                "Held": False,
                "Entry": "Value",
                "Value": None,
                "Units": None,
                "Price override": None,
            },
        ]
    ).set_index("key")
    from snapshots import AssetSpec

    specs = [
        AssetSpec("cash", "cash", "AUD"),
        AssetSpec("shares", "stocks", "USD", ticker="MSFT"),
        AssetSpec("absent", "cash", "AUD"),
    ]
    holdings, errors = holdings_from_editor(editor, specs)
    check("valid inputs produce holdings without errors", not errors, str(errors))
    check(
        "value and fetched-price units serialize to model input",
        holdings == [{"ref": "cash", "value": 1250.0}, {"ref": "shares", "units": 2.5}],
        str(holdings),
    )


def test_holdings_from_editor_reports_incomplete_rows():
    import pandas as pd

    editor = pd.DataFrame(
        [
            {
                "key": "cash",
                "Asset": "Cash",
                "Held": True,
                "Entry": "Value",
                "Value": None,
                "Units": None,
                "Price override": None,
            },
            {
                "key": "other",
                "Asset": "Other",
                "Held": True,
                "Entry": "Units & price",
                "Value": None,
                "Units": 3,
                "Price override": None,
            },
        ]
    ).set_index("key")
    from snapshots import AssetSpec

    specs = [AssetSpec("cash", "cash", "AUD"), AssetSpec("other", "other", "JPY")]
    holdings, errors = holdings_from_editor(editor, specs)
    check("incomplete rows are not submitted", not holdings, str(holdings))
    check("both incomplete inputs are identified", len(errors) == 2, str(errors))


def test_pie_frames_group_latest_values(personal):
    specs = sorted(personal.specs, key=lambda spec: (spec.currency, spec.type))
    included = {spec.key for spec in specs}
    latest = max(personal.snapshots, key=lambda snapshot: snapshot.date)
    frames = build_pie_frames(personal, latest, specs, included, personal.currencies[0])
    check("latest snapshot has pie data", frames is not None)
    if frames is None:
        return

    currency_frame, type_frame = frames
    expected_currencies = set()
    expected_types = set()
    for spec in specs:
        value = personal.value_in(latest, spec.key, personal.currencies[0])
        if spec.key in included and value is not None and abs(value) > 0:
            expected_currencies.add(spec.currency)
            type_label = "Stock" if spec.type == "stocks" else spec.type.title()
            expected_types.add(f"{spec.currency} {type_label}")

    check(
        "currency pie groups by source currency",
        set(currency_frame["group"]) == expected_currencies,
        str(currency_frame["group"].tolist()),
    )
    check(
        "type pie groups by source currency and title-cased type",
        set(type_frame["group"]) == expected_types,
        str(type_frame["group"].tolist()),
    )
    check(
        "both pie frames include percentage and display labels",
        all({"pct", "pct_lbl", "label"} <= set(frame.columns) for frame in frames),
    )


def test_snapshot_type_rows_are_chronological_and_aggregated(personal):
    specs = sorted(personal.specs, key=lambda spec: (spec.currency, spec.type))
    included = {spec.key for spec in specs}
    currency = personal.currencies[0]
    rows = build_snapshot_type_rows(personal, personal.snapshots, specs, included, currency)
    dates = [row["date"] for row in rows]
    check("snapshot data is chronological", dates == sorted(dates), str(dates))

    for row in rows:
        snapshot = personal.by_date(row["date"])
        expected = sum(
            value
            for spec in specs
            if spec.key in included and spec.type == row["type"]
            if (value := personal.value_in(snapshot, spec.key, currency)) is not None
        )
        check(
            f"{row['date']} {row['type']} is aggregated from model values",
            row["value"] == expected,
            f"{row['value']} vs {expected}",
        )
