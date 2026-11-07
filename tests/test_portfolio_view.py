"""Checks for the Streamlit page's pure view-data builders."""

import pytest
from helpers import check

from portfolio_view import (
    build_asset_rows,
    build_pie_frames,
    build_snapshot_editor_rows,
    build_snapshot_trend_rows,
    build_snapshot_type_rows,
    holdings_from_editor,
    previous_month_snapshot,
    snapshot_month_options,
    whole,
)
from snapshots import load


@pytest.fixture
def personal():
    return load("personal")


@pytest.mark.parametrize(
    "totals",
    [{}, {"2026-01-01": 100.0}, {"2026-01-01": None, "2026-02-01": 100.0}],
)
def test_snapshot_trend_requires_two_complete_totals(totals):
    check(
        "no regression with fewer than two complete snapshots",
        build_snapshot_trend_rows(totals) == [],
    )


def test_snapshot_trend_uses_elapsed_days_and_skips_incomplete_totals():
    rows = build_snapshot_trend_rows(
        {"2026-02-10": 180.0, "2026-01-05": None, "2026-01-01": 100.0, "2026-01-11": 120.0}
    )
    check(
        "trend points are chronological",
        [row["date"] for row in rows] == ["2026-01-01", "2026-01-11", "2026-02-10"],
    )
    check(
        "uneven dates follow the fitted daily growth",
        [row["trend"] for row in rows] == pytest.approx([100, 120, 180]),
    )


def test_snapshot_trend_is_a_best_fit_not_a_line_joining_totals():
    rows = build_snapshot_trend_rows({"2026-01-01": -10.0, "2026-01-02": 10.0, "2026-01-03": 0.0})
    check(
        "least-squares fit includes negative net worth",
        [row["trend"] for row in rows] == pytest.approx([-5, 0, 5]),
    )


@pytest.mark.parametrize("live_present", [False, True])
@pytest.mark.parametrize("missing_latest", [False, True])
def test_asset_change_row_compares_previous_totals(personal, live_present, missing_latest):
    snapshots = sorted(personal.snapshots, key=lambda snapshot: snapshot.date, reverse=True)[:3]
    totals = dict(
        zip([snapshot.date for snapshot in snapshots], [200.4, 150.1, 170.8], strict=True)
    )
    totals["Today"] = 225.5
    if missing_latest:
        totals[snapshots[0].date] = None
    rows = build_asset_rows(
        personal,
        [],
        snapshots,
        "AUD",
        snapshots[0] if live_present else None,
        totals,
        set(),
        "Total (AUD)",
        lambda spec: spec.label,
        "Today",
        "How",
    )
    change, total = rows
    check(
        "change row appears above the total",
        change["Asset"] == "Change (AUD)" and total["Asset"] == "Total (AUD)",
    )
    check(
        "summary rows have no checkbox or workings",
        all(row["In total"] is None and row["How"] == "" for row in rows),
    )
    check("oldest snapshot has no prior comparison", change[snapshots[-1].date] is None)
    check("declines remain negative", change[snapshots[1].date] == -21)
    check(
        "changes use unrounded totals",
        change[snapshots[0].date] == (None if missing_latest else 50),
    )
    if live_present:
        check(
            "Today compares with the latest saved total",
            change["Today"] == (None if missing_latest else 25),
        )
    else:
        check("no live change without a live column", "Today" not in change)


def test_whole_rounds_half_away_from_zero():
    check("positive halves round up", whole(2.5) == 3)
    check("negative halves round away from zero", whole(-2.5) == -3)
    check("None stays blank", whole(None) is None)


def test_snapshot_month_options_cover_history_through_today(personal):
    months = snapshot_month_options(personal.snapshots, "2026-10-10")
    check("months are newest first", months == sorted(months, reverse=True), str(months))
    check(
        "saved, current, and next month are available",
        {"2026-05", "2026-10", "2026-11"} <= set(months),
    )
    check("months beyond next month are not offered", "2026-12" not in months)


