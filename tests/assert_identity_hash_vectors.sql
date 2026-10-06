-- Golden vectors for the byte-level issue identity contract. This singular test runs unchanged on
-- every supported adapter; a returned row means that adapter no longer produces the documented,
-- cross-warehouse SHA-256 identity.
{% set vectors = [
  {
    'name': 'null',
    'pairs': [('customer_id', 'cast(null as ' ~ dbt.type_string() ~ ')')],
    'expected': '0169b9347fcfe450d3c8ea1f771cb7386263b0e99782fb382ff1580f357d4508'
  },
  {
    'name': 'trimmed_casefolded',
    'pairs': [('customer_id', 'lower(trim(' ~ dbt_dqm.sql_string('  C-1  ') ~ '))')],
    'expected': '02fa0e4a646bab426ef41aa78da9373808bf74fad4def119ef3938007ac0f5e2'
  },
  {
    'name': 'unicode',
    'pairs': [('city', dbt_dqm.sql_string('münchen'))],
    'expected': '3ec9dee83fe54a8498e002582bb8a3a7141019e7c4b200c52513f3d2ec61f586'
  },
  {
    'name': 'quotes_and_backslash',
    'pairs': [('name', dbt_dqm.sql_string("o'neil\"\\"))],
    'expected': 'a835c12ca50e41fe9c76ab96c717cec07c748ded6f1adc7b95b2a128e8fe58ac'
  },
  {
    'name': 'composite_order',
    'pairs': [('customer_id', dbt_dqm.sql_string('c-1')), ('order_id', dbt_dqm.sql_string('o-9'))],
    'expected': 'cc07d4db5139d14437fb9ded1d15c3728d98945105077c051cc27af2b4795131'
  },
  {
    'name': 'reversed_order_is_distinct',
    'pairs': [('order_id', dbt_dqm.sql_string('o-9')), ('customer_id', dbt_dqm.sql_string('c-1'))],
    'expected': 'dfc33030e97e9b9e6678f077f2d618bfc3330f543b19f9161b3e7bebebdd8b2b'
  }
] %}

with vectors as (
  {% for vector in vectors %}
    select
      {{ dbt_dqm.sql_string(vector.name) }} as vector_name,
      {{ dbt_dqm.sha256_hex(dbt_dqm.canonical_identity_string(vector.pairs)) }} as actual_hash,
      {{ dbt_dqm.sql_string(vector.expected) }} as expected_hash
    {% if not loop.last %}union all{% endif %}
  {% endfor %}
)

select *
from vectors
where actual_hash != expected_hash
