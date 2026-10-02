"""Strict manual-fixture baselines with measured kernel lease quantization.

Only addr_info valid/preferred lifetimes receive countdown admission. Raw
observations stay in the protected result, and all other identities stay exact.
"""
from copy import deepcopy
import json
import math

QUANTIZATION_SECONDS = 1
UNLIMITED_LIFETIME = 0xffffffff
WINDOW = 'observation_window'
MISSING = object()


def exact_equal(a, b):
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return set(a) == set(b) and all(exact_equal(value, b[key]) for key, value in a.items())
    if isinstance(a, list):
        return len(a) == len(b) and all(exact_equal(old, new) for old, new in zip(a, b, strict=True))
    return a == b


def observation_window(before, after):
    values = []
    for snapshot in (before, after):
        window = snapshot.get(WINDOW)
        if not isinstance(window, dict) or set(window) != {'started', 'finished'}:
            raise ValueError('Missing exact monotonic observation window')
        for key in ('started', 'finished'):
            value = window[key]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError('Invalid monotonic observation window')
            values.append(value)
    old_start, old_end, new_start, new_end = values
    if not old_start <= old_end <= new_start <= new_end:
        raise ValueError('Host baseline observation windows overlap or run backwards')
    return new_start-old_end, new_end-old_start


def raw_differences(before, after, changed):
    differences = []

    def visit(a, b, path):
        if exact_equal(a, b) or len(differences) >= 64:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                visit(a.get(key, MISSING), b.get(key, MISSING), [*path, key])
        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            for index, (old, new) in enumerate(zip(a, b, strict=True)):
                visit(old, new, [*path, index])
        else:
            evidence = {'path': path, 'before': None if a is MISSING else a, 'after': None if b is MISSING else b}
            if a is MISSING:
                evidence['before_missing'] = True
            if b is MISSING:
                evidence['after_missing'] = True
            differences.append(evidence)

    for key in changed:
        a, b = before.get(key, MISSING), after.get(key, MISSING)
        if key in {'links', 'addresses'} and isinstance(a, str) and isinstance(b, str):
            try:
                a, b = json.loads(a), json.loads(b)
            except ValueError:
                pass  # Preserve malformed raw evidence; it is never normalized.
        visit(a, b, [key])
    return differences


def compare_baseline(result, before, after):
    result['baseline_before'], result['baseline_after'] = before, after
    changed = sorted(key for key in (set(before) | set(after))-{WINDOW}
                     if not exact_equal(before.get(key, MISSING), after.get(key, MISSING)))
    result['baseline_changed_keys'] = changed
    result['baseline_differences'] = raw_differences(before, after, changed)
    result['baseline_accepted_countdowns'], result['baseline_rejected_countdowns'] = [], []
    result['checks']['legacy_and_host_baseline_preserved'] = False
    try:
        elapsed_min, elapsed_max = observation_window(before, after)
        result['baseline_countdown_policy'] = {
            'observation_scope': 'ip-json-address-command',
            'quantization_seconds': QUANTIZATION_SECONDS,
            'elapsed_min_seconds': elapsed_min, 'elapsed_max_seconds': elapsed_max,
            'unlimited_lifetime': UNLIMITED_LIFETIME,
        }
        old, new = deepcopy(before), deepcopy(after)
        old.pop(WINDOW)
        new.pop(WINDOW)
        old_addresses, new_addresses = json.loads(old['addresses']), json.loads(new['addresses'])
        if not isinstance(old_addresses, list) or not isinstance(new_addresses, list):
            raise ValueError('Host address evidence is not a kernel interface list')
        for index, interface in enumerate(old_addresses):
            if (not isinstance(interface, dict) or index >= len(new_addresses)
                    or not isinstance(new_addresses[index], dict)):
                continue
            a, b = interface.get('addr_info'), new_addresses[index].get('addr_info')
            if not isinstance(a, list) or not isinstance(b, list):
                continue
            for address_index, address in enumerate(a):
                if not isinstance(address, dict) or address_index >= len(b) or not isinstance(b[address_index], dict):
                    continue
                for field in ('valid_life_time', 'preferred_life_time'):
                    value, later = address.get(field), b[address_index].get(field)
                    if type(value) is not int or not 0 <= value < UNLIMITED_LIFETIME:
                        continue  # Unlimited/noninteger values must remain exact.
                    lower = min(value, max(0, elapsed_min-QUANTIZATION_SECONDS))
                    upper = min(value, elapsed_max+QUANTIZATION_SECONDS)
                    decrease = value-later if type(later) is int else None
                    evidence = {'path': ['addresses', index, 'addr_info', address_index, field],
                                'before': value, 'after': later, 'decrease_seconds': decrease,
                                'allowed_min_seconds': lower, 'allowed_max_seconds': upper}
                    if type(later) is not int or not 0 <= later < UNLIMITED_LIFETIME or not lower <= decrease <= upper:
                        result['baseline_rejected_countdowns'].append(evidence)
                        continue
                    result['baseline_accepted_countdowns'].append(evidence)
                    address[field] = b[address_index][field] = '<verified-finite-lease-countdown>'
        old['addresses'] = json.dumps(old_addresses, sort_keys=True, separators=(',', ':'), allow_nan=False)
        new['addresses'] = json.dumps(new_addresses, sort_keys=True, separators=(',', ':'), allow_nan=False)
        result['checks']['legacy_and_host_baseline_preserved'] = exact_equal(old, new) and not result['baseline_rejected_countdowns']
    except (KeyError, TypeError, ValueError) as exc:
        result['baseline_policy_error'] = str(exc)
