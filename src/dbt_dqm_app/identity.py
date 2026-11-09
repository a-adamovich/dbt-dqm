"""Identity metadata encoding shared by diagnostics and the one-time #105 repair.

This is not the issue hash preimage. In particular no row values are accepted here.
"""

import json
import re

PREFIX = 'dqm-id-v1|'


def scheme_signature(grain):
    if not isinstance(grain, list) or any(not isinstance(key, str) for key in grain):
        raise ValueError('Identity granularity must be an ordered list of strings')
    return PREFIX + ''.join(f'k{len(key)}:{key}|' for key in grain)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate identity metadata key')
        result[key] = value
    return result


def signature_grain(signature):
    if not isinstance(signature, str):
        raise TypeError('Identity signature is missing')
    if not signature.startswith(PREFIX):
        value = json.loads(signature, object_pairs_hook=_object)
        if (not isinstance(value, dict) or set(value) != {'version', 'granularity'}
                or value['version'] != 'dqm-id-v1'):
            raise ValueError('Unknown identity scheme')
        scheme_signature(value['granularity'])
        return value['granularity']
    grain = []
    position = len(PREFIX)
    while position < len(signature):
        match = re.match(r'k(0|[1-9][0-9]*):', signature[position:])
        if match is None:
            raise ValueError('Malformed canonical identity signature')
        start = position + match.end()
        end = start + int(match[1])
        if signature[end:end + 1] != '|':
            raise ValueError('Malformed identity key length')
        grain.append(signature[start:end])
        position = end + 1
    return grain


def normalize_signature(signature, granularity_signature=None):
    grain = signature_grain(signature)
    if granularity_signature is not None and json.loads(granularity_signature) != grain:
        raise ValueError('Contradictory identity and granularity metadata')
    return scheme_signature(grain)
