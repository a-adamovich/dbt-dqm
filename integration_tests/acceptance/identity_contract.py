"""The same populated identity migration contract on each supported warehouse."""

import json

from dbt_dqm_app.identity import normalize_signature, signature_grain


def exercise_identity_migration(demo, adapter):
    def table(name):
        return f'@schema.{name}' if adapter == 'postgres' else f'@dataset.{name}`'

    def literal(value):
        if adapter == 'bigquery':
            value = value.replace('\\', '\\\\').replace("'", "\\'")
        else:
            value = value.replace("'", "''")
        return "'" + value + "'"

    occurrences = table('dqm_issue_occurrences')
    ledger = table('dqm_schema_migrations')
    control = table('dqm_reconciliation_control')
    root = demo.project if adapter == 'postgres' else demo.path
    (root / 'tests/ordered_identity.sql').write_text("""{{ config(enabled=var('dqm_enabled',false),
        store_failures=true,severity='warn',tags=['dqm'],meta={'dbt_dqm':{'granularity':['z_last','é','a: b']}}) }}
        select 'row' as z_last, 'a, b: c' as a_first, 'punctuation' as {{ adapter.quote('a: b') }}, 'unicode' as {{ adapter.quote('é') }}""")
    demo.capture('initial')
    demo.dbt('build', '--select', 'package:dbt_dqm')
    demo.sql(f"update {occurrences} set notes='preserved through #105', annotation_version=7 where true")
    baseline = demo.sql(f'select * from {occurrences} order by occurrence_id')
    assert baseline
    ordered = next(row for row in baseline if row['test_name'] == 'ordered_identity')
    assert list(json.loads(ordered['record_values_json'])) == ['z_last', 'a_first', 'a: b', 'é']
    assert json.loads(ordered['record_values_json'])['a_first'] == 'a, b: c'
    for name in ('dqm_issue_occurrences', 'dqm_test_executions'):
        relation = table(name)
        pairs = demo.sql(f'select distinct identity_scheme_signature,granularity_signature from {relation}')
        for index, pair in enumerate(pairs):
            canonical = pair['identity_scheme_signature']
            legacy = json.dumps({'version': 'dqm-id-v1', 'granularity': signature_grain(canonical)},
                                sort_keys=bool(index % 2))
            demo.sql(f'update {relation} set identity_scheme_signature={literal(legacy)} '
                     f'where identity_scheme_signature={literal(canonical)}')
    legacy_rows = demo.sql(f'select * from {occurrences} order by occurrence_id')
    assert legacy_rows != baseline
    demo.sql(f"delete from {ledger} where migration_id='0008_canonical_identity_signature'")
    demo.sql(f"update {control} set schema_version='0007_browse_keyset_index' where true")
    legacy_ordered = next(row for row in legacy_rows if row['occurrence_id'] == ordered['occurrence_id'])
    # Reproduce the old helper corrupting the same field name in BOTH metadata strings.
    broken_grain = json.dumps(['z_last', 'é', 'a:b'])
    broken_signature = json.dumps({'version': 'dqm-id-v1', 'granularity': ['z_last', 'é', 'a:b']})
    demo.sql(f'update {occurrences} set identity_scheme_signature={literal(broken_signature)}, '
             f'granularity_signature={literal(broken_grain)} where occurrence_id={literal(ordered["occurrence_id"])}')
    failure = demo.dbt('run-operation', 'ensure_capture_tables', success=False)
    output = demo.last_output if adapter == 'bigquery' else failure.stdout
    assert 'recorded granularity is absent from captured fields' in output
    assert not demo.sql(f"select 1 from {ledger} where migration_id='0008_canonical_identity_signature'")
    demo.sql(f'update {occurrences} set identity_scheme_signature={literal(legacy_ordered["identity_scheme_signature"])}, '
             f'granularity_signature={literal(legacy_ordered["granularity_signature"])} '
             f'where occurrence_id={literal(ordered["occurrence_id"])}')
    if adapter == 'bigquery':
        demo.sql(f'update {control} set migration_lease_until=timestamp_sub(current_timestamp(), interval 1 minute) where true')
    macro = root / 'macros/interrupt_identity.sql'
    macro.write_text("""{% macro """ + adapter + """__canonical_identity_signature_verify() %}
      {{ dbt_dqm.default__canonical_identity_signature_verify() }}
      {% if var('interrupt_identity',false) %}
        {{ dbt_dqm.assert_sql('false', "'injected identity interruption'") }}
      {% endif %}{% endmacro %}""")
    demo.dbt('run-operation', 'ensure_capture_tables', variables={'interrupt_identity': True}, success=False)
    assert demo.sql(f'select * from {occurrences} order by occurrence_id') == legacy_rows
    assert not demo.sql(f"select 1 from {ledger} where migration_id='0008_canonical_identity_signature'")
    if adapter == 'bigquery':
        demo.sql(f'update {control} set migration_lease_until=timestamp_sub(current_timestamp(), interval 1 minute) where true')
    demo.dbt('run-operation', 'ensure_capture_tables')
    assert demo.sql(f'select * from {occurrences} order by occurrence_id') == baseline
    for name in ('dqm_issue_occurrences', 'dqm_test_executions'):
        for row in demo.sql(f'select identity_scheme_signature,granularity_signature from {table(name)}'):
            assert normalize_signature(row['identity_scheme_signature'], row['granularity_signature']) == row['identity_scheme_signature']
    demo.dbt('run-operation', 'ensure_capture_tables')
    assert demo.sql(f'select * from {occurrences} order by occurrence_id') == baseline
    event_table = table('dqm_issue_events')
    changed_before = demo.sql(f"select count(*) n from {event_table} where event_type='VALUES_CHANGED'")[0]['n']
    for row in baseline:
        payload = json.dumps(dict(reversed(list(json.loads(row['record_values_json']).items()))))
        demo.sql(f'update {occurrences} set record_values_json={literal(payload)} '
                 f'where occurrence_id={literal(row["occurrence_id"])}')
    demo.capture('initial')
    demo.dbt('run', '--select', 'dqm_reconcile')
    current = demo.sql(f'select * from {occurrences} order by occurrence_id')
    assert {row['occurrence_id'] for row in current} == {row['occurrence_id'] for row in baseline}
    assert all(row['notes'] == 'preserved through #105' and row['annotation_version'] == 7 for row in current)
    assert not demo.sql(f"select 1 from {occurrences} where close_reason='IDENTITY_CHANGED'")
    assert demo.sql(f"select count(*) n from {event_table} where event_type='VALUES_CHANGED'")[0]['n'] == changed_before
