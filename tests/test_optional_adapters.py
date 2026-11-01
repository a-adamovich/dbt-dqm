"""Optional imports must not obscure failures in installed warehouse libraries."""

import sys
from importlib.metadata import PackageNotFoundError
from types import ModuleType

import pytest

from dbt_dqm_app import adapters, errors


def test_lazy_module_loads_only_when_used(monkeypatch):
    calls = []
    module = ModuleType("driver")
    module.Client = object()
    monkeypatch.setattr(adapters, "import_module", lambda name: calls.append(name) or module)
    lazy = adapters.LazyModule("driver", "bigquery")
    assert not calls
    assert lazy.Client is module.Client
    assert calls == ["driver"]


def test_postgres_client_can_be_installed_from_source(monkeypatch):
    def package_version(name):
        if name == "psycopg2":
            return "2.9.10"
        raise PackageNotFoundError(name)

    monkeypatch.setattr(adapters, "version", package_version)
    adapters.require_adapter("postgres")


@pytest.mark.parametrize("missing", ["google", "google.cloud", "google.cloud.bigquery"])
def test_missing_driver_has_install_hint(monkeypatch, missing):
    def load(name):
        raise ModuleNotFoundError(name=missing)

    monkeypatch.setattr(adapters, "import_module", load)
    with pytest.raises(errors.AppError, match=r"dbt-dqm\[bigquery\]"):
        _ = adapters.LazyModule("google.cloud.bigquery", "bigquery").Client


def test_broken_dependency_is_not_reported_as_missing_adapter(monkeypatch):
    error = ModuleNotFoundError(name="broken_transitive_dependency")

    def load(name):
        raise error

    monkeypatch.setattr(adapters, "import_module", load)
    with pytest.raises(ModuleNotFoundError) as raised:
        _ = adapters.LazyModule("google.cloud.bigquery", "bigquery").Client
    assert raised.value is error


def test_generic_error_classification_does_not_import_drivers(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg2", None)
    monkeypatch.setitem(sys.modules, "google.api_core.exceptions", None)
    assert errors.classify(ValueError("test")).message.endswith("test")
