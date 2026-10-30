"""Require successful credentialed verification of this exact commit on both engines."""

import json
import subprocess


def gh(*arguments):
    result = subprocess.run(["gh", *arguments], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def main():
    import os

    commit = os.environ["GITHUB_SHA"]
    repository = os.environ["GITHUB_REPOSITORY"]
    runs = gh("run", "list", "--repo", repository, "--workflow", "bigquery-scheduled.yml",
              "--commit", commit, "--status", "success", "--limit", "100",
              "--json", "databaseId,headSha")
    required = {"lifecycle (dbt1)", "lifecycle (dbt2)"}
    for run in runs:
        if run["headSha"] != commit:
            continue
        jobs = gh("api", f"repos/{repository}/actions/runs/{run['databaseId']}/jobs?per_page=100")
        passed = {job["name"] for job in jobs["jobs"] if job["conclusion"] == "success"}
        if required <= passed:
            print(f"Credentialed BigQuery gate passed for {commit}, run {run['databaseId']}")
            return
    raise SystemExit("Release blocked: run the credentialed BigQuery lifecycle workflow on this "
                     "exact commit and require both dbt1 and dbt2 jobs to pass. Skipped jobs "
                     "and offline smoke tests do not satisfy this gate.")


if __name__ == "__main__":
    main()
