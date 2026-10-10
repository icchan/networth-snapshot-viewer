"""Checks for the Streamlit page's pure view-data builders."""

import pytest
from helpers import check

from portfolio_view import build_pie_frames, build_snapshot_type_rows, whole
from snapshots import load


@pytest.fixture
def personal():
    return load("personal")


def test_whole_rounds_half_away_from_zero():
    check("positive halves round up", whole(2.5) == 3)
    check("negative halves round away from zero", whole(-2.5) == -3)
    check("None stays blank", whole(None) is None)


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
