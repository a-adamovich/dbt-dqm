"""Verify release evidence; local attestation is restricted to 0.2.5."""

import argparse
import os
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import ci_validation
import local_release_evidence as local
from release_evidence import (
    bigquery_report,
    commit_tree,
    gh,
    required_ci_jobs,
    verify_run,
    warehouse_report,
)


def successful_run(repository, workflow, sha, expected, tree):
    runs = gh('run', 'list', '--repo', repository, '--workflow', workflow,
              '--commit', sha, '--status', 'success', '--limit', '100',
              '--json', 'databaseId,headSha')
    for candidate in runs:
        if candidate['headSha'] == sha:
            return verify_run(repository, candidate['databaseId'], workflow=workflow,
                              expected_jobs=expected, tree=tree)
    raise ValueError('No successful exact-commit validation: ' + workflow)


def normal_gate():
    repository, sha = os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_SHA']
    tree = commit_tree(repository, sha)
    ci, _ = successful_run(repository, 'ci.yml', sha, required_ci_jobs(), tree)
    if ci['event'] != 'push':
        raise ValueError('Release requires merged-main CI')
    warehouse_report(repository, ci, 'warehouse-core-acceptance', tree, core=True)
    full, pages = successful_run(repository, 'full-validation.yml', sha,
                                ci_validation.full_jobs() | {'validated'}, tree)
    records = []
    for name, entry in ci_validation.warehouse_matrix().items():
        artifact = ('warehouse-postgres-lower-bound-acceptance' if name == 'dbt-lower-bound'
                    else f"warehouse-postgres-py{entry['python']}-pg{entry['postgres']}-acceptance")
        records.append(warehouse_report(repository, full, artifact, tree))
    ci_validation.verify_jobs(pages, {'verified': {'result': 'success'}}, records,
        sha=sha, tree=tree, run_id=str(full['id']), run_attempt=str(full['run_attempt']),
        repository=repository)
    cloud, _ = successful_run(repository, 'bigquery-lifecycle.yml', sha,
        {'lifecycle (dbt1)', 'lifecycle (dbt2)', 'restricted-permissions'}, tree)
    for engine in ('dbt1', 'dbt2'):
        bigquery_report(repository, cloud, f'scheduled-bigquery-{engine}-results', tree, engine)
    bigquery_report(repository, cloud, 'restricted-bigquery-results', tree, 'dbt1', permissions=True)


def local_gate(args):
    repository = os.environ["GITHUB_REPOSITORY"]
    if repository != local.REPOSITORY or args.version != local.VERSION:
        raise ValueError("The local attestation exception is restricted to dbt-dqm 0.2.5")
    if tomllib.loads((local.ROOT / "pyproject.toml").read_text())["project"]["version"] != args.version:
        raise ValueError("The package version does not match the attestation")
    actual = os.environ["GITHUB_SHA"]
    tree = commit_tree(repository, actual)
    if (local.command("git", "rev-parse", "HEAD") != actual
            or local.command("git", "rev-parse", "HEAD^{tree}") != tree
            or local.command("git", "status", "--porcelain", "--untracked-files=no")
            or commit_tree(repository, args.tested_sha) != tree):
        raise ValueError("The checkout and tested candidate must have identical trees")
    if not args.verify_only:
        if (os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
                or os.environ.get("GITHUB_REF") != "refs/tags/v0.2.5"
                or not args.attest):
            raise ValueError("Publication requires explicit manual attestation on v0.2.5")
        actor = os.environ["GITHUB_ACTOR"]
        permission = gh("api", f"repos/{repository}/collaborators/{actor}/permission")
        if permission.get("permission") != "admin":
            raise ValueError("Only a repository administrator may submit local release attestation")
        main = gh("api", f"repos/{repository}/commits/main")
        if main["commit"]["tree"]["sha"] != tree:
            raise ValueError("Main changed after validation")
    value = local.validate_bundle(args.evidence, args.digest, tree=tree,
                                 lock_digest=local.digest((local.ROOT / "uv.lock").read_bytes()))
    for sha in {run["sha"] for run in value["runs"]}:
        if commit_tree(repository, sha) != tree:
            raise ValueError("A local report names a different source tree")
    ci, _ = verify_run(repository, args.ci_run, workflow="ci.yml",
                       expected_jobs=required_ci_jobs(), tree=tree)
    if not args.verify_only and (ci["event"] != "push" or ci["head_sha"] != actual):
        raise ValueError("Publication requires current CI on the merged/tagged commit")
    warehouse_report(repository, ci, "warehouse-core-acceptance", tree, core=True)
    full, pages = verify_run(repository, args.full_run, workflow="full-validation.yml",
                             expected_jobs=ci_validation.full_jobs() | {"validated"}, tree=tree)
    if full["head_sha"] != args.tested_sha:
        raise ValueError("Full validation must identify the recorded tested SHA")
    records = []
    for name, entry in ci_validation.warehouse_matrix().items():
        artifact = ("warehouse-postgres-lower-bound-acceptance" if name == "dbt-lower-bound" else
                    f"warehouse-postgres-py{entry['python']}-pg{entry['postgres']}-acceptance")
        records.append(warehouse_report(repository, full, artifact, tree))
    ci_validation.verify_jobs(pages, {"verified_api_jobs": {"result": "success"}}, records,
        sha=full["head_sha"], tree=tree, run_id=str(full["id"]), run_attempt=str(full["run_attempt"]),
        repository=repository)
    print(f"Verified identical tree {tree}; local BigQuery results are maintainer-attested.")
    if args.verify_only:
        print("Verification only: no publishing, release mutation or approval requested.")


def main(argv=()):
    if not argv:
        return normal_gate()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--tested-sha", required=True)
    parser.add_argument("--ci-run", required=True)
    parser.add_argument("--full-run", required=True)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--attest", action="store_true")
    try:
        local_gate(parser.parse_args(argv))
    except (ValueError, KeyError, OSError, ET.ParseError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        parser.exit(1, f"Release blocked: {error}\n")


if __name__ == "__main__":
    main(sys.argv[1:])
