"""Fail-closed test-result and job-matrix checks for the credential-free CI tiers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

CORE_POSTGRES_TESTS = frozenset({
    'test_lifecycle_annotations_history_and_health',
    'test_run_changes_sorted_results_and_collection_limits',
    'test_dedicated_native_limits_complete_pass_and_stale_edits',
    'test_capture_rejects_failure_tables_changed_before_capture',
    'test_late_skip_cleanup_and_future_migration',
    'test_batched_replay_rollback_and_empty',
    'test_reconciliation_serializes_and_reviewer_edit_survives',
    'test_generic_and_singular_lifecycle',
})
PYTHONS = ('3.11', '3.12', '3.13', '3.14')
POSTGRES = ('14', '16')
PLATFORMS = ('windows-latest', 'macos-latest')


def warehouse_matrix():
    result = {
        f'postgres-full ({python}, {postgres})': {
            'python': python, 'postgres': postgres, 'engine': '1.12.5', 'minimum': 41,
        }
        for python in PYTHONS for postgres in POSTGRES
    }
    result['dbt-lower-bound'] = {
        'python': '3.11', 'postgres': '16', 'engine': '1.11.0', 'minimum': 41,
    }
    return result


def full_jobs():
    return set(warehouse_matrix()) | {
        f'package-full ({python})' for python in PYTHONS
    } | {
        f'bigquery-compile-full ({python})' for python in PYTHONS
    } | {
        f'portability-full ({os_name}, {python})'
        for os_name in PLATFORMS for python in PYTHONS
    } | {'candidate-qa / memory-and-exports', 'candidate-qa / installed-wheel-browser'}


def junit_counts(path, *, core=False, minimum=41, module=None):
    cases = list(ET.parse(path).getroot().iter('testcase'))
    if module:
        cases = [case for case in cases if case.attrib.get('classname', '').split('.')[-1] == module]
    names = [case.attrib['name'] for case in cases]
    counts = {'collected': len(cases), 'passed': 0, 'failed': 0, 'errors': 0, 'skipped': 0}
    for case in cases:
        outcome = next((key for tag, key in (('failure', 'failed'), ('error', 'errors'),
                        ('skipped', 'skipped')) if case.find(tag) is not None), 'passed')
        counts[outcome] += 1
    if core and (len(names) != len(CORE_POSTGRES_TESTS) or set(names) != CORE_POSTGRES_TESTS):
        raise ValueError('Postgres core selection must contain exactly the eight required tests')
    if counts['passed'] < minimum or any(counts[key] for key in ('failed', 'errors', 'skipped')):
        raise ValueError(f'Incomplete or unsuccessful warehouse results: {counts}; minimum={minimum}')
    return counts


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def record(junit, output, job, python, postgres, engine, *, core=False):
    counts = junit_counts(junit, core=core, minimum=8 if core else 41)
    engine_python = os.environ.get('DBT_DQM_ENGINE_PYTHON', sys.executable)
    versions = json.loads(command(engine_python, '-c',
        'import importlib.metadata as m,json; print(json.dumps({n:m.version(n) '
        'for n in ("dbt-core","dbt-postgres")}))'))
    import psycopg2

    with psycopg2.connect(os.environ['DBT_DQM_TEST_DSN']) as connection, connection.cursor() as cursor:
        cursor.execute('show server_version')
        server_version = cursor.fetchone()[0]
    if '.'.join(platform.python_version_tuple()[:2]) != python:
        raise ValueError('Test Python does not match the requested matrix entry')
    if server_version.split('.')[0] != postgres or versions['dbt-core'] != engine:
        raise ValueError('Warehouse or engine version does not match the requested matrix entry')
    freeze = command('uv', 'pip', 'freeze', '--python', engine_python)
    data = {
        'schema_version': 1, 'scope': 'postgres-and-platform', 'job': job,
        'repository': os.environ.get('GITHUB_REPOSITORY'),
        'run_id': os.environ.get('GITHUB_RUN_ID'),
        'run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
        'event': os.environ.get('GITHUB_EVENT_NAME'),
        'workflow': os.environ.get('GITHUB_WORKFLOW_REF'),
        'sha': command('git', 'rev-parse', 'HEAD'),
        'tree': command('git', 'rev-parse', 'HEAD^{tree}'),
        'worktree_dirty': bool(command('git', 'status', '--porcelain', '--untracked-files=no')),
        'matrix': {'python': python, 'postgres': postgres, 'engine': engine},
        'installed': {**versions, 'python': platform.python_version(), 'postgres': server_version},
        'dependency_digest': hashlib.sha256(freeze.encode()).hexdigest(),
        'suite': 'postgres-core' if core else 'postgres-full', 'counts': counts,
        'started_at': os.environ.get('VALIDATION_STARTED_AT') or next(
            (suite.attrib['timestamp'] for suite in ET.parse(junit).getroot().iter('testsuite')
             if 'timestamp' in suite.attrib), None),
        'finished_at': datetime.now(UTC).isoformat(),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2) + '\n')
    print(json.dumps(data, indent=2))


def verify_jobs(pages, needs, records, *, sha, tree, run_id, run_attempt, repository):
    if not needs or any(job.get('result') != 'success' for job in needs.values()):
        raise ValueError('Every required parent job must succeed')
    jobs = [job for page in pages for job in page['jobs']]
    for name in full_jobs():
        matching = [job for job in jobs if job.get('name') == name]
        if len(matching) != 1 or matching[0].get('conclusion') != 'success':
            raise ValueError(f'Missing, duplicate, or unsuccessful required job: {name}')
    by_job = {}
    for value in records:
        name = value['job']
        if name in by_job:
            raise ValueError(f'Duplicate warehouse evidence: {name}')
        by_job[name] = value
    if set(by_job) != set(warehouse_matrix()):
        raise ValueError('Warehouse evidence must cover every required matrix entry')
    trees = set()
    for name, entry in warehouse_matrix().items():
        value = by_job[name]
        if (value.get('schema_version') != 1 or value.get('scope') != 'postgres-and-platform'
                or value.get('worktree_dirty') is not False
                or value.get('suite') != 'postgres-full' or value.get('sha') != sha
                or value.get('repository') != repository or value.get('run_id') != run_id
                or value.get('run_attempt') != run_attempt
                or value.get('matrix') != {key: entry[key] for key in ('python', 'postgres', 'engine')}):
            raise ValueError(f'Stale or mismatched warehouse evidence: {name}')
        counts = value.get('counts', {})
        if (counts.get('passed', 0) < entry['minimum']
                or counts.get('collected') != counts.get('passed')
                or any(counts.get(key) != 0 for key in ('failed', 'errors', 'skipped'))):
            raise ValueError(f'Incomplete warehouse evidence: {name}')
        tree = value.get('tree', '')
        if len(tree) != 40 or any(c not in '0123456789abcdef' for c in tree):
            raise ValueError(f'Missing source tree: {name}')
        trees.add(tree)
    if trees != {tree}:
        raise ValueError('Warehouse jobs did not all test the candidate source tree')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    emit = sub.add_parser('record')
    emit.add_argument('--junit', type=Path, required=True)
    emit.add_argument('--output', type=Path, required=True)
    emit.add_argument('--job', required=True)
    emit.add_argument('--python', required=True)
    emit.add_argument('--postgres', required=True)
    emit.add_argument('--engine', default='1.12.5')
    emit.add_argument('--core', action='store_true')
    verify = sub.add_parser('verify')
    verify.add_argument('--jobs', type=Path, required=True)
    verify.add_argument('--evidence', type=Path, required=True)
    junit = sub.add_parser('junit')
    junit.add_argument('--path', type=Path, required=True)
    junit.add_argument('--minimum', type=int, required=True)
    junit.add_argument('--module')
    args = parser.parse_args()
    try:
        if args.action == 'record':
            record(args.junit, args.output, args.job, args.python, args.postgres, args.engine,
                   core=args.core)
        elif args.action == 'junit':
            print(json.dumps(junit_counts(args.path, minimum=args.minimum, module=args.module)))
        else:
            records = [json.loads(path.read_text()) for path in args.evidence.rglob('evidence.json')]
            verify_jobs(json.loads(args.jobs.read_text()), json.loads(os.environ['VALIDATION_NEEDS']),
                        records, sha=os.environ['GITHUB_SHA'], tree=command('git', 'rev-parse', 'HEAD^{tree}'),
                        run_id=os.environ['GITHUB_RUN_ID'],
                        run_attempt=os.environ['GITHUB_RUN_ATTEMPT'],
                        repository=os.environ['GITHUB_REPOSITORY'])
            print('Every required Postgres and platform validation job passed; BigQuery is not certified.')
    except (ValueError, KeyError, OSError, ET.ParseError, subprocess.SubprocessError) as error:
        parser.exit(1, f'Validation failed: {error}\n')


if __name__ == '__main__':
    main()
