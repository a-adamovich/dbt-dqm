{#
  Reset only synthetic dbt-dqm demo artifacts.

  This is intentionally a demo-project macro, not part of the distributable package. It makes
  repeatable lifecycle tests possible and is useful after a pre-release identity-format change.
  It never touches consumer data: every object is fully qualified to this target's demo schema.
#}
{% macro reset_demo_dqm() %}
  {% if not execute %}
    {{ return('') }}
  {% endif %}

  {% for identifier, relation_type in [
    ('dqm_area_health', 'view'),
    ('dqm_test_health', 'view'),
    ('dqm_issue_timeline', 'view'),
    ('dqm_current_issues', 'view'),
    ('dqm_all_issues', 'view'),
    ('dqm_reconcile', 'view'),
    ('dqm_missed_issues', 'table'),
    ('dqm_issue_events', 'table'),
    ('dqm_reconciliation_inputs', 'table'),
    ('dqm_reconciliation_receipts', 'table'),
    ('dqm_reconciliation_runs', 'table'),
    ('dqm_reconciliation_control', 'table'),
    ('dqm_install', 'table'),
    ('dqm_annotation_changes', 'table'),
    ('dqm_issue_occurrences', 'table'),
    ('dqm_test_executions', 'table'),
    ('dqm_issue_observations', 'table'),
    ('dqm_reconciliation_state', 'table'),
    ('dqm_schema_migrations', 'table'),
    ('dqm_app_change_staging', 'table'),
    ('demo_customer_email_invalid', 'table'),
    ('demo_order_amount_invalid', 'table'),
    ('demo_order_line_invalid', 'table'),
    ('demo_dqm_failure', 'table')
  ] %}
    {% set relation = api.Relation.create(
      database=target.database, schema=target.schema, identifier=identifier, type=relation_type
    ) %}
    {% do run_query('drop ' ~ relation_type ~ ' if exists ' ~ relation) %}
  {% endfor %}
{% endmacro %}
