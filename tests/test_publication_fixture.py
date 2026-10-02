"""The manual publication probe uses the real FD-only production device policy."""
import importlib.util
from pathlib import Path

from shiri.runtime.units import UnitSpec, new_unit
import pytest

SOURCE = Path(__file__).parent/'linux/check_bluetooth_publication.py'
SPEC = importlib.util.spec_from_file_location('publication_fixture', SOURCE)
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


def test_fd_only_probe_has_private_devices_without_masking_missing_sound_directory():
    service = UnitSpec(new_unit('b265eb7d', fixture.OWNER, 'bluetooth-output'), 'bluetooth-output',
                       'shiri-bridge-7', 'shiri-bridge-7', ('/usr/bin/python3.10', '/run/shiri-worker/probe.py'),
                       inaccessible=fixture.HIDDEN)
    policy = service.properties()
    assert policy['PrivateDevices'] == 'yes' and policy['DevicePolicy'] == 'strict'
    assert '/dev/snd' not in policy['InaccessiblePaths']
    assert '/dev/snd/' not in policy['DeviceAllow']
    assert service.devices == service.supplementary_groups == ()
    assert '/run/dbus/system_bus_socket' in policy['InaccessiblePaths']
    assert '/etc/shiri-v2-test' in policy['InaccessiblePaths']
    assert policy['RestrictAddressFamilies'] == 'AF_UNIX'
    # Actual script still probes a genuine host control node; removing the
    # redundant missing-path mount never removes the denial observation.
    assert "('device',os.environ['DEVICE_NODE'])" in fixture.BRIDGE
    assert "fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK)" in fixture.BRIDGE


def broker_capabilities(monkeypatch, *, missing=0, extra=0):
    mask = (sum(1 << index for index in (0, 1, 3, 4, 5, 6, 7, 10, 12, 13, 21, 23)) | extra) & ~missing
    status = '\n'.join(f'{key}:\t{mask:x}' for key in ['CapEff', 'CapPrm', 'CapInh', 'CapBnd', 'CapAmb'])
    monkeypatch.setattr(Path, 'read_text', lambda _: status)


def test_publication_requires_broker_metadata_caps_without_inheriting_them_into_workers(monkeypatch):
    broker_capabilities(monkeypatch)
    receipt = fixture.capability_admission()
    assert int(receipt['CapEff'], 16) & (1 << 3)
    assert int(receipt['CapEff'], 16) & (1 << 4)
    assert all(not int(mask, 16) & (1 << 19) for mask in receipt.values())
    bridge = UnitSpec(new_unit('b265eb7d', fixture.OWNER, 'bluetooth-output'), 'bluetooth-output',
                      'shiri-bridge-7', 'shiri-bridge-7', ('/usr/bin/python3.10', '/run/shiri-worker/probe.py'))
    output = UnitSpec(new_unit('b265eb7d', fixture.OWNER, 'owntone'), 'owntone',
                      'shiri-output-7', 'shiri-output-7', ('/usr/bin/python3.10', '/run/shiri-worker/probe.py'),
                      listen_port=3939)
    for service in (bridge, output):
        assert service.properties()['CapabilityBoundingSet'] == service.properties()['AmbientCapabilities'] == ''


@pytest.mark.parametrize('missing,extra', [(1 << 3, 0), (1 << 4, 0), (0, 1 << 19), (0, 1 << 2)])
def test_publication_refuses_missing_metadata_caps_ptrace_or_unrelated_authority(monkeypatch, missing, extra):
    broker_capabilities(monkeypatch, missing=missing, extra=extra)
    with pytest.raises(RuntimeError, match='CAP_FOWNER|CAP_FSETID|CAP_SYS_PTRACE|outside the production set'):
        fixture.capability_admission()
