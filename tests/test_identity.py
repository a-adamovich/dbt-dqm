import json

import pytest

from dbt_dqm_app.identity import normalize_signature, scheme_signature, signature_grain


def test_engine_independent_signature_keeps_order_and_unicode_codepoints():
    grain = ['source', 'é', '😀', 'é', 'a|b: c', '']
    expected = 'dqm-id-v1|k6:source|k1:é|k1:😀|k2:é|k6:a|b: c|k0:|'
    assert scheme_signature(grain) == expected
    assert signature_grain(expected) == grain
    for sort in (False, True):
        for ascii_only in (False, True):
            legacy = json.dumps({'version': 'dqm-id-v1', 'granularity': grain},
                                sort_keys=sort, ensure_ascii=ascii_only)
            assert normalize_signature(legacy, json.dumps(grain)) == expected
    assert scheme_signature(list(reversed(grain))) != expected


@pytest.mark.parametrize('signature', [
    None, '{}', '[]', '{"version":"dqm-id-v2","granularity":[]}',
    '{"version":"dqm-id-v1","granularity":[1]}',
    '{"version":"dqm-id-v1","granularity":[],"extra":0}',
    '{"version":"dqm-id-v1","granularity":[],"granularity":["x"]}',
    'dqm-id-v1|k02:id|', 'dqm-id-v1|k3:id|', 'dqm-id-v1|k2:id|extra',
])
def test_invalid_or_unknown_signatures_are_not_guessed(signature):
    with pytest.raises((ValueError, TypeError)):
        normalize_signature(signature)


def test_contradictory_granularity_is_rejected():
    with pytest.raises(ValueError, match='Contradictory'):
        normalize_signature(scheme_signature(['a', 'b']), '["b","a"]')
