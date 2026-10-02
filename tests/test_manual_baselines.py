"""Manual kernel reports retain the exact reason for every baseline mismatch."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture(params=['check_receiver_readiness.py', 'check_bluetooth_publication.py'])
def fixture(request):
    source = Path(__file__).parent/'linux'/request.param
    spec = importlib.util.spec_from_file_location('baseline_'+source.stem, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot():
    return {'links': '[{"ifindex":7,"ifname":"ens3"}]',
            'addresses': json.dumps([{'ifindex': 7, 'addr_info': [
                {'local': '192.0.2.10', 'prefixlen': 24, 'valid_life_time': 100, 'preferred_life_time': 100}]}]),
            'legacy_birth': '12345', 'legacy_loopback': {'pcm0p/sub0': 'owner_pid : 1234'},
            'dhclient_births': {'2345': '6789'}, 'observation_window': {'started': 100.0, 'finished': 100.0}}


def test_identical_raw_observations_are_preserved_and_pass_without_diffs(fixture):
    before, result = snapshot(), {'checks': {}}
    after = deepcopy(before)
    fixture.compare_baseline(result, before, after)
    assert result['baseline_before'] == before and result['baseline_after'] == after
    assert result['baseline_changed_keys'] == result['baseline_differences'] == []
    assert result['checks']['legacy_and_host_baseline_preserved'] is True


def test_lease_countdown_is_admitted_only_with_raw_and_field_specific_evidence(fixture):
    before, result = snapshot(), {'checks': {}}
    after = deepcopy(before)
    addresses = json.loads(after['addresses'])
    addresses[0]['addr_info'][0]['valid_life_time'] = 97
    addresses[0]['addr_info'][0]['preferred_life_time'] = 97
    after['addresses'] = json.dumps(addresses)
    after['observation_window'] = {'started': 102.5, 'finished': 102.7}
    fixture.compare_baseline(result, before, after)
    assert result['baseline_before'] == before and result['baseline_after'] == after
    assert result['baseline_changed_keys'] == ['addresses']
    assert result['baseline_differences'] == [
        {'path': ['addresses', 0, 'addr_info', 0, 'preferred_life_time'], 'before': 100, 'after': 97},
        {'path': ['addresses', 0, 'addr_info', 0, 'valid_life_time'], 'before': 100, 'after': 97},
    ]
    assert result['baseline_countdown_policy']['quantization_seconds'] == 1
    assert len(result['baseline_accepted_countdowns']) == 2 and not result['baseline_rejected_countdowns']
    assert result['checks']['legacy_and_host_baseline_preserved'] is True


@pytest.mark.parametrize('later', [101, 100, 95, True, 97.0, -1, 0xffffffff])
def test_extension_reset_excessive_decrease_or_noninteger_lifetime_cannot_pass(fixture, later):
    before, result = snapshot(), {'checks': {}}
    after = deepcopy(before)
    addresses = json.loads(after['addresses'])
    for field in ['valid_life_time', 'preferred_life_time']:
        addresses[0]['addr_info'][0][field] = later
    after['addresses'] = json.dumps(addresses)
    after['observation_window'] = {'started': 102.5, 'finished': 102.7}
    fixture.compare_baseline(result, before, after)
    assert result['baseline_rejected_countdowns']
    assert result['checks']['legacy_and_host_baseline_preserved'] is False


@pytest.mark.parametrize('changed', ['local', 'prefixlen', 'scope', 'ifindex', 'field-type'])
def test_valid_countdowns_never_hide_address_interface_or_typed_identity_changes(fixture, changed):
    before, result = snapshot(), {'checks': {}}
    after = deepcopy(before)
    addresses = json.loads(after['addresses'])
    fields = addresses[0]['addr_info'][0]
    fields['valid_life_time'] = fields['preferred_life_time'] = 97
    if changed == 'ifindex':
        addresses[0]['ifindex'] = 8
    elif changed == 'local':
        fields['local'] = '192.0.2.11'
    elif changed == 'prefixlen':
        fields['prefixlen'] = 25
    elif changed == 'field-type':
        fields['prefixlen'] = 24.0
    else:
        fields['scope'] = 'changed'
    after['addresses'] = json.dumps(addresses)
    after['observation_window'] = {'started': 102.5, 'finished': 102.7}
    fixture.compare_baseline(result, before, after)
    assert len(result['baseline_accepted_countdowns']) == 2
    assert result['checks']['legacy_and_host_baseline_preserved'] is False


@pytest.mark.parametrize('later', [0xffffffff, 0xfffffffe, 'forever'])
def test_unlimited_integer_lifetimes_must_remain_exact(fixture, later):
    before, result = snapshot(), {'checks': {}}
    addresses = json.loads(before['addresses'])
    fields = addresses[0]['addr_info'][0]
    fields['valid_life_time'] = fields['preferred_life_time'] = 0xffffffff
    before['addresses'] = json.dumps(addresses)
    after = deepcopy(before)
    addresses[0]['addr_info'][0]['valid_life_time'] = later
    after['addresses'] = json.dumps(addresses)
    after['observation_window'] = {'started': 102.5, 'finished': 102.7}
    fixture.compare_baseline(result, before, after)
    assert not result['baseline_accepted_countdowns']
    assert result['checks']['legacy_and_host_baseline_preserved'] is (later == 0xffffffff)


def test_finite_countdown_can_reach_zero_without_wrapping_at_expiry(fixture):
    before, result = snapshot(), {'checks': {}}
    addresses = json.loads(before['addresses'])
    for field in ['valid_life_time', 'preferred_life_time']:
        addresses[0]['addr_info'][0][field] = 1
    before['addresses'] = json.dumps(addresses)
    after = deepcopy(before)
    for field in ['valid_life_time', 'preferred_life_time']:
        addresses[0]['addr_info'][0][field] = 0
    after['addresses'] = json.dumps(addresses)
    after['observation_window'] = {'started': 102.5, 'finished': 102.7}
    fixture.compare_baseline(result, before, after)
    assert result['checks']['legacy_and_host_baseline_preserved'] is True


@pytest.mark.parametrize('window', [None, {'started': 101, 'finished': 100},
                                  {'started': 99, 'finished': 101}, {'started': True, 'finished': 102},
                                  {'started': 102, 'finished': float('inf')}])
def test_missing_backwards_overlapping_or_nonfinite_read_windows_fail_closed(fixture, window):
    before, result = snapshot(), {'checks': {}}
    after = deepcopy(before)
    after['observation_window'] = window
    fixture.compare_baseline(result, before, after)
    assert result['baseline_policy_error']
    assert result['checks']['legacy_and_host_baseline_preserved'] is False


@pytest.mark.parametrize('mutation', ['float-prefix', 'bool-number', 'unlimited-float', 'added-null', 'removed-null'])
def test_type_and_field_presence_changes_have_specific_truthful_diagnostics(fixture, mutation):
    before, result = snapshot(), {'checks': {}}
    addresses = json.loads(before['addresses'])
    original = addresses[0]['addr_info'][0]
    if mutation == 'bool-number':
        original['dynamic'] = True
    elif mutation == 'unlimited-float':
        original['valid_life_time'] = 0xffffffff
    elif mutation == 'removed-null':
        original['unexpected'] = None
    before['addresses'] = json.dumps(addresses)
    after = deepcopy(before)
    altered = addresses[0]['addr_info'][0]
    if mutation == 'float-prefix':
        altered['prefixlen'] = 24.0
        field, old, new = 'prefixlen', 24, 24.0
    elif mutation == 'bool-number':
        altered['dynamic'] = 1
        field, old, new = 'dynamic', True, 1
    elif mutation == 'unlimited-float':
        altered['valid_life_time'] = float(0xffffffff)
        field, old, new = 'valid_life_time', 0xffffffff, float(0xffffffff)
    elif mutation == 'added-null':
        altered['unexpected'] = None
        field, old, new = 'unexpected', None, None
    else:
        del altered['unexpected']
        field, old, new = 'unexpected', None, None
    after['addresses'] = json.dumps(addresses)
    fixture.compare_baseline(result, before, after)
    expected = {'path': ['addresses', 0, 'addr_info', 0, field], 'before': old, 'after': new}
    if mutation == 'added-null':
        expected['before_missing'] = True
    elif mutation == 'removed-null':
        expected['after_missing'] = True
    assert result['baseline_differences'] == [expected]
    assert type(result['baseline_differences'][0]['before']) is type(old)
    assert type(result['baseline_differences'][0]['after']) is type(new)
    assert result['checks']['legacy_and_host_baseline_preserved'] is False


@pytest.mark.parametrize('key', ['legacy_birth', 'legacy_loopback', 'dhclient_births'])
def test_nonnetwork_identity_changes_are_not_hidden_by_report_formatting(fixture, key):
    before, result = snapshot(), {'checks': {}}
    after = deepcopy(before)
    after[key] = 'changed'
    fixture.compare_baseline(result, before, after)
    assert result['baseline_changed_keys'] == [key]
    assert result['baseline_differences'] and result['baseline_differences'][0]['path'][0] == key
    assert result['checks']['legacy_and_host_baseline_preserved'] is False
