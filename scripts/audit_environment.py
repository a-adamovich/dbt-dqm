"""Audit a resolved installation; the old compatibility engine has a finite exception."""

import argparse
import json
import subprocess
from pathlib import Path

PIP_AUDIT = 'pip-audit==2.10.1'
# Maintainer-accepted only for reviewed fixtures in the isolated Core 1.11 engine.
CORE_111_SQLPARSE = frozenset({
    'GHSA-f2ff-p2ww-7p4p', 'GHSA-3496-9g83-7v6x', 'GHSA-prg7-hcfm-mfcr',
    'GHSA-pwgv-4x5q-6m9f', 'GHSA-cfqr-cjx5-5jcm',
})


def findings(report, scope):
    if not isinstance(report, dict) or not isinstance(report.get('dependencies'), list):
        raise TypeError('Audit returned no dependency inventory')
    unresolved, accepted = [], []
    for dependency in report['dependencies']:
        if dependency.get('skip_reason'):
            if dependency.get('name') == 'dbt-dqm':
                # An unpublished candidate has no advisory-service release record.
                # Keep this limitation explicit in the retained summary below.
                continue
            raise ValueError('Audit could not assess an installed dependency')
        for vulnerability in dependency.get('vulns', []):
            identifiers = {vulnerability['id'], *vulnerability.get('aliases', [])}
            permitted = (scope == 'core-1.11' and dependency['name'] == 'sqlparse'
                         and dependency['version'] == '0.5.5'
                         and bool(identifiers & CORE_111_SQLPARSE))
            (accepted if permitted else unresolved).append({
                'package': dependency['name'], 'version': dependency['version'],
                'id': vulnerability['id'], 'aliases': vulnerability.get('aliases', [])})
    return unresolved, accepted


def audit(python, output, scope):
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError('Use a fresh audit output path; retain previous attempts')
    site = subprocess.run([str(python), '-c',
                           'import sysconfig; print(sysconfig.get_path("purelib"))'],
                          check=True, capture_output=True, text=True).stdout.strip()
    if scope == 'core-1.11':
        engine = subprocess.run([str(python), '-c',
                                 'from importlib.metadata import version; print(version("dbt-core"))'],
                                check=True, capture_output=True, text=True).stdout.strip()
        if engine != '1.11.0':
            raise ValueError('Compatibility exceptions require the isolated Core 1.11.0 engine')
    result = subprocess.run(['uv', 'tool', 'run', '--from', PIP_AUDIT, 'pip-audit',
                             '--path', site, '--format', 'json', '--output', str(output)],
                            capture_output=True, text=True, check=False)
    output.with_suffix('.log').write_text(result.stdout + result.stderr)
    if result.returncode not in (0, 1) or not output.is_file():
        raise ValueError('Dependency audit did not complete successfully')
    report = json.loads(output.read_text())
    unresolved, accepted = findings(report, scope)
    if result.returncode and not unresolved and not accepted:
        raise ValueError('Audit failed without a vulnerability report')
    output.with_suffix('.summary.json').write_text(json.dumps({
        'tool': PIP_AUDIT, 'scope': scope, 'accepted': accepted, 'unresolved': unresolved,
        'unassessed_root_package': [item for item in report['dependencies']
                                    if item.get('name') == 'dbt-dqm' and item.get('skip_reason')],
    }, indent=2) + '\n')
    if unresolved:
        raise ValueError('Unaccepted dependency advisories block release validation')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--python', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--scope', choices=('runtime', 'development', 'core-1.11'), required=True)
    args = parser.parse_args()
    audit(args.python, args.output, args.scope)
