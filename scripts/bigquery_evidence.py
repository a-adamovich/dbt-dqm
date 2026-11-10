"""Record a credentialed BigQuery acceptance run for release verification."""

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import release_evidence


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def record(junit, output, engine, executable):
    expected = {'dbt1': '1.12.6', 'dbt2': '2.0.11'}[engine]
    python = platform.python_version()
    if not python.startswith('3.14.'):
        raise ValueError('BigQuery release validation requires Python 3.14')
    cases = list(ET.parse(junit).getroot().iter('testcase'))
    names = [case.attrib['name'] for case in cases]
    suite, selection = release_evidence.bigquery_selection(engine)
    count = len(selection)
    if (len(names) != count or len(set(names)) != count or set(names) != selection
            or any(case.attrib.get('classname', '').split('.')[-1] != 'test_bigquery'
                   for case in cases)
            or any(any(case.find(tag) is not None for tag in ('skipped', 'failure', 'error'))
                   for case in cases)):
        raise ValueError(f'BigQuery evidence requires exactly {count} passing {suite} cases')
    version_output = command(executable, '--version')
    if not re.search(r'(?:installed:\s*|\bdbt\s+)' + re.escape(expected) + r'(?:\s|$)', version_output):
        raise ValueError('Unexpected dbt engine version')
    freeze = command('uv', 'pip', 'freeze', '--python', os.environ['DBT_DQM_ENGINE_PYTHON'])
    data = {'schema_version': 1, 'scope': 'bigquery', 'suite': suite,
            'repository': os.environ['GITHUB_REPOSITORY'], 'run_id': os.environ['GITHUB_RUN_ID'],
            'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'], 'sha': command('git', 'rev-parse', 'HEAD'),
            'tree': command('git', 'rev-parse', 'HEAD^{tree}'),
            'worktree_dirty': bool(command('git', 'status', '--porcelain', '--untracked-files=no')),
            'matrix': {'engine': engine, 'version': expected, 'python': '3.14'},
            'installed': {'python': python, 'dbt': expected},
            'dependency_digest': hashlib.sha256(freeze.encode()).hexdigest(),
            'counts': {'collected': count, 'passed': count, 'failed': 0, 'errors': 0, 'skipped': 0},
            'started_at': next((suite.attrib['timestamp'] for suite in ET.parse(junit).getroot().iter('testsuite')
                                if 'timestamp' in suite.attrib), None),
            'finished_at': datetime.now(UTC).isoformat()}
    if data['worktree_dirty'] or not data['started_at']:
        raise ValueError('BigQuery evidence requires a clean checkout and a start timestamp')
    output.write_text(json.dumps(data, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--junit', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--engine', required=True, choices=('dbt1', 'dbt2'))
    parser.add_argument('--executable', required=True)
    args = parser.parse_args()
    record(args.junit, args.output, args.engine, args.executable)
