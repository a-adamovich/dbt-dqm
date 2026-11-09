"""Exercise the fail-closed core selection and manual validation aggregation."""

import ast
import copy
import importlib.util
import itertools
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ci_validation', ROOT / 'scripts/ci_validation.py')
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)


def workflow(name):
    return yaml.safe_load((ROOT / '.github/workflows' / name).read_text())


def test_core_marker_keeps_exact_required_postgres_scenarios():
    source = ast.parse((ROOT / 'integration_tests/acceptance/test_postgres.py').read_text())
    marked = {
        node.name for node in source.body if isinstance(node, ast.FunctionDef)
        and any(ast.unparse(decorator) == 'pytest.mark.core' for decorator in node.decorator_list)
    }
    assert marked == validation.CORE_POSTGRES_TESTS
    assert len(marked) == 8
    full = {node.name for node in source.body if isinstance(node, ast.FunctionDef)
            and node.name.startswith('test_')}
    assert len(full) >= 39  # event_payload_modes expands to three cases: at least 42 in full.


def write_junit(path, names, outcome=None):
    from xml.etree.ElementTree import Element, ElementTree, SubElement

    suite = Element('testsuite')
    for name in names:
        case = SubElement(suite, 'testcase', name=name)
        if outcome and name == names[0]:
            SubElement(case, outcome)
    ElementTree(suite).write(path)


def test_core_report_must_prove_each_named_case_ran(tmp_path):
    path = tmp_path / 'junit.xml'
    names = sorted(validation.CORE_POSTGRES_TESTS)
    write_junit(path, names)
    assert validation.junit_counts(path, core=True, minimum=8)['passed'] == 8
    for invalid in ([], names[:-1], names[:-1] + ['unrelated_test'], names + names[:1]):
        write_junit(path, invalid)
        with pytest.raises(ValueError, match='exactly the eight'):
            validation.junit_counts(path, core=True, minimum=8)


@pytest.mark.parametrize('outcome', ['skipped', 'failure', 'error'])
def test_warehouse_skip_or_failure_is_never_complete(tmp_path, outcome):
    path = tmp_path / 'junit.xml'
    write_junit(path, sorted(validation.CORE_POSTGRES_TESTS), outcome)
    with pytest.raises(ValueError, match='unsuccessful'):
        validation.junit_counts(path, core=True, minimum=8)


def test_real_engine_module_cannot_be_replaced_by_mocked_cases(tmp_path):
    from xml.etree.ElementTree import Element, ElementTree, SubElement

    path = tmp_path / 'junit.xml'
    suite = Element('testsuite')
    for index in range(20):
        SubElement(suite, 'testcase', name=f'mocked_{index}', classname='tests.test_runner')
    ElementTree(suite).write(path)
    with pytest.raises(ValueError, match='unsuccessful'):
        validation.junit_counts(path, minimum=11, module='test_dbt_v2')
    for index in range(11):
        SubElement(suite, 'testcase', name=f'real_{index}', classname='tests.test_dbt_v2')
    ElementTree(suite).write(path)
    assert validation.junit_counts(path, minimum=11, module='test_dbt_v2')['passed'] == 11


def valid_inputs():
    jobs = [{'name': name, 'conclusion': 'success'} for name in validation.full_jobs()]
    records = [{
        'schema_version': 1, 'scope': 'postgres-and-platform', 'job': name,
        'suite': 'postgres-full', 'sha': 'candidate', 'tree': 'a' * 40,
        'worktree_dirty': False,
        'repository': 'owner/repo', 'run_id': '7', 'run_attempt': '2',
        'matrix': {key: entry[key] for key in ('python', 'postgres', 'engine')},
        'counts': {'collected': 42, 'passed': 42, 'failed': 0, 'errors': 0, 'skipped': 0},
    } for name, entry in validation.warehouse_matrix().items()]
    return [{'jobs': jobs}], {'postgres-full': {'result': 'success'}}, records


def verify(pages, needs, records):
    validation.verify_jobs(pages, needs, records, sha='candidate', tree='a' * 40, repository='owner/repo',
                           run_id='7', run_attempt='2')


def test_complete_matrix_and_same_attempt_evidence_pass():
    verify(*valid_inputs())


@pytest.mark.parametrize('name', ['postgres-full (3.14, 16)',
                                 'candidate-qa / installed-wheel-browser',
                                 'candidate-qa / memory-and-exports', 'dbt-lower-bound'])
@pytest.mark.parametrize('state', ['failure', 'skipped', 'cancelled', None])
def test_every_required_job_must_succeed(name, state):
    pages, needs, records = valid_inputs()
    if state is None:
        pages[0]['jobs'] = [job for job in pages[0]['jobs'] if job['name'] != name]
    else:
        next(job for job in pages[0]['jobs'] if job['name'] == name)['conclusion'] = state
    with pytest.raises(ValueError, match='required job'):
        verify(pages, needs, records)


@pytest.mark.parametrize('field,value', [('sha', 'older'), ('run_attempt', '1'),
                                       ('run_id', '6'), ('repository', 'other/repo'),
                                       ('worktree_dirty', True)])
