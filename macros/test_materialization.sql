{# Dedicated tests retain dbt's native limit/count/store-failures contract. The runtime command
   selects this materialization only for enabled metadata-marked tests. Ordinary dbt commands
   continue to use their configured test materialization. #}
{% materialization dqm_test, default %}
  {% set limit=config.get('limit') %}
  {% set header=config.get('sql_header') if flags.REQUIRE_SQL_HEADER_IN_TEST_CONFIGS else none %}
  {% set bounded %}{{ dbt_dqm.get_limit_subquery_sql(sql,limit) }}{% endset %}
  {% set target_relation=api.Relation.create(database=database,schema=schema,identifier=model.alias,type='table') %}
  {% set existing=adapter.get_relation(database=database,schema=schema,identifier=model.alias) %}
  {% if existing %}{% do adapter.drop_relation(existing) %}{% endif %}
  {% call statement(auto_begin=true) %}
    {% if header %}{{ header }}{% endif %}
    {{ get_create_sql(target_relation,bounded) }}
  {% endcall %}
  {% do adapter.commit() %}
  {% call statement('main',fetch_result=true) %}
    {% if header %}{{ header }}{% endif %}
    {{ get_test_sql('select * from ' ~ target_relation,config.get('fail_calc'),config.get('warn_if'),config.get('error_if'),limit=none) }}
  {% endcall %}
  {{ return({'relations':[target_relation]}) }}
{% endmaterialization %}

{% macro validate_outputs(test_ids) %}
  {% set resolved=[] %}
  {% for id in test_ids %}
    {% set node=graph.nodes[id] %}
    {% set columns=dbt.get_columns_in_query(node.compiled_code) %}
    {% for key in node.config.meta.dbt_dqm.granularity %}
      {% set matches=[] %}
      {% for column in columns %}{% if column|lower==key|trim|lower %}{% do matches.append(column) %}{% endif %}{% endfor %}
      {% if matches|length!=1 %}{{ exceptions.raise_compiler_error('DQM granularity column missing or ambiguous: ' ~ key ~ ' in ' ~ node.name ~ ' (' ~ node.original_file_path ~ ')') }}{% endif %}
    {% endfor %}
    {% do resolved.append({'unique_id':id,'columns':columns}) %}
  {% endfor %}
  {% do log('DQM_COLUMNS=' ~ tojson(resolved),info=false) %}
{% endmacro %}
