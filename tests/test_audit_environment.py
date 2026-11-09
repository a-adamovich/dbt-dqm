"""Only reviewed advisories in the isolated compatibility engine may be accepted."""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
audit = importlib.import_module('audit_environment')


@pytest.mark.parametrize('scope,package,identifier,allowed', [
    ('core-1.11', 'sqlparse', 'GHSA-f2ff-p2ww-7p4p', True),
    ('runtime', 'sqlparse', 'GHSA-f2ff-p2ww-7p4p', False),
    ('development', 'sqlparse', 'GHSA-f2ff-p2ww-7p4p', False),
    ('core-1.11', 'other', 'GHSA-f2ff-p2ww-7p4p', False),
    ('core-1.11', 'sqlparse', 'NEW-ADVISORY', False),
])
def test_exceptions_are_scoped(scope, package, identifier, allowed):
    unresolved, accepted = audit.findings({'dependencies': [
        {'name': package, 'version': '0.5.5', 'vulns': [{'id': identifier}]}]}, scope)
    assert bool(accepted) is allowed
    assert bool(unresolved) is not allowed


def test_audit_skipped_packages_are_not_a_clean_result():
    with pytest.raises(ValueError):
        audit.findings({'dependencies': [{'name': 'unknown', 'skip_reason': 'not assessable'}]}, 'runtime')


def test_unpublished_root_package_limitation_does_not_hide_upstream_failures():
    assert audit.findings({'dependencies': [
        {'name': 'dbt-dqm', 'skip_reason': 'unpublished candidate'}]}, 'runtime') == ([], [])
    with pytest.raises(ValueError):
        audit.findings({'dependencies': [
            {'name': 'dependency', 'skip_reason': 'not assessable'}]}, 'runtime')


@pytest.mark.parametrize('exit_code,writes_report', [(2, False), (0, False), (1, True)])
def test_audit_failures_never_become_clean_results(tmp_path, monkeypatch, exit_code, writes_report):
    import json
    from subprocess import CompletedProcess

    output = tmp_path / 'audit.json'
    def run(command, **kwargs):
        if command[0] == 'python':
            return CompletedProcess(command, 0, '/site-packages\n', '')
        if writes_report:
            output.write_text(json.dumps({'dependencies': []}))
        return CompletedProcess(command, exit_code, '', 'audit failure')
    monkeypatch.setattr(audit.subprocess, 'run', run)
    with pytest.raises(ValueError):
        audit.audit(Path('python'), output, 'runtime')


def test_old_audit_reports_cannot_authorize_a_new_attempt(tmp_path):
    output = tmp_path / 'audit.json'
    output.write_text('{"dependencies": []}')
    with pytest.raises(ValueError, match='fresh'):
        audit.audit(Path('python'), output, 'runtime')
    assert output.read_text() == '{"dependencies": []}'


def test_compatibility_exception_rejects_wrong_installed_engine(tmp_path, monkeypatch):
    from subprocess import CompletedProcess

    def run(command, **kwargs):
        output = '1.12.6' if 'importlib.metadata' in command[-1] else '/site-packages'
        return CompletedProcess(command, 0, output, '')
    monkeypatch.setattr(audit.subprocess, 'run', run)
    with pytest.raises(ValueError, match='isolated Core'):
        audit.audit(Path('python'), tmp_path / 'audit.json', 'core-1.11')
