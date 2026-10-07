{% macro health_tests_sql() %}
  {% set tests=[] %}
  {% if execute %}{% for node in graph.nodes.values() %}{% if dbt_dqm.tracked_test(node) %}{% do tests.append(node) %}{% endif %}{% endfor %}{% endif %}
  {% for node in tests %}
    select {{ dbt_dqm.sql_string(node.unique_id) }} as test_unique_id,{{ dbt_dqm.sql_string(node.name) }} as test_name,
      {{ dbt_dqm.sql_string(node.config.severity) }} as test_severity,
      {{ dbt_dqm.sql_string((node.config.meta or {}).get('dbt_dqm',{}).get('priority')) }} as test_priority
    {% if not loop.last %}union all{% endif %}
  {% else %}
    select cast(null as {{ dbt.type_string() }}) as test_unique_id,cast(null as {{ dbt.type_string() }}) as test_name,
      cast(null as {{ dbt.type_string() }}) as test_severity,cast(null as {{ dbt.type_string() }}) as test_priority
    from {{ dbt_dqm.dual() }} where false
  {% endfor %}
{% endmacro %}

{% macro health_areas_sql(dependencies=false) %}
  {% set rows=[] %}
  {% if execute %}
    {% if dependencies %}
      {% for test in graph.nodes.values() %}{% if dbt_dqm.tracked_test(test) %}
        {% for id in test.depends_on.nodes %}
          {% set node=graph.nodes.get(id,graph.sources.get(id)) %}
          {% if node and node.package_name==project_name and node.resource_type in ['model','source'] %}
            {% do rows.append('select ' ~ dbt_dqm.sql_string(node.unique_id) ~ ' as source_unique_id,' ~ dbt_dqm.sql_string(test.unique_id) ~ ' as test_unique_id') %}
          {% endif %}
        {% endfor %}
      {% endif %}{% endfor %}
    {% else %}
      {% for node in (graph.nodes.values()|list + graph.sources.values()|list) %}
        {% if node.package_name==project_name and node.resource_type in ['model','source'] %}
          {% do rows.append('select ' ~ dbt_dqm.sql_string(node.unique_id) ~ ' as source_unique_id,' ~ dbt_dqm.sql_string(node.name) ~ ' as area_name') %}
        {% endif %}
      {% endfor %}
    {% endif %}
  {% endif %}
  {% if rows %}{{ rows|join(' union all ') }}{% else %}
    select cast(null as {{ dbt.type_string() }}) as source_unique_id,
      cast(null as {{ dbt.type_string() }}) as {{ 'test_unique_id' if dependencies else 'area_name' }}
    from {{ dbt_dqm.dual() }} where false
  {% endif %}
{% endmacro %}

{% macro safe_rate(numerator,denominator) %}
  cast({{ numerator }} as {{ dbt.type_float() }}) / nullif({{ denominator }},0)
{% endmacro %}
{% macro health_window_start() %}
  {{ dbt_dqm.timestamp_add('day',-dbt_dqm.positive_integer_var('dbt_dqm_metrics_window_days',30),dbt.current_timestamp()) }}
{% endmacro %}
