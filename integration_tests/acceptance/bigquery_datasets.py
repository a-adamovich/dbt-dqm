"""Labels and cleanup for the disposable datasets the BigQuery acceptance tests create.

Every test's fixture deletes its own dataset. A worker that is killed (or a stopped run) can't, so
each dataset carries the run that created it and its creation time, and `sweep` removes:
- every dataset of a given run, once that run has finished; and
- labelled acceptance datasets older than a threshold, left by earlier runs.

Only datasets named `dqm_acceptance_*` that carry the run label are ever deleted, together with
their `_dbt_test__audit` twin. Shared datasets (QA, demo) never match.

    uv run python integration_tests/acceptance/bigquery_datasets.py --project PROJECT [--older-than-hours 6]
"""

from __future__ import annotations

import argparse
import time

PREFIX = "dqm_acceptance_"
RUN_LABEL = "dbt_dqm_run"
CREATED_LABEL = "dbt_dqm_created"


def labels(run_id: str, now: float | None = None) -> dict[str, str]:
    return {RUN_LABEL: run_id, CREATED_LABEL: str(int(now if now is not None else time.time()))}


def _stale(dataset_labels: dict[str, str], run_id: str | None, cutoff: float | None) -> bool:
    if RUN_LABEL not in dataset_labels:
        return False
    if run_id is not None and dataset_labels[RUN_LABEL] == run_id:
        return True
    created = dataset_labels.get(CREATED_LABEL, "")
    return cutoff is not None and created.isdigit() and int(created) < cutoff


def sweep(client, project: str, run_id: str | None = None, older_than_hours: float | None = None,
          now: float | None = None) -> list[str]:
    """Delete this run's leftovers and labelled acceptance datasets older than the threshold."""
    cutoff = None
    if older_than_hours is not None:
        cutoff = (now if now is not None else time.time()) - older_than_hours * 3600
    deleted = []
    for item in client.list_datasets(project=project, filter=f"labels.{RUN_LABEL}"):
        name = item.dataset_id
        if not name.startswith(PREFIX) or name.endswith("_dbt_test__audit"):
            continue
        if not _stale(dict(item.labels or {}), run_id, cutoff):
            continue
        for target in (name, name + "_dbt_test__audit"):
            client.delete_dataset(f"{project}.{target}", delete_contents=True, not_found_ok=True)
        deleted.append(name)
    return deleted


def main() -> None:
    from google.cloud import bigquery

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--older-than-hours", type=float, default=6)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    deleted = sweep(bigquery.Client(project=args.project), args.project, args.run_id,
                    args.older_than_hours)
    print(f"Deleted {len(deleted)} acceptance dataset(s): {', '.join(deleted) or 'none'}")


if __name__ == "__main__":
    main()
