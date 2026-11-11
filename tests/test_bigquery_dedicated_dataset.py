"""The lifecycle workflow resets its dedicated dataset only when the CI identity verifiably owns it."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import Conflict, Forbidden, NotFound

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration_tests/acceptance"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bigquery_dedicated_dataset import ensure_dedicated
from test_acceptance_sweep import CI, FOREIGN, dataset

NAME, TWIN = "dbt_dqm_scheduled_dbt2", "dbt_dqm_scheduled_dbt2_dbt_test__audit"


class Client:
    """BigQuery's create/get semantics: create conflicts if the dataset exists."""

    def __init__(self, existing=None, identity=CI, created_owner=CI):
        self.existing, self.identity, self.created_owner = dict(existing or {}), identity, created_owner
        self.created, self.mutations = [], []

    def query(self, sql):
        assert sql == "select session_user() as identity"
        if isinstance(self.identity, Exception):
            raise self.identity
        return SimpleNamespace(result=lambda: [SimpleNamespace(identity=self.identity)])

    def create_dataset(self, value):
        name = value.dataset_id
        if name in self.existing:
            raise Conflict("Already Exists")
        assert value.location == "US"
        self.created.append(name)
        self.existing[name] = dataset(name, owners=(self.created_owner,))
        return self.existing[name]

    def get_dataset(self, reference):
        value = self.existing.get(reference.split(".", 1)[1])
        if value is None:
            raise NotFound(reference)
        if isinstance(value, Exception):
            raise value
        return value

    def __getattr__(self, name):  # delete_dataset, update_dataset, query on tables ...
        self.mutations.append(name)
        raise AssertionError(f"The guard must never call {name}")


def test_first_run_creates_the_dataset_owned_by_the_ci_identity():
    client = Client()
    assert ensure_dedicated(client, "proj", NAME, CI) == "created"
    assert client.created == [NAME] and not client.mutations


def test_a_correctly_owned_us_dataset_and_twin_are_reused():
    client = Client({NAME: dataset(NAME), TWIN: dataset(TWIN)})
    assert ensure_dedicated(client, "proj", NAME, CI.upper()) == "reused"
    assert client.created == [] and not client.mutations


@pytest.mark.parametrize("existing, message", [
    ({NAME: dataset(NAME, owners=(FOREIGN,))}, "does not include"),
    ({NAME: dataset(NAME, owners=(CI, FOREIGN))}, "must be the only one"),
    ({NAME: dataset(NAME, owners=())}, "0 explicit owner"),
    ({NAME: dataset(NAME, location="EU")}, "not US"),
    ({NAME: Forbidden("no access")}, "Could not read"),
    ({NAME: dataset(NAME), TWIN: dataset(TWIN, owners=(FOREIGN,))}, "does not include"),
    ({NAME: dataset(NAME), TWIN: dataset(TWIN, location="EU")}, "not US"),
    ({NAME: dataset(NAME), TWIN: Forbidden("no access")}, "Could not read"),
])
def test_an_unexpected_owner_or_location_stops_before_the_reset(existing, message):
    client = Client(existing)
    with pytest.raises(SystemExit, match=message) as stopped:
        ensure_dedicated(client, "proj", NAME, CI)
    assert FOREIGN not in str(stopped.value)  # public logs never name other principals
    assert client.created == [] and not client.mutations


@pytest.mark.parametrize("identity", [FOREIGN, Forbidden("cannot run jobs")])
def test_wrong_or_unconfirmed_credentials_stop_before_any_dataset_call(identity):
    client = Client(identity=identity)
    with pytest.raises(SystemExit):
        ensure_dedicated(client, "proj", NAME, CI)
    assert client.created == [] and not client.mutations


def test_a_created_dataset_must_still_verify():
    client = Client(created_owner=FOREIGN)  # e.g. an organisation policy that rewrites owners
    with pytest.raises(SystemExit, match="Refusing to reset"):
        ensure_dedicated(client, "proj", NAME, CI)
