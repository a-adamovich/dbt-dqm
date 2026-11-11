"""Create, or verify before reuse, the dataset the BigQuery lifecycle workflow resets each run.

`reset_demo_dqm` drops the demo tables in this dataset, so the workflow may only touch a dataset the
CI identity verifiably and exclusively owns, in the expected location. Creating it here makes the CI
identity its owner, which is also how the rollback guide recognises CI's datasets. This script never
drops or modifies a dataset; any doubt stops the job before the reset.

    uv run python integration_tests/acceptance/bigquery_dedicated_dataset.py \\
        --project PROJECT --dataset NAME --owner EMAIL [--location US]
"""

from __future__ import annotations

import argparse

from bigquery_datasets import AUDIT_SUFFIX, OwnershipError, require_owned


def _session_user(client) -> str:
    return next(iter(client.query("select session_user() as identity").result())).identity


def ensure_dedicated(client, project: str, dataset_id: str, owner: str, location: str = "US") -> str:
    """Return "created" or "reused"; raise SystemExit when ownership or location can't be verified."""
    from google.api_core.exceptions import Conflict, NotFound
    from google.cloud import bigquery

    try:
        identity = _session_user(client)
    except Exception as error:  # an unverifiable identity must stop the job
        raise SystemExit(f"Could not confirm the active identity ({type(error).__name__}).") from error
    if str(identity).lower() != owner.lower():
        raise SystemExit("The active credentials are not the expected CI identity; refusing to continue.")
    wanted = bigquery.Dataset(f"{project}.{dataset_id}")
    wanted.location = location
    try:
        client.create_dataset(wanted)
        outcome = "created"
    except Conflict:
        outcome = "reused"
    except Exception as error:
        raise SystemExit(f"Could not create {dataset_id} ({type(error).__name__}).") from error
    for name, required in ((dataset_id, True), (dataset_id + AUDIT_SUFFIX, False)):
        try:
            dataset = client.get_dataset(f"{project}.{name}")
        except NotFound as error:
            if required:
                raise SystemExit(f"{name} is missing after creation; cannot verify its owner.") from error
            continue  # dbt creates the audit twin as the CI identity when it first needs it
        except Exception as error:
            raise SystemExit(f"Could not read {name} to verify its owner ({type(error).__name__}).") from error
        try:
            require_owned(dataset, owner, location)
        except OwnershipError as error:
            raise SystemExit(f"Refusing to reset: {error}.") from error
    return outcome


def main() -> None:
    from google.cloud import bigquery

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--location", default="US")
    args = parser.parse_args()
    client = bigquery.Client(project=args.project, location=args.location)
    outcome = ensure_dedicated(client, args.project, args.dataset, args.owner, args.location)
    print(f"{args.dataset} {outcome}: owned only by the CI identity, in {args.location}.")


if __name__ == "__main__":
    main()
