{#
  Reset only synthetic dbt-dqm demo artifacts.

  This is intentionally a demo-project macro, not part of the distributable package. It makes
  repeatable lifecycle tests possible and is useful after a pre-release identity-format change.
  It never touches consumer data: every object is fully qualified to this target's demo dataset.
#}
{% macro reset_demo_dqm() %}
  {% if not execute %}
    {{ return('') }}
  {% endif %}

  {% set statement %}
    drop view if exists `{{ target.database }}.{{ target.schema }}.dqm_area_health`;
    drop view if exists `{{ target.database }}.{{ target.schema }}.dqm_test_health`;
    drop view if exists `{{ target.database }}.{{ target.schema }}.dqm_issue_timeline`;
    drop view if exists `{{ target.database }}.{{ target.schema }}.dqm_reconcile`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.dqm_missed_issues`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.dqm_issue_events`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.dqm_reconciliation_inputs`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.dqm_reconciliation_receipts`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.dqm_reconciliation_runs`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.dqm_reconciliation_control`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.dqm_install`;
    drop view if exists `{{ target.database }}.{{ target.schema }}.dqm_current_issues`;
    drop view if exists `{{ target.database }}.{{ target.schema }}.dqm_all_issues`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.dqm_annotation_changes`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.dqm_issue_occurrences`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.dqm_test_executions`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.dqm_issue_observations`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.dqm_reconciliation_state`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.dqm_schema_migrations`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.dqm_app_change_staging`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.demo_customer_email_invalid`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.demo_order_amount_invalid`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.demo_order_line_invalid`;
    drop table if exists `{{ target.database }}.{{ target.schema }}.demo_dqm_failure`;
  {% endset %}
  {% do run_query(statement) %}
{% endmacro %}
