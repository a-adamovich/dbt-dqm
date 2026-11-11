"""The acceptance-dataset sweep deletes only this run's or stale labelled acceptance datasets."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import Forbidden, NotFound
from google.cloud import bigquery

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration_tests/acceptance"))

from bigquery_datasets import (
    CREATED_LABEL,
    RUN_LABEL,
    OwnershipError,
    explicit_owners,
    labels,
    require_owned,
    sweep,
)

NOW = 1_800_000_000
CI = "dbt-dqm-ci@example-project.iam.gserviceaccount.com"
FOREIGN = "someone@example.invalid"


def dataset(name, owners=(CI,), location="US", created=NOW - 99 * 3600, extra=()):
    """BigQuery's dataset resource, shaped like `bq show --format=prettyjson` (access entries)."""
    access = [{"role": "WRITER", "specialGroup": "projectWriters"},
              {"role": "OWNER", "specialGroup": "projectOwners"},
              *({"role": "OWNER", "userByEmail": owner} for owner in owners),
              {"role": "READER", "specialGroup": "projectReaders"}, *extra]
    return bigquery.Dataset.from_api_repr({
        "datasetReference": {"projectId": "proj", "datasetId": name}, "location": location,
        "creationTime": str(int(created * 1000)), "access": access})


class FakeClient:
    def __init__(self, datasets, metadata=None):
        self.datasets, self.deleted = datasets, []
        names = [name for name, _ in datasets]
        self.metadata = {name: dataset(name) for name in names}
        self.metadata.update({name + "_dbt_test__audit": dataset(name + "_dbt_test__audit") for name in names})
        self.metadata.update(metadata or {})
        self.unlabelled = []

    def list_datasets(self, project, filter=None):
        if filter is None:  # The orphan scan lists every dataset, labelled or not.
            names = [name for name in self.metadata if name not in self.deleted_names()]
            return [SimpleNamespace(dataset_id=name, labels={}) for name in names]
        assert filter == f"labels.{RUN_LABEL}"
        return [SimpleNamespace(dataset_id=name, labels=value) for name, value in self.datasets]

    def deleted_names(self):
        return {name.split(".", 1)[1] for name in self.deleted}

    def get_dataset(self, name):
        value = self.metadata.get(name.split(".", 1)[1])
        if value is None or name.split(".", 1)[1] in self.deleted_names():
            raise NotFound(name)
        if isinstance(value, Exception):
            raise value
        return value

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
    deleted = sweep(client, "proj", CI, run_id="run-a", older_than_hours=6, now=NOW)
    assert deleted == ["dqm_acceptance_mine", "dqm_acceptance_other_stale"]
    # Each audit twin goes before its parent, so a failed twin deletion stays discoverable.
    assert client.deleted == [
        "proj.dqm_acceptance_mine_dbt_test__audit",
        "proj.dqm_acceptance_mine",
        "proj.dqm_acceptance_other_stale_dbt_test__audit",
        "proj.dqm_acceptance_other_stale",
    ]


def test_sweep_without_thresholds_deletes_only_the_given_run():
    client = FakeClient([
        ("dqm_acceptance_old", labels("run-x", NOW - 99 * 3600)),
        ("dqm_acceptance_mine", labels("run-a", NOW)),
    ])
    assert sweep(client, "proj", CI, run_id="run-a", now=NOW) == ["dqm_acceptance_mine"]


def test_labels_are_valid_bigquery_label_values():
    import re

    for key, value in labels("0123456789abcdef0123456789abcdef", NOW).items():
        assert re.fullmatch(r"[a-z0-9_-]{1,63}", key) and re.fullmatch(r"[a-z0-9_-]{0,63}", value)


@pytest.mark.parametrize("parent, twin", [
    (dataset("dqm_acceptance_mine", owners=(FOREIGN,)), None),
    (dataset("dqm_acceptance_mine", owners=(CI, FOREIGN)), None),
    (dataset("dqm_acceptance_mine", owners=()), None),
    (Forbidden("no metadata access"), None),
    (None, dataset("dqm_acceptance_mine_dbt_test__audit", owners=(FOREIGN,))),
    (None, Forbidden("no metadata access")),
])
def test_foreign_or_unverifiable_datasets_are_kept_with_their_parent(parent, twin):
    metadata = {}
    if parent is not None:
        metadata["dqm_acceptance_mine"] = parent
    if twin is not None:
        metadata["dqm_acceptance_mine_dbt_test__audit"] = twin
    client = FakeClient([("dqm_acceptance_mine", labels("run-a", NOW))], metadata)
    result = sweep(client, "proj", CI, run_id="run-a", now=NOW)
    assert result == [] and len(result.skipped) == 1
    # A kept twin keeps its parent too, so the twin remains discoverable by its label.
    assert "proj.dqm_acceptance_mine" not in client.deleted
    assert FOREIGN not in str(result.skipped)  # reports never name other principals


