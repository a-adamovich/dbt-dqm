"""BigQuery release records must represent the complete live test selection."""

import ast
import importlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
evidence = importlib.import_module('bigquery_evidence')


@pytest.mark.parametrize('problem', [None, 'missing', 'skipped', 'duplicate', 'wrong_engine'])
def test_record_requires_complete_successful_selection(tmp_path, monkeypatch, problem):
    monkeypatch.setattr(evidence.platform, 'python_version', lambda: '3.14.6')
    root = ET.Element('testsuites')
    suite = ET.SubElement(root, 'testsuite', timestamp='2026-11-08T12:00:00Z')
    names = sorted(evidence.release_evidence.source_names('test_bigquery.py'))
    if problem == 'missing':
        names.pop()
    if problem == 'duplicate':
        names[-1] = names[0]
    for name in names:
        case = ET.SubElement(suite, 'testcase', name=name,
                             classname='integration_tests.acceptance.test_bigquery')
        if problem == 'skipped' and name == names[0]:
            ET.SubElement(case, 'skipped')
    junit = tmp_path / 'junit.xml'
    ET.ElementTree(root).write(junit)
    for key, value in {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_RUN_ID': '42',
                       'GITHUB_RUN_ATTEMPT': '1', 'DBT_DQM_ENGINE_PYTHON': '/engine/python'}.items():
        monkeypatch.setenv(key, value)
    def command(*args):
        if '--version' in args:
            return 'dbt 2.0.9' if problem == 'wrong_engine' else 'dbt 2.0.11'
        if args[:3] == ('git', 'status', '--porcelain'):
            return ''
        return 'a' * 40
    monkeypatch.setattr(evidence, 'command', command)
    output = tmp_path / 'evidence.json'
    if problem:
        with pytest.raises(ValueError):
            evidence.record(junit, output, 'dbt2', '/engine/dbt')
        assert not output.exists()
    else:
        evidence.record(junit, output, 'dbt2', '/engine/dbt')
        assert json.loads(output.read_text())['counts']['passed'] == len(names)


@pytest.mark.parametrize('problem', [None, 'skipped', 'attempt', 'sha', 'engine', 'missing', 'python'])
def test_verifier_rejects_mismatched_bigquery_evidence(monkeypatch, problem):
    module = evidence.release_evidence
    tree, sha = 'a' * 40, 'b' * 40
    run = {'id': 42, 'run_attempt': 1, 'head_sha': sha}
    root = ET.Element('testsuite')
    names = sorted(module.source_names('test_bigquery.py'))
    total = len(names)
    if problem == 'missing':
        names.pop()
    for name in names:
        case = ET.SubElement(root, 'testcase', name=name, classname='acceptance.test_bigquery')
        if problem == 'skipped' and name == names[0]:
            ET.SubElement(case, 'skipped')
    record = {'schema_version': 1, 'scope': 'bigquery', 'suite': 'bigquery-full',
              'repository': 'owner/repo', 'run_id': '42', 'run_attempt': '1',
              'sha': sha, 'tree': tree, 'worktree_dirty': False,
              'matrix': {'engine': 'dbt2', 'version': '2.0.11', 'python': '3.14'},
              'installed': {'dbt': '2.0.11', 'python': '3.14.6'}, 'dependency_digest': 'c' * 64,
              'counts': {'collected': total, 'passed': total, 'failed': 0, 'errors': 0, 'skipped': 0}}
    if problem == 'attempt':
        record['run_attempt'] = '2'
    if problem == 'sha':
        record['sha'] = 'd' * 40
    if problem == 'engine':
        record['matrix']['version'] = '2.0.9'
    if problem == 'python':
        record['installed']['python'] = '3.11.15'
    monkeypatch.setattr(module, 'commit_tree', lambda *args: tree)
    monkeypatch.setattr(module, 'artifact_files', lambda *args: {
        'evidence.json': json.dumps(record), 'junit.xml': ET.tostring(root, encoding='unicode')})
    if problem:
        with pytest.raises(ValueError):
            module.bigquery_report('owner/repo', run, 'report', tree, 'dbt2')
    else:
        assert module.bigquery_report('owner/repo', run, 'report', tree, 'dbt2') == record


def gate_junit(tmp_path, module_name, names):
    root = ET.Element('testsuites')
    suite = ET.SubElement(root, 'testsuite', timestamp='2026-11-08T12:00:00Z')
    for name in names:
        ET.SubElement(suite, 'testcase', name=name, classname='integration_tests.acceptance.' + module_name)
    junit = tmp_path / 'junit.xml'
    ET.ElementTree(root).write(junit)
    return junit


