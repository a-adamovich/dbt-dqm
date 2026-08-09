import json

from dbt_dqm_app.display import (
    calendar_date,
    column_value_pairs,
    format_timestamp,
    owner_editor_options,
    owner_label,
    owner_value_from_selection,
    paired_display,
    record_fields_html,
    workflow_status_options,
)
from dbt_dqm_app.store import Patch, Workspace
from dbt_dqm_app.warehouse import _batch_id, _validate_issue_rows


def issue(notes=None):
    return {
        "occurrence_id": "occ-1",
        "record_status": "Active",
        "test_status": "NEW",
        "call_to_action": None,
        "ticket_url": None,
        "notes": notes,
        "poc_responsible": None,
    }


def test_stores_only_differences(tmp_path):
    workspace = Workspace(tmp_path / "workspace.sqlite")
    workspace.replace_snapshot([issue()])

    workspace.set_change("occ-1", "notes", "Investigating")
    assert len(workspace.pending()) == 1
    assert workspace.rows()[0]["notes"] == "Investigating"

    workspace.set_change("occ-1", "notes", None)
    assert workspace.pending() == []


def test_clears_only_frozen_patch_version(tmp_path):
    workspace = Workspace(tmp_path / "workspace.sqlite")
    workspace.replace_snapshot([issue()])
    workspace.set_change("occ-1", "notes", "first")
    frozen = workspace.pending()
    workspace.set_change("occ-1", "notes", "second")

    workspace.clear_applied(frozen)

    assert workspace.pending()[0].new_value == "second"


def test_sync_preserves_pending_overlay(tmp_path):
    workspace = Workspace(tmp_path / "workspace.sqlite")
    workspace.replace_snapshot([issue()])
    workspace.set_change("occ-1", "notes", "local")
    workspace.replace_snapshot([issue("remote")])

    assert workspace.rows()[0]["notes"] == "local"


def test_frozen_patch_batch_id_is_stable_and_order_independent():
    first = Patch("occ-1", "notes", None, "review", 1, "2026-08-08T00:00:00+00:00")
    second = Patch("occ-2", "test_status", "NEW", "ACK", 2, "2026-08-08T00:01:00+00:00")

    assert _batch_id([first, second]) == _batch_id([second, first])


def test_structured_pair_display_preserves_mapping_and_special_values():
    pairs = json.dumps(
        {
            "customer_id": "A · B",
            "empty_value": "",
            "missing_value": None,
            "multiline": "first\nsecond",
        }
    )

    assert paired_display(pairs) == (
        'customer_id = "A · B"\nempty_value = ""\nmissing_value = NULL\n'
        'multiline = "first\\nsecond"'
    )


def test_record_fields_table_has_one_safe_row_per_pair():
    pairs = column_value_pairs(
        json.dumps(
            {"customer<id>": "A&B", "missing": None}
        )
    )
    rendered = record_fields_html(pairs)

    assert rendered.count('class="dqm-field-row"') == 3  # header plus two fields
    assert "customer&lt;id&gt;" in rendered
    assert "A&amp;B" in rendered
    assert "NULL" in rendered


def test_record_fields_table_explains_an_incompatible_empty_payload():
    rendered = record_fields_html([])
    assert "synchronize or rebuild dbt-dqm" in rendered


def test_json_object_pairs_are_supported_for_portable_storage():
    assert column_value_pairs('{"customer_id":"C-1","missing":null}') == [
        ("customer_id", '"C-1"'),
        ("missing", "NULL"),
    ]


def test_native_json_object_pairs_are_supported_for_future_adapters():
    assert column_value_pairs({"customer_id": "C-1", "missing": None}) == [
        ("customer_id", '"C-1"'),
        ("missing", "NULL"),
    ]


def test_warehouse_rows_reject_retired_array_payloads():
    row = issue()
    row.update(
        {
            "test_name": "example_test",
            "record_values_json": '[{"column_name":"customer_id","value":"C-1"}]',
        }
    )
    try:
        _validate_issue_rows([row])
    except ValueError as error:
        assert "incompatible record JSON" in str(error)
    else:
        raise AssertionError("retired array payload was accepted")


def test_warehouse_rows_accept_json_objects_and_optional_null_attributes():
    row = issue()
    row.update(
        {
            "test_name": "example_test",
            "record_values_json": '{"customer_id":"C-1","reason":"invalid"}',
        }
    )
    _validate_issue_rows([row])


def test_status_options_retain_a_legacy_value():
    assert workflow_status_options("custom_status")[-1] == "CUSTOM_STATUS"


def test_owner_label_groups_blank_values_as_unassigned():
    assert owner_label(None) == "Unassigned"
    assert owner_label("  ") == "Unassigned"
    assert owner_label("  data-team  ") == "data-team"


def test_owner_editor_uses_existing_values_and_allows_a_new_value():
    options = owner_editor_options([None, "Finance", "customer", "finance", " "])
    assert options == ["— Unassigned —", "customer", "Finance", "finance"]
    assert owner_value_from_selection("New owner") == "New owner"
    assert owner_value_from_selection("— Unassigned —") is None


def test_timestamp_format_handles_empty_and_iso_values():
    assert format_timestamp(None, "Never") == "Never"
    assert format_timestamp("2026-08-09T02:25:20+00:00") != "2026-08-09T02:25:20+00:00"


def test_calendar_date_handles_iso_values_and_missing_values():
    assert calendar_date("2026-08-09T02:25:20").isoformat() == "2026-08-09"
    assert calendar_date(None) is None
    assert calendar_date("not-a-date") is None
