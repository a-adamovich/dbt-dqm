{# Schema declarations are also the explicit insert contract: annotations never enter matched updates. #}
{% macro table_schemas() %}
  {{ return({
    'dqm_issue_occurrences': [
      ('occurrence_id', 'string'),
      ('test_unique_id', 'string'),
      ('test_name', 'string'),
      ('unique_id', 'string'),
      ('occurrence_number', 'int'),
      ('record_values_json', 'string'),
      ('failure_row_count', 'int'),
      ('test_tags', 'string'),
      ('source_unique_id', 'string'),
      ('source_relation', 'string'),
      ('test_severity', 'string'),
      ('first_seen_at', 'timestamp'),
      ('last_seen_at', 'timestamp'),
      ('archived_at', 'timestamp'),
      ('record_status', 'string'),
      ('close_reason', 'string'),
      ('first_seen_invocation', 'string'),
      ('last_seen_invocation', 'string'),
      ('last_evaluated_invocation', 'string'),
      ('workflow_status', 'string'),
      ('test_status', 'string'),
      ('call_to_action', 'string'),
      ('ticket_url', 'string'),
      ('notes', 'string'),
      ('poc_responsible', 'string'),
      ('annotation_updated_at', 'timestamp'),
      ('annotation_version', 'int'),
      ('granularity_signature', 'string'),
      ('identity_scheme_signature', 'string'),
      ('test_priority', 'string'),
      ('test_criticality', 'string'),
      ('review_verdict', 'string'),
      ('workflow_status_at_close', 'string')
    ],
    'dqm_test_executions': [
      ('invocation_id', 'string'),
      ('captured_at', 'timestamp'),
      ('command', 'string'),
      ('test_unique_id', 'string'),
      ('test_name', 'string'),
      ('test_status', 'string'),
      ('failure_count', 'int'),
      ('test_tags', 'string'),
      ('test_meta', 'string'),
      ('execution_time', 'float'),
      ('message', 'string'),
      ('failure_relation', 'string'),
      ('collection_status', 'string'),
      ('collection_message', 'string'),
      ('granularity_signature', 'string'),
      ('identity_scheme_signature', 'string'),
      ('source_unique_id', 'string'),
      ('source_relation', 'string'),
      ('test_severity', 'string'),
      ('test_priority', 'string'),
      ('test_criticality', 'string'),
      ('owner_conflict_identity_count', 'int'),
      ('capture_mode', 'string')
    ],
    'dqm_issue_observations': [
      ('invocation_id', 'string'),
      ('observed_at', 'timestamp'),
      ('test_unique_id', 'string'),
      ('test_name', 'string'),
      ('unique_id', 'string'),
      ('record_values_json', 'string'),
      ('failure_row_count', 'int'),
      ('initial_poc_responsible', 'string'),
      ('initial_call_to_action', 'string'),
      ('test_tags', 'string'),
      ('owner_conflict', 'int')
    ],
    'dqm_reconciliation_state': [
      ('test_unique_id', 'string'),
      ('captured_at', 'timestamp'),
      ('invocation_id', 'string')
    ],
    'dqm_schema_migrations': [
      ('migration_id', 'string'),
      ('applied_at', 'timestamp')
    ],
    'dqm_reconciliation_runs': [
      ('run_id', 'string'),
      ('generation_at_freeze', 'bigint'),
      ('stage_relation', 'string'),
      ('status', 'string'),
      ('started_at', 'timestamp'),
      ('completed_at', 'timestamp')
    ],
    'dqm_reconciliation_inputs': [
      ('run_id', 'string'),
      ('test_unique_id', 'string'),
      ('invocation_id', 'string'),
      ('captured_at', 'timestamp')
    ],
    'dqm_reconciliation_receipts': [
      ('test_unique_id', 'string'),
      ('invocation_id', 'string'),
      ('captured_at', 'timestamp'),
      ('run_id', 'string'),
      ('outcome', 'string'),
      ('reason', 'string'),
      ('actor', 'string'),
      ('recorded_at', 'timestamp')
    ],
    'dqm_issue_events': [
      ('event_id', 'string'),
      ('test_unique_id', 'string'),
      ('occurrence_id', 'string'),
      ('unique_id', 'string'),
      ('invocation_id', 'string'),
      ('event_type', 'string'),
      ('event_at', 'timestamp'),
      ('previous_record_values_json', 'string'),
      ('record_values_json', 'string'),
      ('reason', 'string'),
      ('actor', 'string')
    ],
    'dqm_annotation_changes': [
      ('batch_id', 'string'),
      ('occurrence_id', 'string'),
      ('field_name', 'string'),
      ('old_value', 'string'),
      ('new_value', 'string'),
      ('changed_at', 'timestamp'),
      ('changed_by', 'string'),
      ('base_annotation_version', 'int'),
      ('resulting_annotation_version', 'int')
    ],
    'dqm_missed_issues': [
      ('missed_issue_id', 'string'),
      ('discovered_at', 'timestamp'),
      ('reported_at', 'timestamp'),
      ('reporter', 'string'),
      ('source_unique_id', 'string'),
      ('area_label', 'string'),
      ('test_unique_id', 'string'),
      ('description', 'string'),
      ('evidence_url', 'string'),
      ('priority', 'string'),
      ('detection_method', 'string'),
      ('root_cause', 'string')
    ]
  }) }}
{% endmacro %}
{% macro table_keys() %}
  {{ return({'dqm_issue_occurrences': ['occurrence_id'], 'dqm_test_executions': ['test_unique_id', 'invocation_id'], 'dqm_issue_observations': ['test_unique_id', 'invocation_id', 'unique_id'], 'dqm_reconciliation_state': ['test_unique_id'], 'dqm_schema_migrations': ['migration_id'], 'dqm_reconciliation_runs': ['run_id'], 'dqm_reconciliation_inputs': ['run_id', 'test_unique_id', 'invocation_id'], 'dqm_reconciliation_receipts': ['test_unique_id', 'invocation_id'], 'dqm_issue_events': ['event_id'], 'dqm_annotation_changes': ['batch_id', 'occurrence_id', 'field_name'], 'dqm_missed_issues': ['missed_issue_id']}) }}
{% endmacro %}
