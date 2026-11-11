"""Labels and cleanup for the disposable datasets the BigQuery acceptance tests create.

Every test's fixture deletes its own dataset. A worker that is killed (or a stopped run) can't, so
each dataset carries the run that created it and its creation time, and `sweep` removes:
- every dataset of a given run, once that run has finished; and
- labelled acceptance datasets older than a threshold, left by earlier runs.

Only datasets named `dqm_acceptance_*` that carry the run label are ever deleted, together with
their `_dbt_test__audit` twin, and only when the expected identity explicitly owns each one. The twin
goes first, so its labelled parent keeps it discoverable. A stale sweep also removes an owned twin
whose parent is already gone. Shared datasets (QA, demo) never match, and a dataset that is foreign or
cannot be verified is skipped and reported, never deleted.

    uv run python integration_tests/acceptance/bigquery_datasets.py --project PROJECT --owner EMAIL [--older-than-hours 6]
"""

from __future__ import annotations

import argparse
import time

PREFIX = "dqm_acceptance_"
AUDIT_SUFFIX = "_dbt_test__audit"
RUN_LABEL = "dbt_dqm_run"
CREATED_LABEL = "dbt_dqm_created"
OWNER_ROLES = {"OWNER", "ROLES/BIGQUERY.DATAOWNER"}


class OwnershipError(Exception):
    """A dataset is not verifiably and exclusively owned by the expected identity."""


def explicit_owners(dataset) -> set[str]:
    """Explicit owners from BigQuery's dataset access entries.

    The creator appears as {"role": "OWNER", "userByEmail": ...}; the project owners group
    ({"specialGroup": "projectOwners"}) owns every dataset and does not count. Any other owner
    entry, including groups, domains and other special groups, is kept so that it fails the check.
    """
    owners = set()
    for entry in dataset.access_entries or ():
        if str(entry.role or "").upper() not in OWNER_ROLES:
            continue
        kind, value = entry.entity_type, str(entry.entity_id or "")
        if kind == "specialGroup" and value == "projectOwners":
            continue
        if kind == "iamMember" and value.split(":", 1)[0] in {"serviceAccount", "user"}:
            kind, value = "userByEmail", value.split(":", 1)[1]
        owners.add(value.lower() if kind == "userByEmail" else f"{kind}:{value}")
    return owners


def require_owned(dataset, owner: str, location: str | None = None) -> None:
    """Raise unless `owner` is the only explicit owner (and the location matches, when given).

    Messages never repeat other principals, because CI logs may be public.
    """
    owners = explicit_owners(dataset)
    if owners != {owner.lower()}:
        present = "includes" if owner.lower() in owners else "does not include"
        raise OwnershipError(f"{dataset.dataset_id} has {len(owners)} explicit owner(s), which "
                             f"{present} the expected identity; it must be the only one")
    if location and str(dataset.location or "").upper() != location.upper():
        raise OwnershipError(f"{dataset.dataset_id} is in {dataset.location}, not {location}")


def labels(run_id: str, now: float | None = None) -> dict[str, str]:
    return {RUN_LABEL: run_id, CREATED_LABEL: str(int(now if now is not None else time.time()))}


def _stale(dataset_labels: dict[str, str], run_id: str | None, cutoff: float | None) -> bool:
    if RUN_LABEL not in dataset_labels:
        return False
    if run_id is not None and dataset_labels[RUN_LABEL] == run_id:
        return True
    created = dataset_labels.get(CREATED_LABEL, "")
    return cutoff is not None and created.isdigit() and int(created) < cutoff


class SweepResult(list):
    """Deleted datasets, in order; `skipped` holds (dataset, reason) pairs that were left alone."""

    def __init__(self):
        super().__init__()
        self.skipped: list[tuple[str, str]] = []


def _delete_owned(client, project, names, owner, result, cutoff=None) -> bool:
    """Delete `names` in order, each only after verifying ownership; stop at the first doubt."""
    from google.api_core.exceptions import NotFound

    for name in names:
        try:
            dataset = client.get_dataset(f"{project}.{name}")
        except NotFound:
            continue
        except Exception as error:  # noqa: BLE001 - unverifiable means keep, never delete
            result.skipped.append((name, f"metadata unreadable ({type(error).__name__})"))
            return False
        try:
            require_owned(dataset, owner)
        except OwnershipError as error:
            result.skipped.append((name, str(error)))
            return False
        created = getattr(dataset, "created", None)
        if cutoff is not None and (created is None or created.timestamp() >= cutoff):
            result.skipped.append((name, "not older than the threshold"))
            return False
        client.delete_dataset(f"{project}.{name}", delete_contents=True, not_found_ok=True)
    return True


def sweep(client, project: str, owner: str, run_id: str | None = None,
          older_than_hours: float | None = None, now: float | None = None) -> SweepResult:
    """Delete this run's leftovers and labelled acceptance datasets older than the threshold.

    Each dataset and audit twin is deleted only if `owner` explicitly owns it. A stale sweep also
    deletes an owned twin whose parent is already gone; a run sweep cannot attribute such an orphan
    to a run, so it leaves orphans alone.
    """
    cutoff = None
    if older_than_hours is not None:
        cutoff = (now if now is not None else time.time()) - older_than_hours * 3600
    result = SweepResult()
    for item in client.list_datasets(project=project, filter=f"labels.{RUN_LABEL}"):
        name = item.dataset_id
        if not name.startswith(PREFIX) or name.endswith(AUDIT_SUFFIX):
            continue
        if not _stale(dict(item.labels or {}), run_id, cutoff):
            continue
        # The twin first: while the labelled parent exists, a failed twin deletion stays discoverable.
        if _delete_owned(client, project, [name + AUDIT_SUFFIX, name], owner, result):
            result.append(name)
    if cutoff is not None:
        names = {item.dataset_id for item in client.list_datasets(project=project)}
        for name in sorted(names):
            if (name.startswith(PREFIX) and name.endswith(AUDIT_SUFFIX)
                    and name[:-len(AUDIT_SUFFIX)] not in names
                    and _delete_owned(client, project, [name], owner, result, cutoff=cutoff)):
                result.append(name)
    return result


def main() -> None:
    from google.cloud import bigquery

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--owner", required=True, help="the identity that must own each deleted dataset")
    parser.add_argument("--older-than-hours", type=float, default=6)
    parser.add_argument("--run-id")
    args = parser.parse_args()
    deleted = sweep(bigquery.Client(project=args.project), args.project, args.owner, args.run_id,
                    args.older_than_hours)
    print(f"Deleted {len(deleted)} acceptance dataset(s): {', '.join(deleted) or 'none'}")
    for name, reason in deleted.skipped:
        print(f"Skipped {name}: {reason}")


if __name__ == "__main__":
    main()
