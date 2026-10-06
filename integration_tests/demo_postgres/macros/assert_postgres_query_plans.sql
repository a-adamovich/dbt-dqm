{% macro assert_postgres_query_plans() %}
  {% if not execute or target.type != 'postgres' %}{{ return('') }}{% endif %}
  {% do run_query('set enable_seqscan = off') %}
  {% set checks = [
    ('latest execution', 'select * from ' ~ dbt_dqm.relation_name('dqm_test_executions')
      ~ " where test_unique_id = 'scale-fixture' order by captured_at desc, invocation_id desc limit 1"),
    ('observation identity join', 'select * from ' ~ dbt_dqm.relation_name('dqm_issue_observations')
      ~ " where test_unique_id = 'scale-fixture' and unique_id = 'identity' and invocation_id = 'invocation'"),
    ('active occurrence', 'select * from ' ~ dbt_dqm.relation_name('dqm_issue_occurrences')
      ~ " where record_status = 'Active' and test_unique_id = 'scale-fixture' and unique_id = 'identity'"),
    ('annotation audit', 'select * from ' ~ dbt_dqm.relation_name('dqm_annotation_changes')
      ~ " where occurrence_id = 'occurrence' order by changed_at desc")
  ] %}
  {% for label, statement in checks %}
    {% set plan_table = run_query('explain ' ~ statement) %}
    {% set plan = plan_table.columns[0].values() | join(' ') %}
    {% if 'Index' not in plan %}
      {{ exceptions.raise_compiler_error(label ~ ' does not have a usable Postgres index: ' ~ plan) }}
    {% endif %}
  {% endfor %}
{% endmacro %}