def gate_environment(monkeypatch, core='1.12.6'):
    monkeypatch.setattr(evidence.platform, 'python_version', lambda: '3.14.6')
    for key, value in {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '1',
                       'DBT_DQM_ENGINE_PYTHON': '/v2/python', 'DBT_DQM_CORE_ENGINE_PYTHON': '/core/python'}.items():
        monkeypatch.setenv(key, value)
    frozen = []
    def command(*args):
        if '--version' in args:
            return f'Core:\n  - installed: {core}\n' if args[0] == '/core/dbt' else 'dbt 2.0.11'
        if args[:3] == ('git', 'status', '--porcelain'):
            return ''
        if args[:3] == ('uv', 'pip', 'freeze'):
            frozen.append(args[-1])
            return 'pkg==1'
        return 'a' * 40
    monkeypatch.setattr(evidence, 'command', command)
    return frozen


def test_upgrade_record_identifies_both_engines(tmp_path, monkeypatch):
    frozen = gate_environment(monkeypatch)
    names = sorted(evidence.release_evidence.source_names('test_identity_upgrade.py'))
    output = tmp_path / 'evidence.json'
    evidence.record(gate_junit(tmp_path, 'test_identity_upgrade', names), output, 'dbt2', '/v2/dbt',
                    'upgrade', '/core/dbt')
    data = json.loads(output.read_text())
    assert data['suite'] == 'bigquery-upgrade' and data['installed']['core'] == '1.12.6'
    assert frozen == ['/v2/python', '/core/python']


@pytest.mark.parametrize('core_executable', [None, 'wrong'])
def test_upgrade_record_rejects_a_missing_or_wrong_core_engine(tmp_path, monkeypatch, core_executable):
    gate_environment(monkeypatch, core='1.11.0')
    names = sorted(evidence.release_evidence.source_names('test_identity_upgrade.py'))
    with pytest.raises(ValueError):
        evidence.record(gate_junit(tmp_path, 'test_identity_upgrade', names), tmp_path / 'e.json', 'dbt2',
                        '/v2/dbt', 'upgrade', core_executable and '/core/dbt')


def test_permissions_record_uses_its_own_test_file(tmp_path, monkeypatch):
    gate_environment(monkeypatch)
    names = sorted(evidence.release_evidence.source_names('test_bigquery_permissions.py'))
    output = tmp_path / 'evidence.json'
    with pytest.raises(ValueError, match='Core engine'):
        evidence.record(gate_junit(tmp_path, 'test_bigquery_permissions', names), output, 'dbt2', '/v2/dbt', 'permissions')
    with pytest.raises(ValueError, match='exactly'):
        evidence.record(gate_junit(tmp_path, 'test_bigquery', names), output, 'dbt1', '/core/dbt', 'permissions')


@pytest.mark.parametrize('problem', [None, 'core', 'wrong_suite'])
def test_gate_rejects_upgrade_evidence_without_both_engines(monkeypatch, problem):
    module = evidence.release_evidence
    tree, sha = 'a' * 40, 'b' * 40
    names = sorted(module.source_names('test_identity_upgrade.py'))
    root = ET.Element('testsuite')
    for name in names:
        ET.SubElement(root, 'testcase', name=name, classname='acceptance.test_identity_upgrade')
    record = {'schema_version': 1, 'scope': 'bigquery', 'suite': 'bigquery-upgrade', 'repository': 'owner/repo',
              'run_id': '42', 'run_attempt': '1', 'sha': sha, 'tree': tree, 'worktree_dirty': False,
              'matrix': {'engine': 'dbt2', 'version': '2.0.11', 'python': '3.14'},
              'installed': {'dbt': '2.0.11', 'python': '3.14.6', 'core': '1.12.6'}, 'dependency_digest': 'c' * 64,
              'counts': {'collected': len(names), 'passed': len(names), 'failed': 0, 'errors': 0, 'skipped': 0}}
    if problem == 'core':
        del record['installed']['core']
    if problem == 'wrong_suite':
        record['suite'] = 'bigquery-full'
    monkeypatch.setattr(module, 'commit_tree', lambda *args: tree)
    monkeypatch.setattr(module, 'artifact_files', lambda *args: {
        'evidence.json': json.dumps(record), 'junit.xml': ET.tostring(root, encoding='unicode')})
    run = {'id': 42, 'run_attempt': 1, 'head_sha': sha}
    if problem:
        with pytest.raises(ValueError):
            module.bigquery_report('owner/repo', run, 'report', tree, 'dbt2', upgrade=True)
    else:
        assert module.bigquery_report('owner/repo', run, 'report', tree, 'dbt2', upgrade=True) == record


