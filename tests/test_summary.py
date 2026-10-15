import pytest

from dbt_dqm_app.config import AppConfig
from dbt_dqm_app.errors import SnapshotUnavailable
from dbt_dqm_app.summary import owner_summary, print_summary


def test_owner_summary_has_total_unassigned_and_new_sort(tmp_path, monkeypatch, capsys):
    config = AppConfig(
        tmp_path, tmp_path, "dev", "demo", "postgres", "db", "schema", "US", "oauth", None
    )
    queries = []

    def read(config, query):
        queries.append(query)
        return (
            [
                {
                    "owner": "z",
                    "new_count": 3,
                    "active_count": 5,
                    "summary_run_status": "completed",
                },
                {
                    "owner": "a, b",
                    "new_count": 1,
                    "active_count": 8,
                    "summary_run_status": "completed",
                },
            ],
            ("install", 4),
        )

    monkeypatch.setattr("dbt_dqm_app.warehouse._read_with_state", read)
    result = owner_summary(config, "exact-run")
    print_summary(result)
    assert result["new"] == 4 and result["active"] == 13
    assert [row["owner"] for row in result["owners"]] == ["z", "a, b", "Unassigned"]
    assert len(queries) == 1 and queries[0].params == {"run_id": "exact-run"}
    assert "record_status='Active'" in queries[0].sql
    assert "='NEW'" in queries[0].sql
    assert capsys.readouterr().out.splitlines()[2].startswith("Total")


def test_empty_summary_and_unavailable_reconciliation(tmp_path, monkeypatch):
    config = AppConfig(
        tmp_path, tmp_path, "dev", "demo", "bigquery", "db", "s", "US", "oauth", None
    )
    monkeypatch.setattr(
        "dbt_dqm_app.warehouse._read_with_state",
        lambda *args: ([{"owner": None, "summary_run_status": "completed"}], ("id", 1)),
    )
    result = owner_summary(config, "run")
    assert result["active"] == result["new"] == 0 and result["owners"] == [
        {"owner": "Unassigned", "new": 0, "active": 0}
    ]
    monkeypatch.setattr(
        "dbt_dqm_app.warehouse._read_with_state",
        lambda *args: ([{"summary_run_status": "started"}], ("id", 1)),
    )
    with pytest.raises(SnapshotUnavailable):
        owner_summary(config, "run")
