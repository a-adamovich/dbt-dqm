"""Development measurements must describe exactly what ran and never pose as release evidence."""

import importlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'integration_tests/acceptance'))
measure = importlib.import_module('bigquery_measure')
phase_timing = importlib.import_module('phase_timing')


def write_junit(path, names, *, classname='acceptance.test_bigquery', outcome=None):
    root = ET.Element('testsuite')
    for name in names:
        case = ET.SubElement(root, 'testcase', name=name, classname=classname)
        if outcome and name == names[0]:
            ET.SubElement(case, outcome)
    ET.ElementTree(root).write(path)


def test_selections_are_the_shared_definitions():
    release = measure.release_evidence
    assert measure.selection_cases('smoke')[1] == set(release.BIGQUERY_SMOKE_TESTS)
    assert measure.selection_cases('smoke')[2] == ['-m', 'bigquery_smoke']
    assert measure.selection_cases('full')[1] == release.source_names('test_bigquery.py')
    assert measure.selection_cases('upgrade')[0] == 'test_identity_upgrade.py'
    with pytest.raises(ValueError):
        measure.selection_cases('anything')


def test_counts_require_the_exact_selection(tmp_path):
    junit = tmp_path / 'junit.xml'
    write_junit(junit, ['a', 'b'])
    assert measure.report_counts(junit, {'a', 'b'}, 'test_bigquery.py')['passed'] == 2
    write_junit(junit, ['a', 'c'])
    with pytest.raises(ValueError, match='selection'):
        measure.report_counts(junit, {'a', 'b'}, 'test_bigquery.py')
    write_junit(junit, ['a', 'b'], classname='acceptance.test_other')
    with pytest.raises(ValueError, match='module'):
        measure.report_counts(junit, {'a', 'b'}, 'test_bigquery.py')


@pytest.mark.parametrize(('outcome', 'key'), [('failure', 'failed'), ('error', 'errors'), ('skipped', 'skipped')])
def test_counts_keep_unsuccessful_outcomes(tmp_path, outcome, key):
    junit = tmp_path / 'junit.xml'
    write_junit(junit, ['a', 'b'], outcome=outcome)
    counts = measure.report_counts(junit, {'a', 'b'}, 'test_bigquery.py')
    assert counts[key] == 1 and counts['passed'] == 1


def test_timing_is_silent_without_a_directory(monkeypatch, tmp_path):
    monkeypatch.delenv(phase_timing.ENVIRONMENT, raising=False)
    with phase_timing.timed('dbt', 'seed'):
        pass
    assert not list(tmp_path.iterdir())


def test_timing_records_are_summarized_per_test(monkeypatch, tmp_path):
    monkeypatch.setenv(phase_timing.ENVIRONMENT, str(tmp_path))
    for current, phase, detail in (
            ('tests/a.py::test_one[und:ci] (setup)', 'dbt', 'seed'),
            ('tests/a.py::test_one[und:ci] (call)', 'dbt', 'run --select x'),
            ('tests/a.py::test_one[und:ci] (call)', 'query', ''),
            ('tests/a.py::test_one[und:ci] (call)', 'dbt-dqm', '--full-refresh'),
            ('tests/a.py::test_one[und:ci] (teardown)', 'fixture', 'delete datasets')):
        monkeypatch.setenv('PYTEST_CURRENT_TEST', current)
        with phase_timing.timed(phase, detail):
            pass
    summary = measure.summarize_timings(tmp_path)
    row = summary['tests']['test_one[und:ci]']
    assert row['dbt_subprocesses'] == 2 and row['queries'] == 1 and row['dbt_dqm_runs'] == 1
    assert summary['totals']['dbt_subprocesses'] == 2


@pytest.mark.parametrize(('selection', 'workers', 'message'), [
    ('smoke', -1, 'negative'), ('upgrade', 2, 'serially')])
def test_measurement_refuses_invalid_requests(monkeypatch, tmp_path, selection, workers, message):
    monkeypatch.setenv('DBT_DQM_BIGQUERY_TEST_PROJECT', 'private-project')
    with pytest.raises(ValueError, match=message):
        measure.measure(selection, Path('/engine/dbt'), workers, tmp_path)


def test_measurement_output_stays_outside_the_repository(monkeypatch):
    monkeypatch.setenv('DBT_DQM_BIGQUERY_TEST_PROJECT', 'private-project')
    with pytest.raises(ValueError, match='outside'):
        measure.measure('smoke', Path('/engine/dbt'), 4, ROOT / 'target')


def test_upgrade_requires_both_engines(monkeypatch, tmp_path):
    monkeypatch.setenv('DBT_DQM_BIGQUERY_TEST_PROJECT', 'private-project')
    monkeypatch.delenv('DBT_DQM_UPGRADE_CORE_EXECUTABLE', raising=False)
    monkeypatch.delenv('DBT_DQM_UPGRADE_V2_EXECUTABLE', raising=False)
    with pytest.raises(ValueError, match='both engine'):
        measure.measure('upgrade', Path('/engine/dbt'), 0, tmp_path)


@pytest.mark.parametrize(('code', 'wrong', 'expected'), [(0, False, 'passed'), (1, False, 'failed'), (0, True, 'failed')])
def test_measurement_is_marked_as_development_only(monkeypatch, tmp_path, code, wrong, expected):
    monkeypatch.setenv('DBT_DQM_BIGQUERY_TEST_PROJECT', 'private-project')
    monkeypatch.setattr(measure, 'engine_version', lambda _: '2.0.11')
    monkeypatch.setattr(measure, 'command', lambda *args: '')
    monkeypatch.setattr(measure, 'job_metrics', lambda *args: {'status': 'unavailable', 'reason': 'Forbidden'})
    names = sorted(measure.release_evidence.BIGQUERY_SMOKE_TESTS)
    def run(arguments, **kwargs):
        assert arguments[arguments.index('bigquery_smoke') - 1] == '-m'
        assert kwargs['env']['DBT_DQM_VALIDATION_RUN_ID']
        junit = next(item.split('=', 1)[1] for item in arguments if item.startswith('--junitxml='))
        write_junit(Path(junit), names[:-1] if wrong else names)
        return SimpleNamespace(returncode=code)
    monkeypatch.setattr(measure.subprocess, 'run', run)
    assert measure.measure('smoke', Path('/engine/dbt'), 6, tmp_path) == (0 if expected == 'passed' else 1)
    written = json.loads(next(tmp_path.glob('*/measurement.json')).read_text())
    assert written['kind'] == 'development_measurement' and written['release_evidence'] is False
    assert written['status'] == expected
    assert written['query_metrics']['status'] == 'unavailable'
    assert 'not a total spending ceiling' in written['maximum_bytes_billed_note']
