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
    def __init__(self, identity=PROVISIONER, existing=()):
        self.identity, self.created, self.deleted = identity, {}, []
        self.existing = set(existing)  # names whose create_dataset call fails

    def query(self, sql):
        return SimpleNamespace(result=lambda: [SimpleNamespace(identity=self.identity)])

    def create_dataset(self, value):
        if value.dataset_id in self.existing or "*" in self.existing:
            raise AssertionError(f"create failed for {value.dataset_id}")
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
    assert client.deleted == [audit, dataset]  # the twin first, so it stays findable by its parent


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
    # Proof run 42957053520 failed here: the demo walkthrough assertion found 3 mismatches on the
    # fresh dataset. It is excluded exactly as the runner's own package test excludes it.
    build = calls[1][0]
    assert build[build.index("--select") + 1] == "package:dbt_dqm"
    assert build[build.index("--exclude") + 1] == "assert_scenario_outcomes"
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


def test_restricted_environments_never_inherit_provisioner_credentials(gate, monkeypatch):
    monkeypatch.setenv("DBT_DQM_BIGQUERY_PROVISIONER_CREDENTIALS", "/federated/provisioner.json")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/federated/provisioner.json")
    environment = gate._identity_environment("/federated/runner.json")
    assert environment["GOOGLE_APPLICATION_CREDENTIALS"] == "/federated/runner.json"
    assert "DBT_DQM_BIGQUERY_PROVISIONER_CREDENTIALS" not in environment
    assert "/federated/provisioner.json" not in environment.values()


def test_only_provisioning_code_passes_provisioner_credentials():
    import ast

    tree = ast.parse((ROOT / "integration_tests/acceptance/test_bigquery_permissions.py").read_text())
    users = set()
    for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
            if any(isinstance(node, ast.Name) and node.id == "PROVISIONER_FILE"
                   for argument in arguments for node in ast.walk(argument)):
                users.add(function.name)
    assert users == {"_install", "_provisioned_dataset"}
    body = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "test_restricted_bigquery_runner_and_reviewer")
    removed = {node.args[0].value for node in ast.walk(body)
               if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "delenv"}
    assert removed == {"DBT_DQM_BIGQUERY_PROVISIONER_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS"}


def test_a_failed_create_never_deletes_a_dataset_this_context_did_not_create(gate, monkeypatch, tmp_path):
    class Twin(Provisioner):
        def create_dataset(self, value):
            if value.dataset_id.endswith("_dbt_test__audit"):
                raise AssertionError("Already Exists")  # e.g. a pre-existing dataset of that name
            return super().create_dataset(value)

    client = Twin()
    monkeypatch.setattr(gate, "_client", lambda path: client)
    monkeypatch.setattr(gate, "_install", lambda dataset, directory: pytest.fail("must not install"))
    with pytest.raises(AssertionError, match="Already Exists"), gate._provisioned_dataset(tmp_path, "run-id"):
        pass
    assert client.deleted == list(client.created)  # only the parent it created; never the twin


def test_a_failed_first_create_deletes_nothing(gate, monkeypatch, tmp_path):
    client = Provisioner(existing={"*"})
    monkeypatch.setattr(gate, "_client", lambda path: client)
    with pytest.raises(AssertionError, match="create failed"), gate._provisioned_dataset(tmp_path, "run-id"):
        pass
    assert client.deleted == []
