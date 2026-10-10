"""The acceptance-dataset sweep deletes only this run's or stale labelled acceptance datasets."""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration_tests/acceptance"))

from bigquery_datasets import CREATED_LABEL, RUN_LABEL, labels, sweep

NOW = 1_800_000_000


class FakeClient:
    def __init__(self, datasets):
        self.datasets, self.deleted = datasets, []

    def list_datasets(self, project, filter):
        assert filter == f"labels.{RUN_LABEL}"
        return [SimpleNamespace(dataset_id=name, labels=value) for name, value in self.datasets]

    def delete_dataset(self, name, delete_contents, not_found_ok):
        assert delete_contents and not_found_ok
        self.deleted.append(name)


def test_sweep_removes_own_run_and_stale_labelled_datasets_only():
    client = FakeClient([
        ("dqm_acceptance_mine", labels("run-a", NOW)),
        ("dqm_acceptance_other_fresh", labels("run-b", NOW - 3600)),
        ("dqm_acceptance_other_stale", labels("run-b", NOW - 7 * 3600)),
        ("dqm_acceptance_unlabelled", {}),
        ("dbt_dqm_qa", labels("run-a", NOW - 99 * 3600)),  # shared datasets never match
        ("dqm_acceptance_bad_time", {RUN_LABEL: "run-c", CREATED_LABEL: "not-a-time"}),
    ])
    deleted = sweep(client, "proj", run_id="run-a", older_than_hours=6, now=NOW)
    assert deleted == ["dqm_acceptance_mine", "dqm_acceptance_other_stale"]
    assert client.deleted == [
        "proj.dqm_acceptance_mine",
        "proj.dqm_acceptance_mine_dbt_test__audit",
        "proj.dqm_acceptance_other_stale",
        "proj.dqm_acceptance_other_stale_dbt_test__audit",
    ]


def test_sweep_without_thresholds_deletes_only_the_given_run():
    client = FakeClient([
        ("dqm_acceptance_old", labels("run-x", NOW - 99 * 3600)),
        ("dqm_acceptance_mine", labels("run-a", NOW)),
    ])
    assert sweep(client, "proj", run_id="run-a", now=NOW) == ["dqm_acceptance_mine"]


def test_labels_are_valid_bigquery_label_values():
    import re

    for key, value in labels("0123456789abcdef0123456789abcdef", NOW).items():
        assert re.fullmatch(r"[a-z0-9_-]{1,63}", key) and re.fullmatch(r"[a-z0-9_-]{0,63}", value)
