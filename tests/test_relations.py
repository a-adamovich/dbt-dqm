from dataclasses import replace

import pytest

from dbt_dqm_app.config import AppConfig
from dbt_dqm_app.errors import AppError
from dbt_dqm_app.relations import bind, lock_key
from dbt_dqm_app.warehouse import _relation_parts


def config(path):
    return AppConfig(path, path, "dev", "demo", "postgres", "db", "schema", "US", "oauth", None)


def contract(prefix):
    return {
        name: {"database": "db", "schema": "shared", "identifier": prefix + name}
        for name in ("dqm_install", "dqm_issue_occurrences", "dqm_reconciliation_control")
    }


def test_namespaces_have_separate_storage_and_locks(tmp_path):
    first = bind(config(tmp_path), contract("alice_"))
    second = bind(config(tmp_path), contract("bob_"))
    assert first.workspace_path != second.workspace_path
    assert lock_key(first) != lock_key(second)
    assert _relation_parts(first, "dqm_issue_occurrences") == (
        "db",
        "shared",
        "alice_dqm_issue_occurrences",
    )
    assert (
        first.relations
        == bind(config(tmp_path), dict(reversed(list(contract("alice_").items())))).relations
    )
    with pytest.raises(AppError, match="absent"):
        _relation_parts(first, "dqm_unknown")


def test_unprefixed_installation_preserves_old_cache_and_lock(tmp_path, monkeypatch):
    monkeypatch.setattr("dbt_dqm_app.config.user_data_path", lambda *args: tmp_path / "app-data")
    original = replace(config(tmp_path), dataset="shared")
    resolved = bind(original, contract(""))
    original.workspace_path.parent.mkdir(parents=True, exist_ok=True)
    original.workspace_path.write_bytes(b"old pending edits")
    assert resolved.workspace_path != original.workspace_path
    assert original.workspace_path.read_bytes() == b"old pending edits"
    other = bind(
        original, {name: {**item, "schema": "another"} for name, item in contract("").items()}
    )
    assert resolved.workspace_path != other.workspace_path
    original.workspace_path.unlink()
    assert lock_key(resolved) == "dbt_dqm:shared"


def test_invalid_relation_contract_is_rejected(tmp_path):
    values = contract("a_")
    values["dqm_install"]["identifier"] = "a_dqm_issue_occurrences"
    with pytest.raises(AppError, match="duplicate"):
        bind(config(tmp_path), values)
    with pytest.raises(AppError, match="incomplete"):
        bind(config(tmp_path), {})


def test_naming_resolution_refuses_state_output_collision(tmp_path, monkeypatch):
    from dbt_dqm_app.relations import resolve_config

    config_value = replace(config(tmp_path), target_path=tmp_path / "target")
    monkeypatch.setenv("DBT_STATE", "target")
    monkeypatch.setattr(
        "dbt_dqm_app.relations._resolve", lambda *args: pytest.fail("must not parse")
    )
    with pytest.raises(AppError, match="comparison artifacts"):
        resolve_config(config_value)
    assert not config_value.target_path.exists()
