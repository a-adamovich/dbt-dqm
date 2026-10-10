"""Shared acceptance-test plumbing: one run id for every pytest-xdist worker, and cleanup.

The controller (or a plain, non-parallel run) creates the run id and hands it to each worker. At
the end of the session the controller deletes any BigQuery dataset this run left behind (for
example from a killed worker) and labelled acceptance datasets older than six hours.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from bigquery_datasets import sweep

STALE_HOURS = 6


def pytest_configure(config):
    worker = getattr(config, "workerinput", None)
    config.dqm_run_id = worker["dqm_run_id"] if worker else uuid.uuid4().hex


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    """xdist hook (controller side): share the controller's run id with each worker."""
    node.workerinput["dqm_run_id"] = node.config.dqm_run_id


@pytest.fixture(scope="session")
def acceptance_run_id(request):
    return request.config.dqm_run_id


def pytest_sessionfinish(session):
    if hasattr(session.config, "workerinput"):
        return  # workers leave cleanup to the controller, after every worker has finished
    # The xdist controller collects no tests itself, so the configured project is the signal.
    project = os.environ.get("DBT_DQM_BIGQUERY_TEST_PROJECT")
    if not project:
        return
    try:
        from google.cloud import bigquery

        location = os.environ.get("DBT_DQM_BIGQUERY_TEST_LOCATION", "US")
        deleted = sweep(
            bigquery.Client(project=project, location=location),
            project,
            run_id=session.config.dqm_run_id,
            older_than_hours=STALE_HOURS,
        )
    except Exception as error:  # noqa: BLE001 - cleanup is best effort and never fails a run
        print(f"\nAcceptance dataset cleanup skipped: {type(error).__name__}")
        return
    if deleted:
        print(f"\nRemoved {len(deleted)} leftover acceptance dataset(s): {', '.join(deleted)}")
