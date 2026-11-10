"""The keyless permissions gate must keep provisioning separate from the restricted identities."""

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "integration_tests/acceptance"))
RUNNER, REVIEWER, PROVISIONER = "runner@example.invalid", "reviewer@example.invalid", "ci@example.invalid"


@pytest.fixture
def gate(monkeypatch):
    module = importlib.import_module("test_bigquery_permissions")
    monkeypatch.setattr(module, "RUNNER", RUNNER)
    monkeypatch.setattr(module, "REVIEWER", REVIEWER)
    monkeypatch.setattr(module, "PROVISIONER_FILE", "/federated/provisioner.json")
    return module


class Provisioner:
    def __init__(self, identity=PROVISIONER):
        self.identity, self.created, self.deleted = identity, {}, []

    def query(self, sql):
        return SimpleNamespace(result=lambda: [SimpleNamespace(identity=self.identity)])

    def create_dataset(self, value):
        self.created[value.dataset_id] = value
        return value

    def update_dataset(self, value, fields):
        assert fields == ["access_entries"]
        return value

    def delete_dataset(self, name, delete_contents, not_found_ok):
        assert delete_contents and not_found_ok
        self.deleted.append(name.split(".")[-1])


def entries(dataset):
    return {(entry.role, entry.entity_id) for entry in dataset.access_entries}


def test_only_the_provisioner_builds_the_disposable_datasets(gate, monkeypatch, tmp_path):
    client, installed = Provisioner(), []
    monkeypatch.setattr(gate, "_client", lambda path: client)
    monkeypatch.setattr(gate, "_install", lambda dataset, directory: installed.append(dataset))
    with gate._provisioned_dataset(tmp_path, "run-id") as dataset:
        assert dataset.startswith("dqm_acceptance_permissions_") and installed == [dataset]
        audit = dataset + "_dbt_test__audit"
        assert entries(client.created[dataset]) >= {("WRITER", RUNNER), ("READER", REVIEWER)}
        assert ("WRITER", REVIEWER) not in entries(client.created[dataset])
        assert entries(client.created[audit]) == {("WRITER", RUNNER)}
        assert client.created[dataset].labels["dbt_dqm_run"] == "run-id"
    assert client.deleted == [dataset, audit]


@pytest.mark.parametrize("identity", [RUNNER, REVIEWER])
def test_a_restricted_identity_cannot_act_as_the_provisioner(gate, monkeypatch, tmp_path, identity):
    client = Provisioner(identity)
    monkeypatch.setattr(gate, "_client", lambda path: client)
    with pytest.raises(AssertionError, match="separate"), gate._provisioned_dataset(tmp_path, "run-id"):
        pass
    assert not client.created


def test_failed_installation_still_deletes_both_datasets(gate, monkeypatch, tmp_path):
    client = Provisioner()
    monkeypatch.setattr(gate, "_client", lambda path: client)
    def fail(dataset, directory):
        raise AssertionError("install failed")
    monkeypatch.setattr(gate, "_install", fail)
    with pytest.raises(AssertionError, match="install failed"), gate._provisioned_dataset(tmp_path, "run-id"):
        pass
    assert len(client.deleted) == 2


def test_install_uses_provisioner_credentials_and_exact_reviewer_grants(gate, monkeypatch, tmp_path):
    calls = []
    def run(command, env, **kwargs):
        calls.append((command, env))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(gate.subprocess, "run", run)
    gate._install("disposable", tmp_path / "install")
    assert [command[1] for command, _ in calls] == ["run-operation", "build"]
    for command, env in calls:
        assert env["GOOGLE_APPLICATION_CREDENTIALS"] == "/federated/provisioner.json"
        grants = json.loads(command[command.index("--vars") + 1])["dbt_dqm_table_grants"]
        assert set(grants) == set(gate.REVIEWER_WRITE_TABLES)
        assert all(value == {"roles/bigquery.dataEditor": ["serviceAccount:" + REVIEWER]} for value in grants.values())
    assert "disposable" in (tmp_path / "install/profiles/profiles.yml").read_text()


def test_shared_qa_mode_does_not_provision(monkeypatch):
    module = importlib.import_module("test_bigquery_permissions")
    monkeypatch.setattr(module, "PROVISIONER_FILE", None)
    generator = module.restricted_dataset.__wrapped__(Path("/unused"), "run-id")
    assert next(generator) == "dbt_dqm_qa"