def test_a_missing_twin_does_not_block_its_parent():
    client = FakeClient([("dqm_acceptance_mine", labels("run-a", NOW))])
    del client.metadata["dqm_acceptance_mine_dbt_test__audit"]
    assert sweep(client, "proj", CI, run_id="run-a", now=NOW) == ["dqm_acceptance_mine"]
    assert client.deleted == ["proj.dqm_acceptance_mine"]


def orphan_client(twin):
    client = FakeClient([], {"dqm_acceptance_gone_dbt_test__audit": twin})
    return client


def test_stale_sweep_removes_an_owned_orphan_twin_whose_parent_is_gone():
    client = orphan_client(dataset("dqm_acceptance_gone_dbt_test__audit", created=NOW - 7 * 3600))
    result = sweep(client, "proj", CI, older_than_hours=6, now=NOW)
    assert result == ["dqm_acceptance_gone_dbt_test__audit"]
    assert client.deleted == ["proj.dqm_acceptance_gone_dbt_test__audit"]


@pytest.mark.parametrize("twin", [
    dataset("dqm_acceptance_gone_dbt_test__audit", owners=(FOREIGN,), created=NOW - 7 * 3600),
    dataset("dqm_acceptance_gone_dbt_test__audit", created=NOW - 3600),  # may belong to a live run
    Forbidden("no metadata access"),
])
def test_foreign_fresh_or_unverifiable_orphan_twins_are_kept(twin):
    client = orphan_client(twin)
    result = sweep(client, "proj", CI, older_than_hours=6, now=NOW)
    assert result == [] and client.deleted == [] and len(result.skipped) == 1


def test_a_run_sweep_cannot_attribute_orphans_and_leaves_them():
    client = orphan_client(dataset("dqm_acceptance_gone_dbt_test__audit", created=NOW - 99 * 3600))
    assert sweep(client, "proj", CI, run_id="run-a", now=NOW) == [] and client.deleted == []


def test_orphan_scan_ignores_twins_with_a_parent_and_other_names():
    client = FakeClient([], {
        "dqm_acceptance_kept": dataset("dqm_acceptance_kept"),  # unlabelled parent, not swept
        "dqm_acceptance_kept_dbt_test__audit": dataset("dqm_acceptance_kept_dbt_test__audit"),
        "dbt_dqm_qa_dbt_test__audit": dataset("dbt_dqm_qa_dbt_test__audit"),
    })
    assert sweep(client, "proj", CI, older_than_hours=6, now=NOW) == [] and client.deleted == []


def test_owners_follow_bigquery_access_entries():
    value = dataset("d", owners=(CI.upper(),), extra=[
        {"role": "OWNER", "iamMember": "serviceAccount:" + CI},
        {"role": "WRITER", "userByEmail": FOREIGN}])
    assert explicit_owners(value) == {CI}  # projectOwners and non-owner roles do not count
    require_owned(value, CI, "US")
    for extra in ({"role": "OWNER", "groupByEmail": "team@example.invalid"},
                  {"role": "OWNER", "domain": "example.invalid"},
                  {"role": "OWNER", "specialGroup": "allAuthenticatedUsers"}):
        with pytest.raises(OwnershipError):
            require_owned(dataset("d", extra=[extra]), CI)
    with pytest.raises(OwnershipError, match="not US"):
        require_owned(dataset("d", location="EU"), CI, "US")


def test_demo_fixture_deletes_its_twin_first_and_only_if_newer(monkeypatch):
    import test_bigquery
    from test_bigquery import _delete_fixture_datasets

    monkeypatch.setattr(test_bigquery, "PROJECT", "proj")

    own = dataset("dqm_acceptance_x", created=NOW)
    for twin_created, expected in ((NOW + 60, ["proj.dqm_acceptance_x_dbt_test__audit", "proj.dqm_acceptance_x"]),
                                   (NOW - 60, ["proj.dqm_acceptance_x"])):
        client = FakeClient([], {"dqm_acceptance_x": own,
                                 "dqm_acceptance_x_dbt_test__audit": dataset("dqm_acceptance_x_dbt_test__audit",
                                                                             created=twin_created)})
        _delete_fixture_datasets(client, own, "dqm_acceptance_x")
        assert client.deleted == expected
    client = FakeClient([], {"dqm_acceptance_x": own})
    _delete_fixture_datasets(client, own, "dqm_acceptance_x")
    assert client.deleted == ["proj.dqm_acceptance_x"]