def test_smoke_marker_matches_the_exact_selection():
    module = evidence.release_evidence
    source = ast.parse((Path(__file__).resolve().parents[1]
                        / 'integration_tests/acceptance/test_bigquery.py').read_text())
    marked = set()
    for node in source.body:
        if isinstance(node, ast.FunctionDef) and any(
                ast.unparse(item) == 'pytest.mark.bigquery_smoke' for item in node.decorator_list):
            marked.add(node.name)
    assert marked == {name.partition('[')[0] for name in module.BIGQUERY_SMOKE_TESTS}
    assert module.BIGQUERY_SMOKE_TESTS < module.source_names('test_bigquery.py')


def selection_report(names):
    root = ET.Element('testsuite')
    for name in names:
        ET.SubElement(root, 'testcase', name=name, classname='acceptance.test_bigquery')
    return ET.tostring(root, encoding='unicode')


def selection_record(suite, engine, version, count):
    return {'schema_version': 1, 'scope': 'bigquery', 'suite': suite, 'repository': 'owner/repo',
            'run_id': '42', 'run_attempt': '1', 'sha': 'b' * 40, 'tree': 'a' * 40,
            'worktree_dirty': False, 'matrix': {'engine': engine, 'version': version, 'python': '3.14'},
            'installed': {'dbt': version, 'python': '3.14.6'}, 'dependency_digest': 'c' * 64,
            'counts': {'collected': count, 'passed': count, 'failed': 0, 'errors': 0, 'skipped': 0}}


def verify_selection(monkeypatch, record, names, engine):
    module = evidence.release_evidence
    monkeypatch.setattr(module, 'commit_tree', lambda *args: 'a' * 40)
    monkeypatch.setattr(module, 'artifact_files', lambda *args: {
        'evidence.json': json.dumps(record), 'junit.xml': selection_report(names)})
    return module.bigquery_report('owner/repo', {'id': 42, 'run_attempt': 1, 'head_sha': 'b' * 40},
                                  'report', 'a' * 40, engine)


def test_core_smoke_evidence_is_accepted(monkeypatch):
    names = sorted(evidence.release_evidence.BIGQUERY_SMOKE_TESTS)
    record = selection_record('bigquery-smoke', 'dbt1', '1.12.6', len(names))
    assert verify_selection(monkeypatch, record, names, 'dbt1') == record


def test_right_number_of_passing_cases_with_wrong_names_is_rejected(monkeypatch):
    module = evidence.release_evidence
    smoke = module.BIGQUERY_SMOKE_TESTS
    wrong = sorted(module.source_names('test_bigquery.py') - smoke)[:len(smoke)]
    assert len(wrong) == len(smoke)
    record = selection_record('bigquery-smoke', 'dbt1', '1.12.6', len(wrong))
    with pytest.raises(ValueError, match='Incomplete BigQuery test selection'):
        verify_selection(monkeypatch, record, wrong, 'dbt1')


def test_smoke_cases_cannot_be_submitted_as_the_full_suite(monkeypatch):
    names = sorted(evidence.release_evidence.BIGQUERY_SMOKE_TESTS)
    for suite in ('bigquery-smoke', 'bigquery-full'):
        record = selection_record(suite, 'dbt2', '2.0.11', len(names))
        with pytest.raises(ValueError):
            verify_selection(monkeypatch, record, names, 'dbt2')


def test_full_suite_on_core_is_not_recorded_as_smoke(monkeypatch):
    names = sorted(evidence.release_evidence.source_names('test_bigquery.py'))
    record = selection_record('bigquery-full', 'dbt1', '1.12.6', len(names))
    with pytest.raises(ValueError):
        verify_selection(monkeypatch, record, names, 'dbt1')


def test_smoke_selection_must_name_existing_cases(monkeypatch):
    module = evidence.release_evidence
    monkeypatch.setattr(module, 'BIGQUERY_SMOKE_TESTS', frozenset({'test_removed_case'}))
    with pytest.raises(ValueError, match='does not exist'):
        module.bigquery_selection('dbt1')