def test_snapshot_month_options_roll_over_at_year_end():
    months = snapshot_month_options([], "2026-12-31")
    check("next month rolls into the next year", months == ["2027-01", "2026-12"])


def test_previous_month_snapshot_selects_latest_from_exact_previous_month(personal):
    previous = previous_month_snapshot(personal.snapshots, "2026-10")
    check(
        "snapshot from immediately preceding month is selected",
        previous is not None and previous.date[:7] == "2026-09",
    )
    check(
        "no snapshot is borrowed across a gap",
        previous_month_snapshot(personal.snapshots, "1900-02") is None,
    )


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

    template_rows = build_snapshot_editor_rows(
        personal.specs, None, lambda spec: spec.label, partial
    )
    template_by_key = {row["key"]: row for row in template_rows}
    template_stock = template_by_key["msft-ian"]
    check(
        "new snapshot copies held and entry from template",
        template_stock["Held"] and template_stock["Entry"] == "Units & price",
    )
    check(
        "new snapshot does not copy previous amounts or price overrides",
        template_stock["Value"] is None
        and template_stock["Units"] is None
        and template_stock["Price override"] is None,
    )
    check(
        "new snapshot keeps previous-month absent assets unchecked",
        not template_by_key[absent_spec.key]["Held"],
    )


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


@pytest.mark.parametrize("currency", ["AUD", "JPY", "USD"])
@pytest.mark.parametrize("exclude_first", [False, True])
def test_snapshot_type_rows_include_a_distinct_live_estimate(personal, currency, exclude_first):
    latest = max(personal.snapshots, key=lambda snapshot: snapshot.date)
    included = {spec.key for spec in personal.specs}
    if exclude_first:
        included.remove(personal.specs[0].key)
    rows = build_snapshot_type_rows(
        personal, personal.snapshots, personal.specs, included, currency, live=latest
    )
    live_rows = [row for row in rows if row["status"] == "Live estimate"]
    check("live estimate appears after recorded history", rows[-len(live_rows) :] == live_rows)
    check(
        "live label differs even on a recorded date",
        {row["date"] for row in live_rows} == {"Today (live)"},
    )
    check(
        "live tooltip retains its actual date", {row["as_of"] for row in live_rows} == {latest.date}
    )
    check(
        "live total uses selected currency and exclusions",
        sum(row["value"] for row in live_rows)
        == pytest.approx(personal.total_in(latest, currency, only=included)),
    )


@pytest.mark.parametrize("currency", ["AUD", "JPY", "USD"])
@pytest.mark.parametrize("exclude_first", [False, True])
def test_snapshot_type_rows_are_chronological_and_aggregated(personal, currency, exclude_first):
    specs = sorted(personal.specs, key=lambda spec: (spec.currency, spec.type))
    included = {spec.key for spec in specs}
    if exclude_first:
        included.remove(specs[0].key)
    rows = build_snapshot_type_rows(personal, personal.snapshots, specs, included, currency)
    dates = [row["date"] for row in rows]
    check("snapshot data is chronological", dates == sorted(dates), str(dates))

    expected_groups = {
        (snapshot.date, spec.currency, spec.type)
        for snapshot in personal.snapshots
        for spec in specs
        if spec.key in included and personal.value_in(snapshot, spec.key, currency) is not None
    }
    actual_groups = [(row["date"], row["currency"], row["type"]) for row in rows]
    check(
        "each date/currency/type group appears once", len(actual_groups) == len(set(actual_groups))
    )
    check("all included groups are represented", set(actual_groups) == expected_groups)

    for row in rows:
        snapshot = personal.by_date(row["date"])
        expected = sum(
            value
            for spec in specs
            if spec.key in included
            and spec.type == row["type"]
            and spec.currency == row["currency"]
            if (value := personal.value_in(snapshot, spec.key, currency)) is not None
        )
        check(
            f"{row['date']} {row['group']} is aggregated from model values",
            row["value"] == expected,
            f"{row['value']} vs {expected}",
        )
        type_label = "Stock" if row["type"] == "stocks" else row["type"].title()
        check("group labels match the pie chart", row["group"] == f"{row['currency']} {type_label}")
