{% test dqm_generic_invalid(model, column_name) %}
  {{ config(enabled=var('demo_generic_checks', false), tags=['dqm_generic_checks'],
      store_failures=true, severity='warn', limit=100,
      meta={'dbt_dqm': {'granularity': ['id']}}) }}
  select * from {{ model }} where {{ column_name }} = 'invalid'
{% endtest %}
