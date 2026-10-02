"""All isolated launch/recovery readers accept the same canonical unit grammar."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from shiri.runtime import launch_gate, log_reader, units
from shiri.runtime.network import NetworkManager
from shiri.runtime.system import RuntimeFailure

OWNER = 'bc7e0c48-5d61-49b4-a9ba-bae36ef5d4b1'


@pytest.fixture(autouse=True)
def kernel_boot(monkeypatch):
    monkeypatch.setattr(units, 'boot_id', lambda: '49d00e63-ed5f-45a0-bc99-63cde4f1f15b')


@pytest.mark.parametrize('owner', ['sender', OWNER])
@pytest.mark.parametrize('role', sorted(units.ROLE_USERS))
def test_canonical_uuid_and_sender_names_are_shared_by_launch_recovery_and_logging(owner, role):
    name = units.new_unit('b265eb7d', owner, role)
    matches = [reader.UNIT_RE.fullmatch(name) for reader in (units, launch_gate, log_reader)]
    assert all(matches)
    assert len({tuple(match.groupdict().items()) for match in matches}) == 1
    assert matches[0]['role'] == role
    assert matches[0]['owner'] == owner.replace('-', '')


@pytest.mark.parametrize('owner', ['unknown-owner', 'A'*32, 'a'*31, 'a'*33])
def test_unstructured_owner_strings_never_gain_unit_admission(owner):
    name = f'shiri-b265eb7d-{owner}-bluetooth-output-{"1"*32}.service'
    assert all(not reader.UNIT_RE.fullmatch(name) for reader in (units, launch_gate, log_reader))


def test_hyphenated_role_ambiguity_never_bypasses_the_immutable_typed_role():
    name = f'shiri-b265eb7d-sender-other-bluetooth-output-{"1"*32}.service'
    assert units.UNIT_RE.fullmatch(name)['owner'] == 'sender'
    assert units.UNIT_RE.fullmatch(name)['role'] == 'other-bluetooth-output'
    with pytest.raises(RuntimeFailure, match='owned daemon unit identity'):
        units.UnitSpec(name, 'bluetooth-output', 'shiri-bridge-7', 'shiri-bridge-7', ('/usr/bin/python3.10',))
    with pytest.raises(RuntimeFailure):
        units.new_unit('b265eb7d', 'sender-other', 'bluetooth-output')


def test_new_names_cannot_choose_a_role_outside_the_existing_launch_allowlist():
    with pytest.raises(RuntimeFailure):
        units.new_unit('b265eb7d', OWNER, 'custom-root-service')


@pytest.mark.parametrize('owner', ['send-er', 's-e-n-d-e-r', '-sender', 'sender-',
                                  OWNER.replace('-', '')[:16]+'--'+OWNER.replace('-', '')[16:],
                                  OWNER.upper(), OWNER.replace('-', '').upper(), None, []])
def test_new_owner_text_is_validated_before_any_hyphens_are_removed(owner):
    with pytest.raises(RuntimeFailure, match='unit owner'):
        units.new_unit('b265eb7d', owner, 'bluetooth-output')


def test_canonical_uuid_and_explicit_lowercase_hex32_have_the_same_bounded_owner_identity():
    canonical = units.UNIT_RE.fullmatch(units.new_unit('b265eb7d', OWNER, 'bluetooth-output'))
    raw = units.UNIT_RE.fullmatch(units.new_unit('b265eb7d', OWNER.replace('-', ''), 'bluetooth-output'))
    assert canonical['owner'] == raw['owner'] == OWNER.replace('-', '')


@pytest.mark.parametrize('drift', ['installation', 'owner', 'role', 'noncanonical-key'])
def test_durable_room_key_must_match_parsed_installation_owner_and_role(tmp_path, drift):
    network = NetworkManager(tmp_path, SimpleNamespace())
    tag = network.installation_tag
    service = units.UnitSpec(units.new_unit(tag, OWNER, 'bluetooth-output'), 'bluetooth-output',
                             'shiri-bridge-7', 'shiri-bridge-7', ('/usr/bin/python3.10', '-m', 'shiri.runtime.bluetooth_output'))
    key = OWNER+':bluetooth-output'
    if drift == 'installation':
        service = replace(service, name=units.new_unit('00000000' if tag != '00000000' else 'ffffffff', OWNER, 'bluetooth-output'))
    elif drift == 'owner':
        service = replace(service, name=units.new_unit(tag, 'sender', 'bluetooth-output'))
    elif drift == 'role':
        key = OWNER+':audio'
    else:
        key = OWNER.replace('-', '')+':bluetooth-output'
    network.reserve_unit(key, service.intent())
    before = network.manifest_path.read_bytes()
    with pytest.raises(RuntimeFailure, match='canonical room owner|another installation or room'):
        NetworkManager(tmp_path, SimpleNamespace())
    assert network.manifest_path.read_bytes() == before
