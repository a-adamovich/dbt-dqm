"""Verify the normal credentialed release evidence. Any missing or mismatched record blocks release."""

import os

import ci_validation
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
        {'lifecycle (dbt1)', 'lifecycle (dbt2)', 'cross-engine-upgrade', 'restricted-permissions'}, tree)
    for engine in ('dbt1', 'dbt2'):
        bigquery_report(repository, cloud, f'scheduled-bigquery-{engine}-results', tree, engine)
    bigquery_report(repository, cloud, 'cross-engine-bigquery-results', tree, 'dbt2', upgrade=True)
    bigquery_report(repository, cloud, 'restricted-bigquery-results', tree, 'dbt1', permissions=True)


if __name__ == "__main__":
    normal_gate()
