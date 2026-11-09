"""One-time, bounded BigQuery #105 repair. Preview is read-only; never resets an installation.

Run from the matching source checkout with the app's BigQuery extra installed. All original
scoped rows are retained in the private manifest; apply is fenced by that exact snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from dbt_dqm_app.identity import normalize_signature

MAX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 100_000
UPDATE_TYPES = {
    'record_status': 'STRING', 'close_reason': 'STRING', 'archived_at': 'TIMESTAMP',
    'workflow_status_at_close': 'STRING', 'last_seen_at': 'TIMESTAMP',
    'last_seen_invocation': 'STRING', 'last_evaluated_invocation': 'STRING',
    'record_values_json': 'STRING', 'failure_row_count': 'INT64', 'test_tags': 'STRING',
    'source_unique_id': 'STRING', 'source_relation': 'STRING', 'test_severity': 'STRING',
    'test_priority': 'STRING', 'test_criticality': 'STRING',
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _one(rows, message):
    if len(rows) != 1:
        raise ValueError(message)
    return rows[0]


def _time(value):
    return datetime.fromisoformat(value)


def event_id(test_id, occurrence_id, invocation_id, kind):
    values = [test_id, occurrence_id, invocation_id, kind]
    preimage = 'dqm-event-v1|' + ''.join('n|' if value is None else f's{len(value)}:{value}|' for value in values)
    return hashlib.sha256(preimage.encode()).hexdigest()


def make_plan(snapshot):
    """Require the untouched, single-reconciliation incident; never merge reviewer conflicts."""
    tables = snapshot['tables']
    install = _one(tables['dqm_install'], 'Expected exactly one installation')
    control = _one(tables['dqm_reconciliation_control'], 'Expected exactly one control row')
    if (install.get('marker') != 'dqm-full-v1' or not install.get('installation_id')
            or control.get('setup_status') != 'ready'
            or control.get('schema_version') != '0008_canonical_identity_signature'):
        raise ValueError('Install the matching package and complete migration 0008 first')
    run = _one([r for r in tables['dqm_reconciliation_runs'] if r['run_id'] == snapshot['run_id']],
               'Affected reconciliation is missing or ambiguous')
    if run['status'] != 'completed' or not run.get('completed_at'):
        raise ValueError('Affected reconciliation must be completed')
    if any(r['status'] == 'started' or (r['run_id'] != run['run_id']
           and _time(r['started_at']) > _time(run['started_at']))
           for r in tables['dqm_reconciliation_runs']):
        raise ValueError('A later or active reconciliation blocks this bounded repair')
    if snapshot.get('later_captures') != 0:
        raise ValueError('A later capture blocks this bounded repair')
    inputs = [r for r in tables['dqm_reconciliation_inputs'] if r['run_id'] == run['run_id']]
    if not inputs or len({r['test_unique_id'] for r in inputs}) != len(inputs):
        raise ValueError('Repair requires exactly one execution per test in the incident')
    executions = {}
    for item in inputs:
        key = item['test_unique_id'], item['invocation_id']
        executions[key] = _one([r for r in tables['dqm_test_executions']
                               if (r['test_unique_id'], r['invocation_id']) == key],
                              'Required execution evidence is unavailable')
    original_rows = tables['dqm_issue_occurrences']
    updates, removed, event_updates, removed_events = [], [], [], []
    seen = set()
    for original in original_rows:
        key = original['test_unique_id'], original['last_evaluated_invocation']
        if (original.get('close_reason') != 'IDENTITY_CHANGED'
                or original.get('record_status') != 'Archived' or key not in executions):
            continue
        execution = executions[key]
        if normalize_signature(original['identity_scheme_signature'], original['granularity_signature']) != normalize_signature(execution['identity_scheme_signature'], execution['granularity_signature']):
            continue  # a genuine identity change, not this incident
        identity = original['test_unique_id'], original['unique_id']
        if identity in seen:
            raise ValueError('Ambiguous original occurrences')
        seen.add(identity)
        if (execution.get('collection_status') not in ('success', 'not_applicable')
                or str(execution.get('test_status')).lower() not in ('pass', 'warn', 'fail')):
            raise ValueError('Incomplete capture cannot establish a pass or a safe repair')
        captured = [r for r in tables['dqm_issue_observations']
                    if (r['test_unique_id'], r['invocation_id']) == key]
        if (len(captured) != (execution.get('captured_issue_count') or 0)
                or sum(r['failure_row_count'] for r in captured) != (execution.get('failure_count') or 0)):
            raise ValueError('Retained observations do not prove complete capture')
        observation = [r for r in tables['dqm_issue_observations']
                       if (r['test_unique_id'], r['invocation_id']) == key
                       and r['unique_id'] == original['unique_id']]
        successors = [r for r in original_rows if r['test_unique_id'] == identity[0]
                      and r['unique_id'] == identity[1] and r['occurrence_id'] != original['occurrence_id']
                      and r.get('first_seen_invocation') == key[1]]
        if any(r['test_unique_id'] == identity[0] and r['unique_id'] == identity[1]
               and r.get('record_status') == 'Active' and r not in successors
               for r in original_rows):
            raise ValueError('Another active occurrence makes recovery ambiguous')
        closure = _one([r for r in tables['dqm_issue_events']
                        if r.get('occurrence_id') == original['occurrence_id']
                        and r['invocation_id'] == key[1] and r['event_type'] == 'CLOSED_STRUCTURAL'],
                       'Original structural-closure event is missing or ambiguous')
        if closure.get('reason') != 'IDENTITY_CHANGED':
            raise ValueError('Closure event contradicts the occurrence')
        if any(r.get('occurrence_id') == original['occurrence_id']
               and _time(r['changed_at']) > _time(run['completed_at'])
               for r in tables['dqm_annotation_changes']):
            raise ValueError('Later reviewer edits block repair')
        if (original.get('workflow_status') != original.get('workflow_status_at_close')
                or (original.get('annotation_updated_at') is not None
                    and _time(original['annotation_updated_at']) > _time(run['completed_at']))):
            raise ValueError('Original occurrence has later reviewer state')
        if observation:
            observed = _one(observation, 'Duplicate failure observations')
            successor = _one(successors, 'Expected one synthetic successor')
            if (successor.get('record_status') != 'Active'
                    or successor.get('occurrence_number') != original['occurrence_number'] + 1
                    or normalize_signature(successor['identity_scheme_signature']) != normalize_signature(original['identity_scheme_signature'])
                    or successor.get('annotation_version') != 0
                    or successor.get('workflow_status') != 'NEW'
                    or successor.get('review_verdict') != 'UNREVIEWED'
                    or successor.get('notes') or successor.get('ticket_url')
                    or successor.get('annotation_updated_at') is not None
                    or successor.get('last_seen_invocation') != key[1]
                    or successor.get('last_evaluated_invocation') != key[1]
                    or json.loads(successor['record_values_json']) != json.loads(observed['record_values_json'])
                    or successor.get('failure_row_count') != observed['failure_row_count']
                    or successor.get('dqm_owner') != observed.get('initial_dqm_owner')
                    or successor.get('call_to_action') != observed.get('initial_call_to_action')
                    or any(r.get('occurrence_id') == successor['occurrence_id']
                           for r in tables['dqm_annotation_changes'])):
                raise ValueError('Successor was edited or is not a synthetic continuation')
            changes = {name: successor.get(name) for name in UPDATE_TYPES}
            changes['workflow_status_at_close'] = None
            removed.append(successor['occurrence_id'])
            successor_events = [r for r in tables['dqm_issue_events']
                                if r.get('occurrence_id') == successor['occurrence_id']]
            opening = _one(successor_events, 'Unexpected successor history')
            if opening['event_type'] != 'APPEARED' or opening['invocation_id'] != key[1]:
                raise ValueError('Unexpected successor opening event')
            removed_events.extend([closure['event_id'], opening['event_id']])
            if json.loads(original['record_values_json']) != json.loads(observed['record_values_json']):
                event_updates.append({'event_id': closure['event_id'], 'occurrence_id': original['occurrence_id'],
                    'event_type': 'VALUES_CHANGED', 'reason': None,
                    'previous_record_values_json': original['record_values_json'],
                    'record_values_json': observed['record_values_json']})
                removed_events.remove(closure['event_id'])
        else:
            if successors:
                raise ValueError('Successor without retained failure evidence')
            changes = {name: original.get(name) for name in UPDATE_TYPES}
            changes['close_reason'] = 'Passed'
            event_updates.append({'event_id': closure['event_id'], 'occurrence_id': original['occurrence_id'],
                'event_type': 'DISAPPEARED', 'reason': 'Passed',
                'previous_record_values_json': original['record_values_json'],
                'record_values_json': original['record_values_json']})
        updates.append({'occurrence_id': original['occurrence_id'], 'changes': changes})
    if not updates:
        raise ValueError('No proven #105 closures found; no changes planned')
    originals = {r['occurrence_id']: r for r in original_rows}
    for update in event_updates:
        update['original_event_id'] = update['event_id']
        original = originals[update['occurrence_id']]
        update['event_id'] = event_id(original['test_unique_id'], original['occurrence_id'],
                                      original['last_evaluated_invocation'], update['event_type'])
        if any(row['event_id'] == update['event_id']
               for row in tables['dqm_issue_events']):
            raise ValueError('Corrected event identity already exists')
        before = json.loads(update['previous_record_values_json'])
        after = json.loads(update['record_values_json'])
        missing = object()
        update['changed_columns'] = ','.join(sorted(key for key in before.keys() | after.keys()
            if before.get(key, missing) != after.get(key, missing))) or None
    return {'schema_version': 1, 'issue': 105, 'snapshot_digest': digest(snapshot),
            'installation_id': install['installation_id'], 'generation': control['generation'],
            'run_id': run['run_id'], 'updates': updates, 'removed': removed,
            'event_updates': event_updates, 'removed_events': removed_events}


def scopes(table, run_parameter='@run_id'):
    affected = f'select test_unique_id from {table("dqm_reconciliation_inputs")} where run_id={run_parameter}'
    occurrence_ids = f'select occurrence_id from {table("dqm_issue_occurrences")} where test_unique_id in ({affected})'
    return {name: ('true' if name in ('dqm_install', 'dqm_reconciliation_control', 'dqm_reconciliation_runs')
        else f'occurrence_id in ({occurrence_ids})' if name == 'dqm_annotation_changes'
        else f'test_unique_id in ({affected})') for name in (
            'dqm_install', 'dqm_reconciliation_control', 'dqm_reconciliation_runs',
            'dqm_reconciliation_inputs', 'dqm_test_executions', 'dqm_issue_occurrences',
            'dqm_issue_observations', 'dqm_issue_events', 'dqm_annotation_changes')}


def read_snapshot(client, table, run_id, maximum_bytes):
    from google.cloud import bigquery

    config = bigquery.QueryJobConfig(maximum_bytes_billed=maximum_bytes,
        query_parameters=[bigquery.ScalarQueryParameter('run_id', 'STRING', run_id)])
    tables, raw = {}, {}
    for name, predicate in scopes(table).items():
        query = f'select to_json_string(t) as raw from {table(name)} t where {predicate} limit {MAX_ROWS + 1}'
        values = [r['raw'] for r in client.query(query, job_config=config).result()]
        if len(values) > MAX_ROWS:
            raise ValueError('Repair scope exceeds the supported row limit')
        raw[name] = sorted(values)
        tables[name] = [json.loads(value) for value in sorted(values)]
    later = client.query(f'select count(*) n from {table("dqm_test_executions")} '
        f'where captured_at>(select max(captured_at) from {table("dqm_reconciliation_inputs")} '
        'where run_id=@run_id)', job_config=config).result()
    value = {'run_id': run_id, 'tables': tables, 'raw': raw,
             'later_captures': next(iter(later))['n']}
    latest_control = sorted(r['raw'] for r in client.query(
        f'select to_json_string(t) raw from {table("dqm_reconciliation_control")} t',
        job_config=config).result())
    if latest_control != raw['dqm_reconciliation_control']:
        raise ValueError('Installation changed while preparing the preview')
    if len(encoded(value)) > MAX_BYTES:
        raise ValueError('Repair scope exceeds the private manifest size limit')
    return value


def apply_sql(table):
    """Recheck exact rows in one transaction, including edits that don't bump generation."""
    checks = []
    for name, predicate in scopes(table).items():
        checks.append(f"""assert (select count(*) from {table(name)} where {predicate}) =
          array_length(json_value_array(@snapshot,'$.raw.{name}'))
          and not exists(select 1 from {table(name)} t where {predicate} and to_json_string(t)
            not in unnest(json_value_array(@snapshot,'$.raw.{name}'))) as 'Repair snapshot changed';""")
    fields = ','.join(f"cast(json_value(item,'$.changes.{name}') as {kind}) as {name}"
                      for name, kind in UPDATE_TYPES.items())
    assignments = ','.join(f'{name}=source.{name}' for name in UPDATE_TYPES)
    return f"""begin transaction;
      assert (select count(*) from {table('dqm_reconciliation_control')}
        where generation=cast(json_value(@plan,'$.generation') as int64) and setup_status='ready')=1
        as 'Repair generation changed';
      {''.join(checks)}
      assert (not exists(select 1 from {table('dqm_test_executions')} where captured_at>
        (select max(captured_at) from {table('dqm_reconciliation_inputs')} where run_id=@run_id)))
        as 'Later captures block repair';
      create temp table repair_updates as select json_value(item,'$.occurrence_id') occurrence_id,{fields}
        from unnest(json_query_array(@plan,'$.updates')) item;
      update {table('dqm_issue_occurrences')} as target set {assignments}
        from repair_updates source where target.occurrence_id=source.occurrence_id;
      delete from {table('dqm_issue_events')} where event_id in unnest(json_value_array(@plan,'$.removed_events'));
      update {table('dqm_issue_events')} as target set
        event_id=json_value(source,'$.event_id'),event_type=json_value(source,'$.event_type'),reason=json_value(source,'$.reason'),
        previous_record_values_json=if(target.payload_mode='full',json_value(source,'$.previous_record_values_json'),null),
        record_values_json=if(target.payload_mode='full',json_value(source,'$.record_values_json'),null),
        changed_columns=if(target.payload_mode='none',null,json_value(source,'$.changed_columns')),
        previous_payload_digest=if(target.payload_mode='none',null,lower(to_hex(sha256(json_value(source,'$.previous_record_values_json'))))),
        payload_digest=if(target.payload_mode='none',null,lower(to_hex(sha256(json_value(source,'$.record_values_json')))))
        from unnest(json_query_array(@plan,'$.event_updates')) source
        where target.event_id=json_value(source,'$.original_event_id');
      delete from {table('dqm_issue_occurrences')} where occurrence_id in unnest(json_value_array(@plan,'$.removed'));
      insert into {table('dqm_issue_events')}
        (event_id,test_unique_id,occurrence_id,unique_id,invocation_id,event_type,event_at,reason,actor)
      select lower(to_hex(sha256(concat(@digest,'|',occurrence.occurrence_id)))),occurrence.test_unique_id,
        occurrence.occurrence_id,occurrence.unique_id,@run_id,'IDENTITY_REPAIRED',current_timestamp(),@digest,session_user()
      from {table('dqm_issue_occurrences')} occurrence join repair_updates using(occurrence_id);
      update {table('dqm_reconciliation_control')} set generation=generation+1 where true;
      commit transaction;"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-dir', type=Path, required=True)
    parser.add_argument('--profiles-dir', type=Path)
    parser.add_argument('--target')
    parser.add_argument('--vars', default='{}')
    parser.add_argument('--dbt-executable')
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output', type=Path, required=True, help='Private manifest outside the checkout')
    parser.add_argument('--apply', metavar='SHA256', help='Apply the reviewed manifest with this digest')
    parser.add_argument('--maximum-bytes-billed', type=int, default=1024 ** 3)
    args = parser.parse_args(argv)
    import yaml
    from google.cloud import bigquery

    from dbt_dqm_app.config import load_config
    from dbt_dqm_app.relations import resolve_config
    from dbt_dqm_app.warehouse import _relation_parts, client_for

    if args.maximum_bytes_billed <= 0 or Path(__file__).resolve().parents[1] in args.output.resolve().parents:
        parser.error('Use a positive per-query byte ceiling and a private output outside the checkout')
    config = resolve_config(load_config(args.project_dir, args.profiles_dir, args.target,
                                       cli_vars=yaml.safe_load(args.vars), dbt_executable=args.dbt_executable))
    if config.adapter_type != 'bigquery':
        parser.error('The incident utility supports BigQuery only; normal migration supports both warehouses')
    def table(name):
        return '`' + '.'.join(_relation_parts(config, name)) + '`'
    client = client_for(config)
    if args.apply:
        data = args.output.read_bytes()
        if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != args.apply:
            parser.error('Manifest digest mismatch or size limit exceeded')
        bundle = json.loads(data)
        if make_plan(bundle['snapshot']) != bundle['plan'] or bundle['snapshot']['run_id'] != args.run_id:
            parser.error('Repair manifest does not reproduce its plan')
        parameters = [bigquery.ScalarQueryParameter(name, 'STRING', value) for name, value in (
            ('run_id', args.run_id), ('digest', args.apply),
            ('plan', encoded(bundle['plan']).decode()), ('snapshot', encoded(bundle['snapshot']).decode()))]
        query_config = bigquery.QueryJobConfig(maximum_bytes_billed=args.maximum_bytes_billed,
                                             query_parameters=parameters)
        completed = list(client.query(f"select count(*) n from {table('dqm_issue_events')} "
            "where event_type='IDENTITY_REPAIRED' and reason=@digest", job_config=query_config).result())
        if completed[0]['n'] == len(bundle['plan']['updates']):
            print('This exact repair is already complete; no changes made.')
            return
        if completed[0]['n']:
            parser.error('Incomplete repair audit; investigate before applying')
        client.query(apply_sql(table), job_config=query_config).result()
        print('Repair applied. Originals remain in the private manifest; synchronize reviewer workspaces.')
    else:
        snapshot = read_snapshot(client, table, args.run_id, args.maximum_bytes_billed)
        bundle = {'snapshot': snapshot, 'plan': make_plan(snapshot)}
        data = encoded(bundle)
        if len(data) > MAX_BYTES:
            parser.error('Private manifest exceeds size limit')
        with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
            output.write(data)
        print(f'Preview only: {len(bundle["plan"]["updates"])} original occurrences, '
              f'{len(bundle["plan"]["removed"])} synthetic successors. SHA256={hashlib.sha256(data).hexdigest()}')


if __name__ == '__main__':
    main()
