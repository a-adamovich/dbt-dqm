{# A CLI invocation cannot inject a mutated manifest. Select the bounded implementation here,
   only for the dedicated workflow. Ordinary dbt commands use the native implementation. #}
{% materialization test, default %}
  {% if var('_dbt_dqm_workflow', false) and var('dqm_enabled', false) and dbt_dqm.tracked_test(model) %}
    {{ return(dbt_dqm.execute_dqm_test()) }}
  {% else %}
    {{ return(dbt.materialization_test_default()) }}
  {% endif %}
{% endmaterialization %}

{% materialization dqm_test, default %}
  {{ return(dbt_dqm.execute_dqm_test()) }}
{% endmaterialization %}

{% macro execute_dqm_test() %}
  {% set limit=config.get('limit') %}
  {% set header=config.get('sql_header') %}
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
{% endmacro %}

{% macro validate_outputs(test_ids, compiled_queries=none) %}
  {% set resolved=[] %}
  {% for id in test_ids %}
    {% set node=graph.nodes[id] %}
    {% set query=compiled_queries.get(id) if compiled_queries is not none else node.compiled_code %}
    {% if not query %}{{ exceptions.raise_compiler_error('DQM compiled test SQL is missing: ' ~ id) }}{% endif %}
    {% set columns=dbt.get_columns_in_query(query) %}
    {% for key in node.config.meta.dbt_dqm.granularity %}
      {% set matches=[] %}
      {% for column in columns %}{% if column|lower==key|trim|lower %}{% do matches.append(column) %}{% endif %}{% endfor %}
      {% if matches|length!=1 %}{{ exceptions.raise_compiler_error('DQM granularity column missing or ambiguous: ' ~ key ~ ' in ' ~ node.name ~ ' (' ~ node.original_file_path ~ ')') }}{% endif %}
    {% endfor %}
    {% do resolved.append({'unique_id':id,'columns':columns}) %}
  {% endfor %}
  {% do log('DQM_COLUMNS=' ~ tojson(resolved),info=true) %}
{% endmacro %}
