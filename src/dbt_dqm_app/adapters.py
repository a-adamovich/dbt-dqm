"""Optional warehouse dependencies, loaded only when their attributes are used."""

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version

from .errors import AppError


def missing_adapter(adapter: str) -> AppError:
    return AppError(
        f"The {adapter} warehouse client is not installed. Install it in this Python environment with "
        f"`python -m pip install 'dbt-dqm[{adapter}]'`."
    )


def require_adapter(adapter: str) -> None:
    packages = {"bigquery": ("google-cloud-bigquery",),
                "postgres": ("psycopg2-binary", "psycopg2")}[adapter]
    for package in packages:
        try:
            version(package)
        except PackageNotFoundError:
            continue
        return
    raise missing_adapter(adapter) from None


class LazyModule:
    def __init__(self, name: str, adapter: str):
        self._name = name
        self._adapter = adapter

    def __getattr__(self, name: str):
        try:
            module = import_module(self._name)
        except ModuleNotFoundError as error:
            if error.name and (self._name == error.name or self._name.startswith(error.name + ".")):
                raise missing_adapter(self._adapter) from None
            raise
        return getattr(module, name)
