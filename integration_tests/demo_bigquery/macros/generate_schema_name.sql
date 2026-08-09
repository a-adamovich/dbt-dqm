{#
  Keep every demo relation in its one pre-created dataset. This deliberately overrides dbt's
  normal custom-schema suffix for stored failures so the demo service account can stay scoped to
  BigQuery Data Editor on a single dataset.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
  {%- if custom_schema_name is none or custom_schema_name == 'dbt_test__audit' -%}
    {{ target.schema }}
  {%- else -%}
    {{ target.schema }}_{{ custom_schema_name | trim }}
  {%- endif -%}
{%- endmacro %}
