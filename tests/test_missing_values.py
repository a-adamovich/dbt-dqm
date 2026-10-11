"""Missing values from pandas rows render as missing, under pandas 2 (None) and 3 (NaN)."""

import pandas as pd

from dbt_dqm_app.display import (
    calendar_date,
    format_timestamp,
    is_missing,
    owner_label,
    owner_value_from_selection,
    workflow_status_options,
)


def _missing_row():
    rows = [
        {
            "annotation_updated_at": "2026-10-10T12:00:00+00:00",
            "poc_responsible": "x",
            "first_seen_at": "2026-10-09T00:00:00+00:00",
            "workflow_status": "NEW",
        },
        {
            "annotation_updated_at": None,
            "poc_responsible": None,
            "first_seen_at": None,
            "workflow_status": None,
        },
    ]
    return next(
        row for _, row in pd.DataFrame(rows).iterrows() if is_missing(row["poc_responsible"])
    )


def test_card_helpers_treat_dataframe_missing_values_as_missing():
    row = _missing_row()
    assert format_timestamp(row.get("annotation_updated_at"), "Never") == "Never"
    assert format_timestamp(row.get("first_seen_at")) == "—"
    assert calendar_date(row.get("first_seen_at")) is None
    assert owner_label(row.get("poc_responsible")) == "Unassigned"
    assert owner_value_from_selection(row.get("poc_responsible")) is None
    assert workflow_status_options(row.get("workflow_status"))[-1] == "FALSE_POSITIVE"


def test_present_values_are_not_missing():
    assert not any(is_missing(value) for value in ("", "x", 0, 0.0, "2026-01-01"))
    assert all(is_missing(value) for value in (None, float("nan"), pd.NaT, pd.NA))
