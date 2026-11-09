"""BigQuery release records must represent the complete live test selection."""

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
        assert json.loads(output.read_text())['counts']['passed'] == 33


@pytest.mark.parametrize('problem', [None, 'skipped', 'attempt', 'sha', 'engine', 'missing'])
def test_verifier_rejects_mismatched_bigquery_evidence(monkeypatch, problem):
    module = evidence.release_evidence
    tree, sha = 'a' * 40, 'b' * 40
    run = {'id': 42, 'run_attempt': 1, 'head_sha': sha}
    root = ET.Element('testsuite')
    names = sorted(module.source_names('test_bigquery.py'))
    if problem == 'missing':
        names.pop()
    for name in names:
        case = ET.SubElement(root, 'testcase', name=name, classname='acceptance.test_bigquery')
        if problem == 'skipped' and name == names[0]:
            ET.SubElement(case, 'skipped')
    record = {'schema_version': 1, 'scope': 'bigquery', 'suite': 'bigquery-full',
              'repository': 'owner/repo', 'run_id': '42', 'run_attempt': '1',
              'sha': sha, 'tree': tree, 'worktree_dirty': False,
              'matrix': {'engine': 'dbt2', 'version': '2.0.11'},
              'installed': {'dbt': '2.0.11'}, 'dependency_digest': 'c' * 64,
              'counts': {'collected': 33, 'passed': 33, 'failed': 0, 'errors': 0, 'skipped': 0}}
    if problem == 'attempt':
        record['run_attempt'] = '2'
    if problem == 'sha':
        record['sha'] = 'd' * 40
    if problem == 'engine':
        record['matrix']['version'] = '2.0.9'
    monkeypatch.setattr(module, 'commit_tree', lambda *args: tree)
    monkeypatch.setattr(module, 'artifact_files', lambda *args: {
        'evidence.json': json.dumps(record), 'junit.xml': ET.tostring(root, encoding='unicode')})
    if problem:
        with pytest.raises(ValueError):
            module.bigquery_report('owner/repo', run, 'report', tree, 'dbt2')
    else:
        assert module.bigquery_report('owner/repo', run, 'report', tree, 'dbt2') == record
