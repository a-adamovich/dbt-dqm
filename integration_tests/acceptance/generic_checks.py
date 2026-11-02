"""Real-warehouse lifecycle checks shared by the generic and singular test fixtures."""

import json


def check_generic_lifecycle(demo, dedicated):
    project = getattr(demo, 'project', None) or demo.path
    snapshot = demo.occurrences if hasattr(demo, 'occurrences') else demo.snapshot
    first_tests = None
    initial_ids = None
    for scenario in ('initial', 'passed', 'recurrence'):
        variables = {'demo_generic_checks': True, 'demo_generic_scenario': scenario}
        demo.dbt('run', '--select', 'generic_check_records', variables=variables)
        dedicated(demo, '--select', 'tag:dqm_generic_checks', variables=variables)
        artifact = json.loads((project / 'target/dqm_run_results.json').read_text())
        results = [row for row in artifact['results'] if row['unique_id'].startswith('test.')]
        assert len(results) == 2
        assert {row['status'] for row in results} == {'pass' if scenario == 'passed' else 'warn'}
        manifest = json.loads((project / 'target/manifest.json').read_text())
        generic = [row for row in results if manifest['nodes'][row['unique_id']].get('test_metadata')]
        singular = [row for row in results if row['unique_id'].endswith('.generic_check_singular')]
        assert len(generic) == len(singular) == 1
        tests = {row['unique_id'] for row in results}
        if first_tests is None:
            first_tests = tests
        assert tests == first_tests

        issues = snapshot()
        assert {row['test_unique_id'] for row in issues} == tests
        active = [row for row in issues if row['record_status'] == 'Active']
        archived = [row for row in issues if row['record_status'] == 'Archived']
        if scenario == 'initial':
            assert len(active) == 2 and not archived
            initial_ids = {row['occurrence_id'] for row in active}
        elif scenario == 'passed':
            assert not active and len(archived) == 2
            assert {row['occurrence_id'] for row in archived} == initial_ids
        else:
            assert len(active) == len(archived) == 2
            assert {row['occurrence_id'] for row in archived} == initial_ids
            assert {row['occurrence_id'] for row in active}.isdisjoint(initial_ids)
            assert {(row['test_unique_id'], row['unique_id']) for row in active} == {
                (row['test_unique_id'], row['unique_id']) for row in archived}
