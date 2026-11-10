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


def write_timings(monkeypatch, directory, *records):
    monkeypatch.setenv(phase_timing.ENVIRONMENT, str(directory))
    for stage, phase, detail, seconds, *runner in records:
        monkeypatch.setenv('PYTEST_CURRENT_TEST', f'tests/a.py::test_one[und:ci] ({stage})')
        phase_timing.record(phase, detail, seconds, **({'runner': runner[0]} if runner else {}))


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


def test_fixture_setup_is_separate_from_tracking_installation(monkeypatch, tmp_path):
    write_timings(monkeypatch, tmp_path,
                  ('setup', 'fixture', 'create dataset', 2.0),
                  ('setup', 'fixture', 'prepare project', 0.5),
                  ('setup', 'dbt', 'seed', 10.0),
                  ('call', 'dbt', 'build --select package:dbt_dqm', 30.0),
                  ('call', 'dbt', 'run --select demo_records', 5.0),
                  ('call', 'dbt', 'build --select package:dbt_dqm', 40.0),
                  ('teardown', 'fixture', 'delete datasets', 3.0))
    junit = tmp_path / 'junit.xml'
    root = ET.Element('testsuite')
    ET.SubElement(root, 'testcase', name='test_one[und:ci]', classname='a', time='100.5')
    ET.ElementTree(root).write(junit)
    row = measure.summarize_timings(tmp_path, junit)['tests']['test_one[und:ci]']
    assert (row['fixture_dataset_seconds'], row['fixture_project_seconds'], row['fixture_seed_seconds']) == (2.0, .5, 10.0)
    assert row['fixture_setup_seconds'] == 12.5
    # Only the first package deployment installs; the second is scenario work.
    assert row['installation_seconds'] == 30.0
    assert row['installation_steps'] == ['harness: build --select package:dbt_dqm']
    assert row['installation_includes_reconcile'] is False
    assert row['case_seconds'] == 100.5 and row['scenario_seconds'] == 55.0


def test_repeated_initialization_is_not_installation(monkeypatch, tmp_path):
    write_timings(monkeypatch, tmp_path,
                  ('call', 'dbt', 'dbt_dqm.initialize_dqm', 8.0),
                  ('call', 'dbt', 'dbt_dqm.collect_results', 4.0),
                  ('call', 'dbt', 'dbt_dqm.initialize_dqm', 3.0),
                  ('call', 'dbt', 'build --select package:dbt_dqm', 20.0))
    row = measure.summarize_timings(tmp_path)['tests']['test_one[und:ci]']
    assert row['installation_seconds'] == 28.0 and row['initialize_repeat_seconds'] == 3.0
    # This deployment reconciled captured evidence, so its time is an upper bound on installing.
    assert row['installation_includes_reconcile'] is True
    assert row['case_seconds'] is None and row['scenario_seconds'] is None


def test_runner_metrics_are_kept_and_attributed(monkeypatch, tmp_path):
    first = {'phase_seconds': {'parse': 2.0, 'initialize_dqm': 6.0, 'test': 9.0, 'collect_results': 4.0, 'run': 20.0},
             'sql_calls': {'test': 3}, 'query_jobs': {'maintain_dqm': ['a', 'a', 'b']}}
    second = {'phase_seconds': {'initialize_dqm': 2.0, 'run': 15.0}, 'sql_calls': {'test': 1},
              'query_jobs': {'maintain_dqm': ['c']}}
    write_timings(monkeypatch, tmp_path, ('call', 'dbt-dqm', '', 41.0, first), ('call', 'dbt-dqm', '', 17.0, second))
    summary = measure.summarize_timings(tmp_path)
    row = summary['tests']['test_one[und:ci]']
    assert row['runner_phase_seconds'] == {'collect_results': 4.0, 'initialize_dqm': 8.0, 'parse': 2.0,
                                           'run': 35.0, 'test': 9.0}
    assert row['runner_sql_calls'] == {'test': 4} and row['runner_query_jobs'] == {'maintain_dqm': 3}
    assert row['installation_seconds'] == 26.0 and row['initialize_repeat_seconds'] == 2.0
    assert row['installation_steps'] == ['runner: initialize_dqm', 'runner: run']
    assert row['installation_includes_reconcile'] is True
    assert row['runner_metrics_missing'] == 0
    assert summary['totals']['runner_phase_seconds']['run'] == 35.0


def test_engine_without_query_metrics_is_null_not_zero(monkeypatch, tmp_path):
    write_timings(monkeypatch, tmp_path,
                  ('call', 'dbt-dqm', '', 5.0, {'phase_seconds': {'run': 4.0}, 'sql_calls': {}, 'query_jobs': {}}))
    summary = measure.summarize_timings(tmp_path)
    row = summary['tests']['test_one[und:ci]']
    assert row['runner_sql_calls'] is None and row['runner_query_jobs'] is None
    assert summary['totals']['runner_sql_calls'] is None


def test_missing_runner_metrics_are_counted(monkeypatch, tmp_path):
    write_timings(monkeypatch, tmp_path, ('call', 'dbt-dqm', '', 5.0, None))
    row = measure.summarize_timings(tmp_path)['tests']['test_one[und:ci]']
    assert row['runner_metrics_missing'] == 1 and row['dbt_dqm_runs'] == 1


def read_records(directory):
    return [json.loads(line) for path in directory.glob('*.jsonl') for line in path.read_text().splitlines()]


def test_stale_runner_artifact_is_not_attributed_to_this_invocation(monkeypatch, tmp_path):
    monkeypatch.setenv(phase_timing.ENVIRONMENT, str(tmp_path))
    artifact = tmp_path / 'metrics.json'
    artifact.write_text(json.dumps({'phase_seconds': {'run': 99.0}}))
    with phase_timing.timed('dbt-dqm', '', runner_metrics=artifact):
        pass
    assert read_records(tmp_path)[0]['runner'] is None


def test_real_harness_records_the_runner_artifact(monkeypatch, tmp_path):
    test_bigquery = importlib.import_module('test_bigquery')
    timing = tmp_path / 'timing'
    timing.mkdir()
    monkeypatch.setenv(phase_timing.ENVIRONMENT, str(timing))
    monkeypatch.setenv('PYTEST_CURRENT_TEST', 'tests/a.py::test_one (call)')
    project = tmp_path / 'project'
    (project / 'target').mkdir(parents=True)
    def run(command, **kwargs):
        assert command[1] == 'run'
        (project / 'target/dqm_workflow_metrics.json').write_text(json.dumps(
            {'phase_seconds': {'initialize_dqm': 1.5}, 'sql_calls': {'test': 2}, 'query_jobs': {},
             'total_seconds': 3.0, 'completion_status': 'success', 'exit_code': 0, 'owner_summary': {'private': 1}}))
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(test_bigquery.subprocess, 'run', run)
    test_bigquery.dedicated(SimpleNamespace(path=project, profiles=tmp_path), '--threads', '4')
    runner = read_records(timing)[0]['runner']
    assert runner['phase_seconds'] == {'initialize_dqm': 1.5} and runner['sql_calls'] == {'test': 2}
    assert 'owner_summary' not in runner


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
    assert 'cannot justify' in written['consolidation_note']
