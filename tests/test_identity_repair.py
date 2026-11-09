"""The repair is deliberately limited to an untouched, single-run #105 incident."""

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from dbt_dqm_app.identity import scheme_signature

spec = importlib.util.spec_from_file_location('repair105', Path(__file__).resolve().parents[1] / 'scripts/repair_identity_105.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


@pytest.fixture
def incident():
    signature = scheme_signature(['id'])
    original = {'occurrence_id': 'original', 'test_unique_id': 'test.a', 'unique_id': 'row',
        'occurrence_number': 2, 'record_status': 'Archived', 'close_reason': 'IDENTITY_CHANGED',
        'first_seen_at': '2026-11-01T00:00:00Z', 'last_seen_at': '2026-11-08T00:00:00Z',
        'first_seen_invocation': 'old', 'last_seen_invocation': 'old',
        'last_evaluated_invocation': 'capture', 'archived_at': '2026-11-09T00:00:00Z',
        'workflow_status': 'IN_PROGRESS', 'workflow_status_at_close': 'IN_PROGRESS',
        'review_verdict': 'TRUE_POSITIVE', 'notes': 'keep me', 'ticket_url': 'https://ticket/1',
        'annotation_version': 3, 'failure_row_count': 1, 'record_values_json': '{"id":"row","value":"same"}',
        'identity_scheme_signature': signature, 'granularity_signature': '["id"]'}
    successor = {**original, 'occurrence_id': 'successor', 'occurrence_number': 3,
        'record_status': 'Active', 'close_reason': None, 'archived_at': None,
        'first_seen_invocation': 'capture', 'first_seen_at': '2026-11-09T00:00:00Z',
        'last_seen_invocation': 'capture', 'last_seen_at': '2026-11-09T00:00:00Z',
        'workflow_status': 'NEW', 'workflow_status_at_close': None,
        'review_verdict': 'UNREVIEWED', 'notes': None, 'ticket_url': None,
        'annotation_updated_at': None, 'annotation_version': 0,
        'dqm_owner': 'initial-owner', 'call_to_action': 'investigate'}
    tables = {
        'dqm_install': [{'marker': 'dqm-full-v1', 'installation_id': 'install'}],
        'dqm_reconciliation_control': [{'generation': 7, 'setup_status': 'ready',
                                       'schema_version': '0008_canonical_identity_signature'}],
        'dqm_reconciliation_runs': [{'run_id': 'run', 'status': 'completed',
            'started_at': '2026-11-09T00:00:00Z', 'completed_at': '2026-11-09T00:05:00Z'}],
        'dqm_reconciliation_inputs': [{'run_id': 'run', 'test_unique_id': 'test.a', 'invocation_id': 'capture'}],
        'dqm_test_executions': [{'test_unique_id': 'test.a', 'invocation_id': 'capture',
            'collection_status': 'success', 'test_status': 'warn', 'failure_count': 1,
            'captured_issue_count': 1, 'identity_scheme_signature': signature,
            'granularity_signature': '["id"]'}],
        'dqm_issue_occurrences': [original, successor],
        'dqm_issue_observations': [{'test_unique_id': 'test.a', 'invocation_id': 'capture',
            'unique_id': 'row', 'record_values_json': original['record_values_json'],
            'failure_row_count': 1, 'initial_dqm_owner': 'initial-owner', 'initial_call_to_action': 'investigate'}],
        'dqm_issue_events': [
            {'event_id': 'closure', 'occurrence_id': 'original', 'invocation_id': 'capture',
             'event_type': 'CLOSED_STRUCTURAL', 'reason': 'IDENTITY_CHANGED'},
            {'event_id': 'opening', 'occurrence_id': 'successor', 'invocation_id': 'capture',
             'event_type': 'APPEARED'}],
        'dqm_annotation_changes': [],
    }
    return {'run_id': 'run', 'tables': tables, 'later_captures': 0}


def test_restore_original_identity_age_and_annotations_instead_of_copying_forward(incident):
    before = copy.deepcopy(incident)
    plan = repair.make_plan(incident)
    assert incident == before
    assert plan['removed'] == ['successor']
    assert plan['removed_events'] == ['closure', 'opening']
    assert plan['updates'][0]['occurrence_id'] == 'original'
    changes = plan['updates'][0]['changes']
    assert changes['record_status'] == 'Active' and changes['close_reason'] is None
    assert changes['workflow_status_at_close'] is None
    assert not {'occurrence_id', 'first_seen_at', 'first_seen_invocation', 'occurrence_number',
                'notes', 'ticket_url', 'annotation_version', 'workflow_status', 'review_verdict'} & changes.keys()
    assert plan['event_updates'] == []
    assert repair.make_plan(incident) == plan


def test_confirmed_absence_corrects_closure_and_event(incident):
    incident['tables']['dqm_issue_occurrences'].pop()
    incident['tables']['dqm_issue_observations'].clear()
    incident['tables']['dqm_issue_events'].pop()
    execution = incident['tables']['dqm_test_executions'][0]
    execution.update(test_status='pass', failure_count=0, captured_issue_count=0)
    plan = repair.make_plan(incident)
    assert plan['removed'] == []
    assert plan['updates'][0]['changes']['close_reason'] == 'Passed'
    assert plan['event_updates'][0]['event_type'] == 'DISAPPEARED'
    assert plan['event_updates'][0]['original_event_id'] == 'closure'
    assert plan['event_updates'][0]['event_id'] != 'closure'


def test_actual_payload_changes_get_a_corrected_values_event(incident):
    incident['tables']['dqm_issue_observations'][0]['record_values_json'] = '{"value":"changed","id":"row"}'
    incident['tables']['dqm_issue_occurrences'][1]['record_values_json'] = '{"value":"changed","id":"row"}'
    plan = repair.make_plan(incident)
    event = plan['event_updates'][0]
    assert event['event_type'] == 'VALUES_CHANGED' and event['changed_columns'] == 'value'
    assert json.loads(event['previous_record_values_json'])['value'] == 'same'
    assert 'closure' not in plan['removed_events']


@pytest.mark.parametrize('problem', ['limited', 'error', 'missing_observation', 'missing_execution',
    'later_capture', 'later_run', 'edited_successor', 'annotation_audit', 'missing_closure',
    'duplicate_successor', 'wrong_identity', 'migrating', 'contradictory_grain'])
def test_ambiguous_incomplete_or_edited_incidents_fail_closed(incident, problem):
    tables = incident['tables']
    if problem in ('limited', 'error'):
        tables['dqm_test_executions'][0]['collection_status'] = 'limit_reached' if problem == 'limited' else 'collection_error'
    elif problem == 'missing_observation':
        tables['dqm_issue_observations'].clear()
    elif problem == 'missing_execution':
        tables['dqm_test_executions'].clear()
    elif problem == 'later_capture':
        incident['later_captures'] = 1
    elif problem == 'later_run':
        tables['dqm_reconciliation_runs'].append({'run_id': 'later', 'status': 'started',
                                                'started_at': '2026-11-09T01:00:00Z'})
    elif problem == 'edited_successor':
        tables['dqm_issue_occurrences'][1]['annotation_version'] = 1
    elif problem == 'annotation_audit':
        tables['dqm_annotation_changes'].append({'occurrence_id': 'successor'})
    elif problem == 'missing_closure':
        tables['dqm_issue_events'].pop(0)
    elif problem == 'duplicate_successor':
        tables['dqm_issue_occurrences'].append(copy.deepcopy(tables['dqm_issue_occurrences'][1]))
    elif problem == 'wrong_identity':
        tables['dqm_test_executions'][0]['identity_scheme_signature'] = scheme_signature(['other'])
        tables['dqm_test_executions'][0]['granularity_signature'] = '["other"]'
    elif problem == 'migrating':
        tables['dqm_reconciliation_control'][0]['setup_status'] = 'migrating'
    else:
        tables['dqm_issue_occurrences'][0]['granularity_signature'] = '["wrong"]'
    with pytest.raises(ValueError):
        repair.make_plan(incident)


def test_apply_checks_all_snapshots_in_transaction_and_never_resets_tables():
    import sqlglot

    sql = repair.apply_sql(lambda name: f'`project.dataset.{name}`')
    assert sql.index('begin transaction') < sql.index('Repair snapshot changed') < sql.index('update `project.dataset.dqm_issue_occurrences`')
    assert 'Repair generation changed' in sql and 'Later captures block repair' in sql
    assert 'IDENTITY_REPAIRED' in sql and 'generation=generation+1' in sql
    assert 'drop table' not in sql.lower() and 'truncate' not in sql.lower()
    assert sqlglot.parse(sql, dialect='bigquery')


def test_preview_is_read_only_and_completed_apply_is_idempotent(incident, tmp_path, monkeypatch, capsys):
    from dbt_dqm_app import config, relations, warehouse

    settings = SimpleNamespace(adapter_type='bigquery')
    monkeypatch.setattr(config, 'load_config', lambda *a, **k: settings)
    monkeypatch.setattr(relations, 'resolve_config', lambda value: value)
    monkeypatch.setattr(warehouse, '_relation_parts', lambda *a: ('project', 'dataset', a[-1]))
    monkeypatch.setattr(repair, 'read_snapshot', lambda *a: copy.deepcopy(incident))
    client = MagicMock()
    state = {'completed': False}
    def query(sql, **kwargs):
        answer = MagicMock()
        if sql.startswith('select count(*)'):
            answer.result.return_value = [{'n': int(state['completed'])}]
        else:
            assert sql.startswith('begin transaction;')
            assert not state['completed']
            state['completed'] = True
            answer.result.return_value = []
        return answer
    client.query.side_effect = query
    monkeypatch.setattr(warehouse, 'client_for', lambda value: client)
    path = tmp_path / 'private.json'
    arguments = ['--project-dir', str(tmp_path), '--run-id', 'run', '--output', str(path)]
    repair.main(arguments)
    assert path.is_file() and not client.query.called
    digest = repair.hashlib.sha256(path.read_bytes()).hexdigest()
    repair.main([*arguments, '--apply', digest])
    assert state['completed'] and client.query.call_count == 2
    repair.main([*arguments, '--apply', digest])
    assert client.query.call_count == 3
    assert 'already complete' in capsys.readouterr().out
    with pytest.raises(SystemExit):
        repair.main([*arguments, '--apply', '0' * 64])
    assert client.query.call_count == 3
