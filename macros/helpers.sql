{# Render a Python/Jinja value as a SQL string literal, dispatched per adapter because string-
   literal escaping rules genuinely differ (BigQuery interprets backslash escapes in a plain
   quoted literal; Postgres's standard-conforming strings treat backslash as a literal
   character, so only the quote character itself needs doubling). #}
{% macro sql_string(value) %}
  {{ return(adapter.dispatch('sql_string', 'dbt_dqm')(value)) }}
{% endmacro %}

{% macro default__sql_string(value) %}
  {{ dbt_dqm.unsupported_adapter_error('string literal escaping') }}
{% endmacro %}

{% macro bigquery__sql_string(value) -%}
  {%- if value is none -%}cast(null as {{ dbt.type_string() }})
  {%- else -%}'{{ (value | string)
    .replace('\\', '\\\\')
    .replace("'", "\\'" )
    .replace('\r', '\\r')
    .replace('\n', '\\n') }}'
  {%- endif -%}
{%- endmacro %}

{% macro postgres__sql_string(value) -%}
  {%- if value is none -%}cast(null as {{ dbt.type_string() }})
  {%- else -%}'{{ (value | string).replace("'", "''") }}'
  {%- endif -%}
{%- endmacro %}

{# Decide whether a result node is an explicitly metadata-marked dbt data test. #}
{% macro tracked_test(node) -%}
  {{ return(node.resource_type == 'test' and 'dbt_dqm' in (node.config.meta or {})) }}
{%- endmacro %}

{# Owners are one text field; retain all nonblank metadata entries in list order. #}
{% macro owner_text(value, key='owner') %}
  {% if value is none %}{{ return(none) }}{% endif %}
  {% if value is string %}{{ return(value|trim or none) }}{% endif %}
  {% if value is sequence and value is not string and value is not mapping %}
    {% set owners=[] %}
    {% for item in value %}
      {% if item is not string %}{{ exceptions.raise_compiler_error(key ~ ' must be a string, list of strings or null; effective value ' ~ tojson(value)) }}{% endif %}
      {% if item|trim %}{% do owners.append(item|trim) %}{% endif %}
    {% endfor %}
    {{ return(owners|join(', ') or none) }}
  {% endif %}
  {{ exceptions.raise_compiler_error(key ~ ' must be a string, list of strings or null; effective value ' ~ tojson(value)) }}
{% endmacro %}

{# Resolve a dbt-dqm warehouse relation using the target and optional schema override.
   api.Relation.create() renders correctly per adapter (BigQuery backticks, Postgres double
   quotes, ...) instead of hand-rolling one quoting convention. #}
{% macro dqm_relation(name) %}
  {% set nodes = graph.nodes if graph.nodes is defined else {} %}
  {% set model_node = nodes.get('model.dbt_dqm.' ~ name) %}
  {% if model_node %}
    {{ return(api.Relation.create(database=model_node.database, schema=model_node.schema,
        identifier=model_node.alias, type='table')) }}
  {% endif %}
  {% if model is defined and model.package_name == 'dbt_dqm' and model.name == name %}
    {{ return(this) }}
  {% endif %}
  {% set anchor = nodes.get('model.dbt_dqm.dqm_reconcile') %}
  {% set schema = anchor.schema if anchor else (target.schema if var('dbt_dqm_schema', none) is none else generate_schema_name(var('dbt_dqm_schema'), none)) %}
  {{ return(api.Relation.create(database=anchor.database if anchor else target.database,
      schema=schema, identifier=name, type='table')) }}
{% endmacro %}
{% macro relation_name(name) %}{{ return(dbt_dqm.dqm_relation(name)) }}{% endmacro %}

{# Serialize manifest values to stable compact JSON for warehouse storage. #}
{% macro compact_json(value) -%}
  {{ return(tojson(value).replace(', ', ',').replace(': ', ':')) }}
{%- endmacro %}

{# Validate cost/retention variables at compile time. Accept YAML integers only: silently
   coercing strings, floats, zero, or negative values makes pruning behavior too easy to
   misunderstand. #}
{% macro positive_integer_var(name, default=none) %}
  {% set value = var(name, default) %}
  {% if value is none %}
    {% if default is not none %}{{ exceptions.raise_compiler_error(name ~ ' must be a positive integer when set.') }}{% endif %}
    {{ return(none) }}
  {% endif %}
  {% if value is not integer or value <= 0 %}
    {{ exceptions.raise_compiler_error(name ~ ' must be a positive integer when set.') }}
  {% endif %}
  {{ return(value | int) }}
{% endmacro %}
{% macro timestamp_add(datepart, interval, expression) %}
  cast({{ dbt.dateadd(datepart, interval, expression) }} as {{ dbt.type_timestamp() }})
{% endmacro %}