def test_stale_or_foreign_evidence_fails(field, value):
    pages, needs, records = valid_inputs()
    records[0][field] = value
    with pytest.raises(ValueError, match='mismatched'):
        verify(pages, needs, records)


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'low_count', 'skip', 'tree', 'matrix'])
def test_incomplete_or_ambiguous_matrix_evidence_fails(change):
    pages, needs, records = valid_inputs()
    if change == 'missing':
        records.pop()
    elif change == 'duplicate':
        records.append(copy.deepcopy(records[0]))
    elif change == 'low_count':
        records[0]['counts'].update(passed=40, collected=40)
    elif change == 'skip':
        records[0]['counts'].update(skipped=1, passed=40)
    elif change == 'tree':
        records[0]['tree'] = 'b' * 40
    else:
        records[0]['matrix']['engine'] = '1.11.0'
    with pytest.raises(ValueError):
        verify(pages, needs, records)


def test_skipped_or_empty_parent_results_fail():
    for needs in ({}, {'parent': {'result': 'skipped'}}, {'parent': {'result': 'failure'}}):
        pages, _, records = valid_inputs()
        with pytest.raises(ValueError, match='parent job'):
            verify(pages, needs, records)


def test_fast_ci_and_manual_validation_preserve_the_agreed_matrices():
    ci, full = workflow('ci.yml'), workflow('full-validation.yml')
    fast = ci['jobs']
    assert 'dbt-postgres' not in fast and 'dbt-lower-bound' not in fast
    assert fast['package-smoke']['strategy']['matrix']['python'] == ['3.11', '3.14']
    assert fast['portability']['strategy']['matrix'] == {'include': [
        {'os': 'windows-latest', 'python': '3.11'}, {'os': 'macos-latest', 'python': '3.14'}]}
    assert set(fast['required']['needs']) == set(fast) - {'required'}
    assert fast['required']['if'] == 'always()'
    assert 'github.run_id' in ci['concurrency']['group']
    assert ci['concurrency']['cancel-in-progress'] == "${{ github.event_name == 'pull_request' }}"
    assert full.get('on', full.get(True)) == {'workflow_dispatch': None}
    assert full['jobs']['postgres-full']['strategy']['matrix'] == {
        'python': list(validation.PYTHONS), 'postgres': list(validation.POSTGRES)}
    assert set(full['jobs']['validated']['needs']) == set(full['jobs']) - {'validated'}
    assert full['jobs']['validated']['if'] == 'always()'
    assert len(validation.full_jobs()) == 27
    assert len(validation.warehouse_matrix()) == 9
    expanded = set()
    for job_id, job in full['jobs'].items():
        if job_id == 'validated':
            continue
        if 'uses' in job:
            qa = workflow('candidate-qa.yml')
            expanded.update(job['name'] + ' / ' + child['name'] for child in qa['jobs'].values())
            continue
        matrix = job.get('strategy', {}).get('matrix', {})
        entries = itertools.product(*matrix.values()) if matrix else [()]
        for values in entries:
            name = job['name']
            for key, value in zip(matrix, values, strict=True):
                name = name.replace('${{ matrix.' + key + ' }}', value)
            expanded.add(name)
    assert expanded == validation.full_jobs()
    assert 'bigquery-lifecycle' not in str(full)  # Stage 1 cannot certify cloud tests.
    for flow in (ci, full, workflow('candidate-qa.yml')):
        for job in flow['jobs'].values():
            for step in job.get('steps', []):
                if step.get('uses', '').startswith('actions/checkout@'):
                    assert step['with']['ref'] == '${{ github.sha }}'
    qa = workflow('candidate-qa.yml')
    assert qa.get('on', qa.get(True)) == {'workflow_dispatch': None, 'workflow_call': None}
    for name in ('postgres-core',):
        runs = '\n'.join(step.get('run', '') for step in fast[name]['steps'])
        assert '-m core -n 4' in runs and 'for scenario' not in runs


def test_current_engine_expectations_match_the_development_pin():
    import tomllib
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    pin = next(item.split("==")[1] for item in project["dependency-groups"]["dbt1"]
               if item.startswith("dbt-core=="))
    assert {entry["engine"] for name, entry in validation.warehouse_matrix().items()
            if name != "dbt-lower-bound"} == {pin}
    assert f"--engine {pin} --core" in (ROOT / ".github/workflows/ci.yml").read_text()
    lower = workflow("full-validation.yml")["jobs"]["dbt-lower-bound"]
    step = next(s for s in lower["steps"] if s.get("name") == "Require complete warehouse evidence")
    assert step["env"]["DBT_DQM_ENGINE_PYTHON"] == "/tmp/dbt-dqm-v111-engine/bin/python"


def test_uniform_but_wrong_source_trees_cannot_replace_expected_tree():
    pages, needs, records = valid_inputs()
    for value in records:
        value["tree"] = "b" * 40
    with pytest.raises(ValueError, match="candidate source tree"):
        verify(pages, needs, records)
