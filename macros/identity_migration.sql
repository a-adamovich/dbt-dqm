{# Parse historical JSON once per distinct signature/grain pair. Both tables change in the
   migration transaction; byte comparisons remain cheap on the normal reconciliation path. #}
{% macro default__canonical_identity_signature_apply() %}{% endmacro %}

{% macro canonical_signature_sql(grain) %}
  {% if target.type == 'postgres' %}
    concat('dqm-id-v1|', coalesce((select string_agg(
      'k' || length(item.key)::text || ':' || item.key || '|', '' order by item.position)
      from jsonb_array_elements_text({{ grain }}) with ordinality item(key, position)), ''))
  {% else %}
    concat('dqm-id-v1|', coalesce((select string_agg(
      concat('k', cast(char_length(item) as string), ':', item, '|'), '' order by position)
      from unnest(json_value_array({{ grain }})) item with offset position), ''))
  {% endif %}
{% endmacro %}

{% macro default__canonical_identity_signature_backfill() %}
  {% if target.type == 'postgres' %}
    create temporary table dqm_identity_signatures on commit drop as
  {% else %}
    create or replace temp table dqm_identity_signatures default collate '' as
  {% endif %}
  with signatures as (
    select identity_scheme_signature as original, granularity_signature as grain_text
      from {{ dbt_dqm.dqm_relation('dqm_test_executions') }}
    union distinct
    select identity_scheme_signature, granularity_signature
      from {{ dbt_dqm.dqm_relation('dqm_issue_occurrences') }}
  )
  select original, grain_text,
    {% if target.type == 'postgres' %}cast(grain_text as jsonb)
    {% else %}parse_json(grain_text){% endif %} as grain
  from signatures;

  {{ dbt_dqm.assert_sql("not exists(select 1 from dqm_identity_signatures where original is null or grain_text is null or "
      ~ ("jsonb_typeof(grain)" if target.type=='postgres' else "json_type(grain)") ~ " is distinct from 'array')",
      "'DQM identity migration: missing or invalid granularity metadata; no identity was guessed.'") }}
  {{ dbt_dqm.assert_sql("not exists(select 1 from dqm_identity_signatures mapping, "
      ~ ("jsonb_array_elements(mapping.grain) item where jsonb_typeof(item)" if target.type=='postgres' else "unnest(json_query_array(mapping.grain)) item where json_type(item)")
      ~ " is distinct from 'string')", "'DQM identity migration: granularity entries must be strings.'") }}

  {% if target.type == 'postgres' %}create temporary table dqm_identity_mapping on commit drop as
  {% else %}create or replace temp table dqm_identity_mapping default collate '' as{% endif %}
  select *, {{ dbt_dqm.canonical_signature_sql('grain') }} as canonical
  from dqm_identity_signatures;

  {# Already canonical rows allow safe reapplication after interrupted setup or ledger repair.
     CASE protects the JSON parser from the deliberately non-JSON canonical format. #}
  {{ dbt_dqm.assert_sql("not exists(select 1 from dqm_identity_mapping where not coalesce(case when original=canonical then true else "
      ~ ("jsonb_typeof(original::jsonb)='object' and original::jsonb->'version'=to_jsonb('dqm-id-v1'::text) and original::jsonb->'granularity'=grain and (select count(*) from jsonb_object_keys(original::jsonb))=2"
         if target.type=='postgres' else
         "json_type(parse_json(original))='object' and json_value(original,'$.version')='dqm-id-v1' and array_length(json_keys(parse_json(original),1))=2 and json_type(json_query(parse_json(original),'$.granularity'))='array' and not exists(select 1 from unnest(json_query_array(parse_json(original),'$.granularity')) item where json_type(item)!='string') and "
         ~ dbt_dqm.canonical_signature_sql("json_query(parse_json(original),'$.granularity')") ~ "=canonical")
      ~ " end,false))", "'DQM identity migration: unknown or contradictory signature; restore original metadata before retrying.'") }}

  {# The old compact_json could corrupt a name containing ', ' or ': ' in BOTH metadata
     fields. Matching those two fields is insufficient: require the recorded grain to exist
     in retained payloads too. Never infer a historical name from the current manifest. #}
  {% for name in ['dqm_issue_occurrences','dqm_issue_observations'] %}
    {% set subject = 'payload' if name=='dqm_issue_occurrences' else 'execution' %}
    {% set invalid_payload %}
      not exists(select 1 from {{ dbt_dqm.dqm_relation(name) }} payload
      {% if name=='dqm_issue_observations' %}
        join {{ dbt_dqm.dqm_relation('dqm_test_executions') }} execution
          on execution.test_unique_id=payload.test_unique_id and execution.invocation_id=payload.invocation_id
      {% endif %}
      join dqm_identity_mapping mapping on mapping.original={{ subject }}.identity_scheme_signature
        and mapping.grain_text={{ subject }}.granularity_signature
      where exists(select 1 from
        {% if target.type=='postgres' %}jsonb_array_elements_text(mapping.grain) grain(key)
        where grain.key not in (select lower(key) from jsonb_object_keys(payload.record_values_json::jsonb) key)
        {% else %}unnest(json_value_array(mapping.grain)) grain_key
        where grain_key not in (select lower(case when starts_with(encoded_key, '"')
          then json_value(parse_json(encoded_key)) else encoded_key end)
          from unnest(json_keys(parse_json(payload.record_values_json),1)) encoded_key)
        {% endif %}))
    {% endset %}
    {{ dbt_dqm.assert_sql(invalid_payload, "'DQM identity migration: recorded granularity is absent from captured fields; historical string contents may be damaged. Preserve original manifests and investigate; no column name was guessed.'") }}
  {% endfor %}

  {% for name in ['dqm_test_executions','dqm_issue_occurrences'] %}
    update {{ dbt_dqm.dqm_relation(name) }} as target
    set identity_scheme_signature=mapping.canonical
    from dqm_identity_mapping mapping
    where {{ dbt_dqm.uncollated_string('target.identity_scheme_signature') }}={{ dbt_dqm.uncollated_string('mapping.original') }}
      and target.granularity_signature=mapping.grain_text
      and target.identity_scheme_signature!=mapping.canonical;
  {% endfor %}
{% endmacro %}

{% macro default__canonical_identity_signature_verify() %}
  {% for name in ['dqm_test_executions','dqm_issue_occurrences'] %}
    {{ dbt_dqm.assert_sql("not exists(select 1 from " ~ dbt_dqm.dqm_relation(name)
        ~ " where identity_scheme_signature is null or identity_scheme_signature not like 'dqm-id-v1|%')",
        "'DQM identity migration did not complete.'") }}
  {% endfor %}
{% endmacro %}
