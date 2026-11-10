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


def installed_version(executable, expected):
    version_output = command(executable, '--version')
    if not re.search(r'(?:installed:\s*|\bdbt\s+)' + re.escape(expected) + r'(?:\s|$)', version_output):
        raise ValueError('Unexpected dbt engine version')


def record(junit, output, engine, executable, kind='acceptance', core_executable=None):
    expected = {'dbt1': '1.12.6', 'dbt2': '2.0.11'}[engine]
    python = platform.python_version()
    if not python.startswith('3.14.'):
        raise ValueError('BigQuery release validation requires Python 3.14')
    cases = list(ET.parse(junit).getroot().iter('testcase'))
    names = [case.attrib['name'] for case in cases]
    suite, filename, selection = release_evidence.bigquery_suite(kind, engine)
    count = len(selection)
    if (len(names) != count or len(set(names)) != count or set(names) != selection
            or any(case.attrib.get('classname', '').split('.')[-1] != filename[:-3]
                   for case in cases)
            or any(any(case.find(tag) is not None for tag in ('skipped', 'failure', 'error'))
                   for case in cases)):
        raise ValueError(f'BigQuery evidence requires exactly {count} passing {suite} cases')
    installed_version(executable, expected)
    engines = [os.environ['DBT_DQM_ENGINE_PYTHON']]
    versions = {'python': python, 'dbt': expected}
    if kind == 'upgrade':  # The proof crosses engines, so it must identify both.
        if not core_executable:
            raise ValueError('The cross-engine upgrade requires the Core executable')
        installed_version(core_executable, release_evidence.CROSS_ENGINE_CORE_VERSION)
        engines.append(os.environ['DBT_DQM_CORE_ENGINE_PYTHON'])
        versions['core'] = release_evidence.CROSS_ENGINE_CORE_VERSION
    freeze = '\n'.join(command('uv', 'pip', 'freeze', '--python', engine_python) for engine_python in engines)
    data = {'schema_version': 1, 'scope': 'bigquery', 'suite': suite,
            'repository': os.environ['GITHUB_REPOSITORY'], 'run_id': os.environ['GITHUB_RUN_ID'],
            'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'], 'sha': command('git', 'rev-parse', 'HEAD'),
            'tree': command('git', 'rev-parse', 'HEAD^{tree}'),
            'worktree_dirty': bool(command('git', 'status', '--porcelain', '--untracked-files=no')),
            'matrix': {'engine': engine, 'version': expected, 'python': '3.14'},
            'installed': versions,
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
    parser.add_argument('--kind', choices=('acceptance', 'permissions', 'upgrade'), default='acceptance')
    parser.add_argument('--core-executable')
    args = parser.parse_args()
    record(args.junit, args.output, args.engine, args.executable, args.kind, args.core_executable)
