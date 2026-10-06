"""Independent failure regressions. No real process or network is mutated."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx
import pytest

from shiri.domain import Room, SpeakerRef
from shiri.rpc import RpcError
from shiri.runtime.backend import OwnToneClient
from shiri.runtime.broker import Broker, RuntimeRoom
from shiri.runtime.configuration import backend_configs
from shiri.runtime.network import NetworkManager
from shiri.runtime.system import CommandResult, Runner, RuntimeFailure
from shiri.settings import Settings


def definition(**changes):
    data = {
        "id": str(uuid4()), "slot": 0, "name": "Kitchen", "airplay_name": "Kitchen input",
        "interface": "eth0", "enabled": True,
    }
    data.update(changes)
    return Room.model_validate(data)


def broker(directory):
    return Broker(Settings(
        state_dir=directory / "api", runtime_state_dir=directory / "runtime",
        runtime_dir=directory / "run", runtime_socket=directory / "run" / "runtime.sock",
    ))


def test_receiver_pcm_settings_use_native_shairport_lookup_paths(tmp_path):
    # The native receiver resolves PCM settings under its own named stanza;
    # bare text presence would accept ignored settings in general instead.
    receiver, _ = backend_configs(
        definition(), tmp_path, {"interface": "receiver0"},
        all_receiver_names=["Kitchen input"], password="private", audio_uid=1234,
    )
    sections = dict(re.findall(r"(?m)^(\w+) = \{\n(.*?)^\};", receiver.read_text(), re.DOTALL))
    for parameter in ("output_rate = 48000;", 'output_format = "S16_LE";', "output_channels = 2;"):
        assert parameter in sections["shiri"]
        assert parameter not in sections["general"]
    assert 'output_backend = "shiri";' in sections["general"]
    assert 'peer_uid = 1234;' in sections["shiri"]


@pytest.mark.parametrize("device,expected_pcm", [
    ("hw:CARD=Loopback,DEV=1,SUBDEV=7", "hw:CARD=Loopback,DEV=1,SUBDEV=7"),
    ("plughw:CARD=Speakers,DEV=2,SUBDEV=1", "plughw:CARD=Speakers,DEV=2,SUBDEV=1"),
])
def test_local_outputs_use_session_volume_without_a_shared_card_mixer(tmp_path, device, expected_pcm):
    # OwnTone's unpatched hardware path treats card as a CTL fallback and cannot
    # select Loopback's mixerless PCM. Keep the exact route and opt into only the
    # patched local ALSA session; never choose another room's Master/PCM control.
    _, own = backend_configs(
        definition(local_audio_device=device, slot=7), tmp_path,
        {"interface": "receiver0"}, all_receiver_names=[], password="private", audio_uid=1234,
    )
    content = own.read_text()
    audio = re.search(r"(?m)^audio \{ (.*?)^\}", content, re.DOTALL).group(1)
    assert 'type = "alsa"' in audio
    assert f'card = "{expected_pcm}"' in audio
    assert "software_volume = true" in audio
    assert not re.search(r"\b(?:mixer|mixer_device)\s*=", audio)
    assert content.count("software_volume") == 1


def test_unconfigured_local_output_remains_disabled(tmp_path):
    _, own = backend_configs(
        definition(), tmp_path, {"interface": "receiver0"},
        all_receiver_names=[], password="private", audio_uid=1234,
    )
    content = own.read_text()
    assert 'audio { type = "disabled" }' in content
    assert not re.search(r"\b(?:card|mixer|mixer_device|software_volume)\s*=", content)


@pytest.mark.asyncio
@pytest.mark.parametrize("device,expected_pcm", [
    ("plughw:CARD=USB,DEV=0", "plughw:CARD=USB,DEV=0,SUBDEV=0"),
    ("hw:CARD=USB,DEV=1", "hw:CARD=USB,DEV=1,SUBDEV=0"),
    ("plughw:USB,2,1", "plughw:CARD=USB,DEV=2,SUBDEV=1"),
    ("plughw:CARD=USB,DEV=255,SUBDEV=255", "plughw:CARD=USB,DEV=255,SUBDEV=255"),
])
async def test_startup_preserves_named_pcm_conversion_and_device_indexes(
    tmp_path, device, expected_pcm, monkeypatch,
):
    service = broker(tmp_path)
    room = RuntimeRoom(definition(local_audio_device=device), tmp_path / "room")
    service.network = SimpleNamespace(
        manifest={"processes": {}},
        interfaces=AsyncMock(return_value=[{"name": "eth0", "eligible": True}]),
        create_receiver=AsyncMock(return_value={"interface": "receiver0"}),
    )
    service._ensure_sender = AsyncMock(return_value={"api_host_ip": "10.190.1.1", "api_ip": "10.190.1.2"})
    service._account = lambda owner, role, slot=None: {"name": f"shiri-{role}-{slot}", "uid": 200, "gid": 200}
    match = re.fullmatch(r"(?:plug)?hw:CARD=USB,DEV=(\d+),SUBDEV=(\d+)", expected_pcm)
    pin = SimpleNamespace(pcm=expected_pcm, playback_node=f"/dev/snd/pcmC5D{match[1]}p",
                          manifest={"card_index": 5, "device": int(match[1]), "subdevice": int(match[2])},
                          fingerprint={"version": 1, "kind": "pci", "binding": "path",
                                       "device_path": "/devices/pci0000:00/0000:00:01.0",
                                       "driver": "/bus/pci/drivers/snd_test", "device": int(match[1]), "subdevice": int(match[2])},
                          validate=Mock(), close=Mock())
    # This test exercises admitted-pin -> generated config conversion. Separate
    # admission regressions reject raw physical CARD names before resources.
    service._resolve_local_pin = Mock(return_value=pin)
    def prepared(path, *args, **kwargs):
        path.mkdir(parents=True, exist_ok=True)
        return path
    monkeypatch.setattr("shiri.runtime.broker.private_directory", prepared)
    monkeypatch.setattr("shiri.runtime.broker.prepare_room_view", lambda *_: None)
    monkeypatch.setattr("shiri.runtime.broker.file_owner", lambda *_: None)
    fake_server = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
    monkeypatch.setattr("shiri.runtime.broker.serve_rpc", AsyncMock(return_value=fake_server))
    # Observe the real startup configuration before any process or device opens.
    service._start_namespace_services = AsyncMock(side_effect=RuntimeFailure("Configuration captured"))
    try:
        with pytest.raises(RuntimeFailure, match="Configuration captured"):
            await service._start_room(room)
        assert room.active_local_device == expected_pcm
        content = (room.directory / "config" / "owntone.conf").read_text()
        assert 'card = "shiri"' in content
        assert 'pcm_identity_file = "/run/shiri-worker/credentials/pcm-identity.json"' in content
        assert "software_volume = true" in content
        private_alsa = (room.directory / "config" / "alsa.conf").read_text()
        assert "card 5\n" in private_alsa and f"device {match[1]}\n" in private_alsa
        assert f"subdevice {match[2]}\n" in private_alsa
        assert ("type plug" in private_alsa) is expected_pcm.startswith("plughw:")
        assert json.loads((room.directory / "credentials" / "pcm-identity.json").read_text()) == pin.manifest
    finally:
        if room.signal_server:
            room.signal_server.close()
            await room.signal_server.wait_closed()
        if service.slot_locks[room.desired.slot].locked():
            service.slot_locks[room.desired.slot].release()
        await service._release_local_pin(room)


@pytest.mark.asyncio
@pytest.mark.parametrize("device", [
    "hw:0", "plughw:7,0,0", "hw:2,1,3", "plughw:CARD=2,DEV=1,SUBDEV=3",
    "hw:CARD=7", "hw:CARD=255,DEV=255,SUBDEV=255",
])
@pytest.mark.parametrize("hardware_map_available", [True, False])
async def test_startup_rejects_numeric_card_before_mapping_cleanup_or_open(
    tmp_path, device, hardware_map_available,
):
    service = broker(tmp_path)
    # Internal construction simulates stale/bypassed input independently of
    # normal Pydantic admission, which now rejects this stored intent itself.
    unsafe = definition(local_audio_device="plughw:USB").model_copy(update={"local_audio_device": device})
    room = RuntimeRoom(unsafe, tmp_path / "room")
    room.processes = {"old-backend": object()}
    service._stop_room = AsyncMock()
    service.network = SimpleNamespace(interfaces=AsyncMock(), create_receiver=AsyncMock())
    service._ensure_sender = AsyncMock()
    options = {"return_value": "DifferentDAC\n"} if hardware_map_available else {
        "side_effect": FileNotFoundError("No card map"),
    }
    with patch("shiri.runtime.broker.Path.read_text", **options) as hardware_map:
        with pytest.raises(RuntimeFailure, match="Numeric ALSA CARD.*stable named CARD.*KitchenDAC"):
            await service._start_room(room)
    hardware_map.assert_not_called()
    service._stop_room.assert_not_awaited()
    service.network.interfaces.assert_not_awaited()
    service.network.create_receiver.assert_not_awaited()
    service._ensure_sender.assert_not_awaited()
    assert room.active_local_device is None
    assert not service.slot_locks[room.desired.slot].locked()
    assert not room.directory.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("converted,direct", [
    ("plughw:CARD=USB,DEV=1", "hw:USB,1"),
    ("plughw:USB,1,0", "hw:CARD=USB,DEV=1"),
])
async def test_converted_and_direct_pcm_aliases_cannot_share_a_room_assignment(
    tmp_path, converted, direct,
):
    service = broker(tmp_path)
    first = definition(local_audio_device=converted, enabled=False)
    second = definition(
        slot=1, name="Bedroom", airplay_name="Bedroom input", local_audio_device=direct, enabled=False,
    )
    with pytest.raises(RpcError, match="local sound device cannot be shared"):
        await service.reconcile({"rooms": [first.model_dump(), second.model_dump()]})
    assert not service.rooms


@pytest.mark.asyncio
async def test_converted_playback_endpoint_keeps_the_same_exclusive_live_lease(tmp_path):
    service = broker(tmp_path)
    speaker = SpeakerRef(id="0", name="Local speaker", protocol="alsa")
    first = RuntimeRoom(definition(local_audio_device="plughw:USB,1"), tmp_path / "first")
    second = RuntimeRoom(definition(local_audio_device="hw:USB,1"), tmp_path / "second")
    first_device = service._playback_device(first.desired.local_audio_device)
    second_device = service._playback_device(second.desired.local_audio_device)
    await service._reserve_speakers(first, [speaker], first_device)
    with pytest.raises(RuntimeFailure, match="still being released"):
        await service._reserve_speakers(second, [speaker], second_device)
    assert first.held_speakers == {("local", "hw:CARD=USB,DEV=1,SUBDEV=0")}
    assert not second.held_speakers


@pytest.fixture
def preflight_environment(tmp_path, monkeypatch):
    service = broker(tmp_path)
    environment = {"version": "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event1-idle1-drain1-startupmeta1-coldmusic1-outputclock1-duck1-warm1", "shairport": "Shairport Sync 5.5.2-shiri-timed3-startup1-volume2-phone1 AirPlay2 smi10", "identities": True, "cgroup": True, "hook": True, "imports": True}
    monkeypatch.setattr("shiri.runtime.broker.sys.platform", "linux")
    monkeypatch.setattr("shiri.runtime.broker.os.geteuid", lambda: 0)
    monkeypatch.setattr("shiri.runtime.broker.shutil.which", lambda name: name)
    monkeypatch.setattr("shiri.runtime.broker.os.access", lambda *_: True)
    monkeypatch.setattr("shiri.runtime.broker.DHCP_HOOK", SimpleNamespace(is_file=lambda: environment["hook"]))
    monkeypatch.setattr(service, "binary", lambda name: name)
    monkeypatch.setattr("shiri.runtime.broker.trusted_file", lambda path, **_: path)
    is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda path: environment["cgroup"]
                        if str(path) == "/sys/fs/cgroup/cgroup.controllers" else is_file(path))
    monkeypatch.setattr("shiri.runtime.broker.os.pidfd_open", Mock(), raising=False)
    monkeypatch.setattr("shiri.runtime.broker.signal.pidfd_send_signal", Mock(), raising=False)
    def identities():
        if not environment["identities"]:
            raise RuntimeFailure("Daemon identities are unavailable")
        return SimpleNamespace()
    monkeypatch.setattr("shiri.runtime.broker.DaemonIdentities.load", lambda _: identities())

    async def command(args, **_):
        versions = {
            "nqptp": "NQPTP smi10",
            "shairport-sync": environment["shairport"],
            "avahi-daemon": "avahi-daemon 0.8-shiri-user1",
            "owntone": environment["version"],
        }
        if args[0] in versions:
            return CommandResult(args, 0, versions[args[0]])
        if not environment["imports"]:
            raise RuntimeFailure("Required native audio dependency is unavailable")
        return CommandResult(args, 0)

    service.runner.run = AsyncMock(side_effect=command)
    return service, environment


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "-shiri-timed3-startup1", "-shiri-timed3-startup1-volume1", "-shiri-timed3-startup1-volume20", "-shiri-timed3-startup1-volume2", "-shiri-timed3-startup1-volume2-phone0", "-shiri-timed3-startup1-volume2-phone10", "-shiri-timed3-startup1-volume2-phone1-other", "-shiri-timed1", "-shiri-timed2", "-shiri-timed20", "-shiri-timed2-other", "-shiri-timed2-soxr-other", "-shiri-timed30", "-shiri-timed3-other", "-shiri-timed3-startup1-soxr-other"])
async def test_receiver_requires_bounded_clock_sampling_and_recovery_before_launch(preflight_environment, suffix):
    service, environment = preflight_environment
    environment["shairport"] = f"Shairport Sync 5.5.2{suffix} AirPlay2 smi10"
    with pytest.raises(RuntimeFailure, match="bounded clock sampling.*rebuild pinned backends"):
        await service.preflight()
    assert service.versions["shairport-sync"] == environment["shairport"]
    assert "owntone" not in service.versions
    assert not any(call.args[0][1] == "-c" for call in service.runner.run.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("features", ["", "-soxr-metadata", "-soxr-convolution-metadata-mqtt-dbus-mpris"])
async def test_pinned_receiver_feature_string_passes_full_preflight(preflight_environment, features):
    service, environment = preflight_environment
    environment["shairport"] = f"5.5.2-AirPlay2-smi10-OpenSSL-Avahi-ALSA-shiri-timed3-startup1-volume2-phone1{features}-sysconfdir:/opt/shiri/etc"
    await service.preflight()
    assert service.runner.run.call_args_list[-1].args[0][1] == "-c"


@pytest.mark.asyncio
async def test_receiver_configuration_path_cannot_supply_missing_backend_marker(preflight_environment):
    service, environment = preflight_environment
    environment["shairport"] = "5.5.2-AirPlay2-smi10-ALSA-sysconfdir:/opt/-shiri-timed3-startup1-volume2-phone1"
    with pytest.raises(RuntimeFailure, match="bounded clock sampling"):
        await service.preflight()


@pytest.mark.asyncio
async def test_pinned_git_receiver_with_recovery_version_passes_full_preflight(preflight_environment, tmp_path):
    service, environment = preflight_environment
    # Preserve the captured pinned Git feature layout with the required recovery marker.
    environment["shairport"] = "7bad231-dirty-AirPlay2-smi10-OpenSSL-Avahi-ALSA-shiri-timed3-startup1-volume2-phone1-soxr-metadata-sysconfdir:/opt/shiri-v2-next9-deps/etc"
    service.config = replace(service.config, binary_dir=tmp_path / "pinned")
    manifest = service.config.binary_dir / "share" / "shiri" / "backends.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"shairport": "7bad231c18368dbd26f298577f6210e36e4b0797"}))
    await service.preflight()
    assert service.versions["shairport-sync"] == environment["shairport"]
    assert service.runner.run.call_args_list[-1].args[0][1] == "-c"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["OwnTone 29.3", "OwnTone 29.3-shiri-swvol1", "OwnTone 29.3-shiri-swvol1-timed1", "OwnTone 29.3-shiri-swvol1-timed1-source1", "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1", "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1", "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset10", "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-other", "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1", "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer10", "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-other", "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-framed10", "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1", "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-framed1", "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample10", "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-other", "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed10"])
async def test_unpatched_owntone_is_rejected_with_rebuild_instruction(preflight_environment, version):
    service, environment = preflight_environment
    environment["version"] = version
    with pytest.raises(RuntimeFailure, match="29.3-shiri-swvol1.*rebuild pinned backends"):
        await service.preflight()
    assert service.versions["owntone"] == version
    assert not any(call.args[0][1] == "-c" for call in service.runner.run.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "-framed1", "-framed1-alsa10", "-framed1-alsa1", "-framed1-alsa1-other", "-framed1-alsa1-speech10", "-framed1-alsa1-speech1", "-framed1-alsa1-speech1-other", "-framed1-alsa1-speech1-ready10", "-framed1-alsa1-speech1-ready1-other", "-framed1-alsa1-speech1-ready1", "-framed1-alsa1-speech1-ready1-anchor10", "-framed1-alsa1-speech1-ready1-anchor1-other", "-framed1-alsa1-speech1-ready1-anchor1", "-framed1-alsa1-speech1-ready1-anchor1-jitter10", "-framed1-alsa1-speech1-ready1-anchor1-jitter1-other", "-framed1-alsa1-speech1-ready1-anchor1-jitter1", "-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance10", "-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-other", "-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1", "-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition10", "-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-other", "-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1", "-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed10", "-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-other", "-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1", "-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event10", "-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event1-other"])
async def test_owntone_requires_exact_partial_write_guard_before_launch(preflight_environment, suffix):
    service, environment = preflight_environment
    environment["version"] = "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1" + suffix
    with pytest.raises(RuntimeFailure, match="partial-write preservation.*rebuild pinned backends"):
        await service.preflight()
    assert service.versions["owntone"] == environment["version"]
    assert not any(call.args[0][1] == "-c" for call in service.runner.run.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "-idle10", "-idle1-other"])
async def test_owntone_without_exact_idle_speech_backend_is_rejected_before_dependency_probe(preflight_environment, suffix):
    service, environment = preflight_environment
    environment["version"] = "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event1" + suffix
    with pytest.raises(RuntimeFailure, match="idle speech output without input refill.*rebuild pinned backends"):
        await service.preflight()
    assert service.versions["owntone"] == environment["version"]
    assert not any(call.args[0][1] == "-c" for call in service.runner.run.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "-drain1", "-drain10-startupmeta1", "-drain1-startupmeta10", "-drain1-startupmeta1-other"])
async def test_speech_startup_requires_exact_drain_and_metadata_contract_before_launch(preflight_environment, suffix):
    service, environment = preflight_environment
    environment["version"] = "OwnTone 29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1-anchor1-jitter1-owner1-balance1-transition1-bed1-event1-idle1" + suffix
    with pytest.raises(RuntimeFailure, match="natural speech drain and acknowledged startup metadata.*rebuild pinned backends"):
        await service.preflight()
    assert service.versions["owntone"] == environment["version"]
    assert not any(call.args[0][1] == "-c" for call in service.runner.run.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "-coldmusic0", "-coldmusic10", "-coldmusic1-other"])
async def test_timed_music_backend_without_exact_empty_input_recovery_is_rejected(preflight_environment, suffix):
    service, environment = preflight_environment
    environment["version"] = (environment["version"].removesuffix("-coldmusic1-outputclock1-duck1-warm1")
                              + suffix + "-outputclock1-duck1-warm1")
    with pytest.raises(RuntimeFailure, match="timed music input without legacy refill.*rebuild pinned backends"):
        await service.preflight()
    assert not any(call.args[0][1] == "-c" for call in service.runner.run.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", " (built for Shiri)"])
async def test_patched_owntone_passes_full_preflight(preflight_environment, suffix):
    service, environment = preflight_environment
    environment["version"] += suffix
    await service.preflight()
    probe = service.runner.run.call_args_list[-1].args[0]
    assert probe[1] == "-c"
    assert all(f"import {module}" in probe[2] for module in ["aiortc", "av", "numpy"])
    assert "gi" not in probe[2] and "Gst" not in probe[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,error", [
    ("hook", False, "private namespace DHCP hook"),
    ("identities", False, "Daemon identities are unavailable"),
    ("cgroup", False, "requires unified cgroup v2"),
    ("imports", False, "native audio dependency is unavailable"),
])
async def test_patched_marker_does_not_bypass_later_runtime_requirements(
    preflight_environment, field, value, error,
):
    service, environment = preflight_environment
    environment[field] = value
    with pytest.raises(RuntimeFailure, match=error):
        await service.preflight()


async def wait_until(predicate):
    async def wait():
        while not predicate():  # noqa: ASYNC110 - observe the actual asynchronous state transition.
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), timeout=2)


def process_record():
    executable = "/opt/shiri/venv/bin/python"
    return {
        "name": "audio", "pid": 44, "pgid": 44, "birth": "100", "boot_id": "boot-A",
        "executable": executable, "argv": [executable, "-m", "shiri.runtime.audio"],
        "log_path": "/var/lib/shiri-runtime/room/audio.log",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["boot_id", "executable", "argv", "missing_boot_id", "missing_argv"])
async def test_saved_identity_mismatch_never_signals_a_process_group(mismatch):
    saved = process_record()
    current_boot, current_executable, current_args = saved["boot_id"], saved["executable"], saved["argv"]
    if mismatch == "boot_id":
        current_boot = "boot-B"
    elif mismatch == "executable":
        current_executable = "/usr/bin/unrelated-service"
    elif mismatch == "argv":
        current_args = [saved["executable"], "-m", "unrelated.module"]
    else:
        saved.pop(mismatch.removeprefix("missing_"))
    with patch("shiri.runtime.system.boot_id", return_value=current_boot), \
         patch("shiri.runtime.system.process_birth", side_effect=["100", None]), \
         patch("shiri.runtime.system.process_executable", return_value=current_executable), \
         patch("shiri.runtime.system.process_args", return_value=current_args), \
         patch("shiri.runtime.system.os.getpgid", return_value=44), \
         patch("shiri.runtime.system.os.killpg") as signal_group:
        await Runner().stop_saved(saved)
    signal_group.assert_not_called()


@pytest.mark.asyncio
async def test_verified_saved_process_can_be_stopped_without_touching_another_group():
    saved = process_record()
    with patch("shiri.runtime.system.boot_id", return_value=saved["boot_id"]), \
         patch("shiri.runtime.system.process_birth", side_effect=["100", None]), \
         patch("shiri.runtime.system.process_executable", return_value=saved["executable"]), \
         patch("shiri.runtime.system.process_args", return_value=saved["argv"]), \
         patch("shiri.runtime.system.os.getpgid", return_value=44), \
         patch("shiri.runtime.system.os.killpg") as signal_group:
        await Runner().stop_saved(saved)
    assert signal_group.call_count == 1
    assert signal_group.call_args.args[0] == 44


@pytest.mark.parametrize("malformed", [
    {},
    {"version": 1, "networks": {"sender": {"namespace": "still-owned"}}, "processes": {}},
    {"version": 1, "installation_id": str(uuid4()), "networks": [], "processes": {}},
    {"version": 1, "installation_id": str(uuid4()), "networks": {}, "processes": []},
    {"version": 1, "installation_id": str(uuid4()), "networks": {"sender": {"namespace": "foreign"}}, "processes": {}},
])
def test_existing_malformed_manifest_preserves_bytes_and_refuses_recovery(tmp_path, malformed):
    source = tmp_path / "ownership.json"
    original = json.dumps(malformed, indent=2).encode()
    source.write_bytes(original)
    runner = SimpleNamespace(run=AsyncMock(), json=AsyncMock(), stop_saved=AsyncMock())
    with pytest.raises(RuntimeFailure):
        NetworkManager(tmp_path, runner)
    assert source.read_bytes() == original
    runner.run.assert_not_called()
    runner.json.assert_not_called()
    runner.stop_saved.assert_not_called()


class SelectionBackend:
    def __init__(self, speaker):
        self.speaker = speaker
        self.selected = []
        self.selection_history = []
        self.block_release = False
        self.release_entered = asyncio.Event()
        self.release_allowed = asyncio.Event()
        self.master_volume = 23
        self.volume_error = None
        self.stable_master = False

    async def outputs(self, exclusions):
        return [{
            "id": self.speaker.id, "name": self.speaker.name, "protocol": self.speaker.protocol,
            "assignable": True, "selected": self.speaker.id in self.selected, "offset_ms": 0,
        }]

    async def select(self, speakers, outputs):
        ids = [speaker.id for speaker in speakers]
        if not ids and self.block_release:
            self.release_entered.set()
            await self.release_allowed.wait()
        self.selected = ids
        self.selection_history.append(ids)
        if not self.stable_master:
            self.master_volume = 23  # Unmanaged OwnTone restores persisted device volume.
        return {"ok": True}

    async def volume(self, value):
        if self.volume_error:
            raise self.volume_error
        self.master_volume = value
        return {"ok": True}

    async def volume_settings(self, value, speakers):
        result = await self.volume(value)
        self.stable_master = True
        return result

    async def request(self, method, path, *, json=None):
        assert method == "PUT" and path == "/api/outputs/set"
        self.selected = json["outputs"]
        self.selection_history.append(self.selected)
        return {"ok": True}


@pytest.mark.asyncio
async def test_zone_volume_survives_persisted_device_volume_on_start_restore_and_selection(tmp_path):
    service = broker(tmp_path)
    speaker = SpeakerRef(id="123", name="Physical speaker", protocol="airplay2")
    desired = definition(speakers=[speaker], volume=77)
    state = RuntimeRoom(desired, tmp_path/desired.id, current_volume=77,
                        timing=(500, 600), active_timing=(500, 600))
    state.client = SelectionBackend(speaker)
    service.rooms[desired.id] = state
    await service._restore_outputs(state)
    assert state.client.selected == ['123'] and state.client.master_volume == 77
    state.current_volume = 61
    await service.set_outputs(state, [speaker])
    assert state.client.master_volume == 61 and state.current_volume == 61
    await service._restore_outputs(state)
    assert state.client.master_volume == 61


@pytest.mark.asyncio
async def test_failed_gain_staging_before_selection_retains_output_lease_until_reconciliation(tmp_path):
    service = broker(tmp_path)
    speaker = SpeakerRef(id="123", name="Physical speaker", protocol="airplay2")
    desired = definition()
    state = RuntimeRoom(desired, tmp_path/desired.id, status='running', current_volume=77,
                        timing=(500, 600), active_timing=(500, 600))
    state.client = SelectionBackend(speaker)
    state.client.volume_error = RuntimeFailure('Volume not acknowledged')
    service.rooms[desired.id] = state
    with pytest.raises(RuntimeFailure, match='Volume not acknowledged'):
        await service.set_outputs(state, [speaker])
    assert state.status == 'degraded' and state.wake.is_set()
    assert state.client.selected == [] and state.desired.speakers == []
    assert service.speaker_leases[('owntone', '123')] == desired.id


@pytest.mark.asyncio
async def test_live_speaker_handoff_waits_for_old_backend_acknowledgment(tmp_path):
    service = broker(tmp_path)
    speaker = SpeakerRef(id="123", name="Physical speaker", protocol="airplay2")
    old_definition = definition(speakers=[speaker])
    new_definition = definition(slot=1, name="Bedroom", airplay_name="Bedroom input")
    old = RuntimeRoom(old_definition, tmp_path / old_definition.id)
    new = RuntimeRoom(new_definition, tmp_path / new_definition.id)
    old.client, new.client = SelectionBackend(speaker), SelectionBackend(speaker)
    service.rooms = {old_definition.id: old, new_definition.id: new}
    await service._restore_outputs(old)
    assert old.client.selected == [speaker.id]
    old.desired = old.desired.model_copy(update={"speakers": []})
    new.desired = new.desired.model_copy(update={"speakers": [speaker]})
    old.client.block_release = True
    releasing = asyncio.create_task(service._restore_outputs(old))
    claiming = None
    try:
        await asyncio.wait_for(old.client.release_entered.wait(), 1)
        claiming = asyncio.create_task(service._restore_outputs(new))
        await asyncio.sleep(0.03)
        assert new.client.selected == [], "A second sender claimed a speaker before its old sender released it"
        assert old.client.selected == [speaker.id]
        old.client.release_allowed.set()
        await asyncio.wait_for(releasing, 1)
        await asyncio.wait_for(claiming, 1)
        # A lease conflict may fail immediately and require the normal retry.
        if new.client.selected != [speaker.id]:
            await service._restore_outputs(new)
        assert old.client.selected == [] and new.client.selected == [speaker.id]
    finally:
        old.client.release_allowed.set()
        for task in (releasing, claiming):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (releasing, claiming) if t is not None), return_exceptions=True)


@pytest.mark.asyncio
async def test_disabled_room_retries_failed_cleanup_and_releases_its_slot(tmp_path):
    service = broker(tmp_path)
    service.ready = True
    state = RuntimeRoom(definition(enabled=False), tmp_path / "room")
    state.receiver = {"namespace": "owned-room"}
    state.reserved_slot = state.desired.slot
    await service.slot_locks[state.desired.slot].acquire()
    service.rooms[state.desired.id] = state
    service.sender_users.add(state.desired.id)
    attempts = 0
    async def remove(key):
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise RuntimeFailure("temporary namespace cleanup failure")
    service.network = SimpleNamespace(remove=remove, manifest={"processes": {}})
    service._stop_sender = AsyncMock()
    state.wake.set()
    state.task = asyncio.create_task(service._room_loop(state))
    try:
        await wait_until(lambda: state.status == "error" and attempts == 2)
        assert state.receiver is not None and service.slot_locks[state.desired.slot].locked()
        state.retry_at = asyncio.get_running_loop().time() - 1
        await service._probe_room(state)
        await wait_until(lambda: state.status == "stopped")
        assert attempts == 3
        assert state.receiver is None and state.reserved_slot is None
        assert not service.slot_locks[state.desired.slot].locked()
        assert state.desired.id not in service.sender_users
    finally:
        service._closing = True
        state.task.cancel()
        await asyncio.gather(state.task, return_exceptions=True)
        if state.receiver is not None:
            await service._stop_room(state)


class FaultKernel(NetworkManager):
    """Kernel state where NEWLINK ignores alias and creation can fail afterward."""
    def __init__(self, directory):
        self.namespaces = set()
        self.host_links = {}
        self.failed = False
        self.commands = []
        super().__init__(directory, SimpleNamespace(run=self.run))

    async def namespace_exists(self, name):
        return name in self.namespaces

    def namespace_inode(self, name):
        return 100 if name in self.namespaces else None

    async def namespace_pids(self, name):
        return []

    async def link(self, namespace, name):
        if namespace is None:
            if name == "eth0":
                return {"ifindex": 7}
            return self.host_links.get(name, {})
        return {}

    async def run(self, args, **kwargs):
        self.commands.append(args.copy())
        if args[:3] == ["ip", "netns", "add"]:
            self.namespaces.add(args[3])
        elif args[:3] == ["ip", "netns", "delete"]:
            self.namespaces.discard(args[3])
        elif args[:3] == ["ip", "link", "add"]:
            name = args[3]
            self.host_links[name] = {
                "ifname": name, "ifindex": 1000, "link_index": 7,
                "address": args[args.index("address") + 1], "linkinfo": {"info_kind": "macvlan"},
            }
            # The guest's actual iproute2/kernel ignores alias on NEWLINK.
            # Only a later SET may attach it; a fault leaves this link untagged.
            if not self.failed:
                self.failed = True
                raise RuntimeFailure("injected failure after interface creation")
        elif args[:3] == ["ip", "link", "delete"]:
            self.host_links.pop(args[3], None)
        elif args[:3] == ["ip", "link", "set"] and "alias" in args:
            self.host_links[args[3]]["ifalias"] = args[args.index("alias") + 1]
        return CommandResult(args, 0)


@pytest.mark.asyncio
async def test_post_create_fault_cleans_untagged_macvlan_with_its_reserved_identity(tmp_path):
    with patch("shiri.runtime.network.boot_id", return_value="boot-A"):
        kernel = FaultKernel(tmp_path)
        room = definition()
        with pytest.raises(RuntimeFailure, match="injected failure after interface creation"):
            await kernel.create_receiver(room)
    assert kernel.failed, "The test must reach interface creation, rather than fail preflight"
    assert not kernel.host_links
    assert not kernel.namespaces
    assert any(args[:3] == ["ip", "link", "delete"] for args in kernel.commands)
    key = f"receiver:{room.id}"
    saved = json.loads((tmp_path / "ownership.json").read_text())
    assert key not in saved["networks"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["mac", "parent", "alias", "kind", "boot", "inode"])
async def test_untagged_creation_recovery_never_deletes_substituted_host_link(tmp_path, mismatch):
    with patch("shiri.runtime.network.boot_id", return_value="boot-A"):
        kernel = FaultKernel(tmp_path)
        room = definition()
        key = f"receiver:{room.id}"
        record = kernel.new_record(key, "eth0", room_id=room.id)
        record.update(inode=100, parent_ifindex=7)
        kernel.manifest["networks"][key] = record
        kernel.namespaces.add(record["namespace"])
        link = {
            "ifname": record["interface"], "ifindex": 1000, "link_index": 7,
            "address": record["mac"], "linkinfo": {"info_kind": "macvlan"},
        }
        if mismatch == "mac":
            link["address"] = "02:00:00:00:00:00"
        elif mismatch == "parent":
            link["link_index"] = 8
        elif mismatch == "alias":
            link["ifalias"] = "some-other-installation:owned"
        elif mismatch == "kind":
            link["linkinfo"]["info_kind"] = "veth"
        elif mismatch == "boot":
            record["boot_id"] = "boot-before-reboot"
        elif mismatch == "inode":
            record["inode"] = 101
        kernel.host_links[record["interface"]] = link
        kernel.save()
        with pytest.raises(RuntimeFailure):
            await kernel.remove(key)
    assert kernel.host_links[record["interface"]] == link
    assert not any(args[:3] == ["ip", "link", "delete"] for args in kernel.commands)
    assert key in kernel.manifest["networks"]
    saved = json.loads((tmp_path / "ownership.json").read_text())
    assert saved["networks"][key] == record


@pytest.mark.asyncio
async def test_continuously_trickling_backend_response_has_a_total_deadline():
    class Trickle(httpx.AsyncByteStream):
        def __init__(self):
            self.chunks = 0
            self.closed = False

        async def __aiter__(self):
            # Every interval is well below the HTTP read inactivity timeout.
            # A size cap also cannot help: this sends only a few bytes per tick.
            for _ in range(100):
                await asyncio.sleep(0.002)
                self.chunks += 1
                yield b" "

        async def aclose(self):
            self.closed = True

    stream = Trickle()
    async def response(request):
        return httpx.Response(200, stream=stream)
    client = OwnToneClient("http://private-backend", transport=httpx.MockTransport(response), deadline=0.03)
    try:
        with pytest.raises(RuntimeFailure, match="deadline"):
            await asyncio.wait_for(client.request("GET", "/api/player"), timeout=0.25)
        assert 1 < stream.chunks < 100
        assert stream.closed, "Timed-out HTTP exchanges must release their connection/stream"
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "-outputclock0", "-outputclock10", "-outputclock1-other"])
async def test_missing_exact_output_clock_backend_is_rejected_before_launch(preflight_environment, suffix):
    service, environment = preflight_environment
    environment["version"] = (environment["version"].removesuffix("-outputclock1-duck1-warm1")
                              + suffix + "-duck1-warm1")
    with pytest.raises(RuntimeFailure, match="stable identity speaker clock selection.*rebuild pinned backends"):
        await service.preflight()
    assert not any(call.args[0][1] == "-c" for call in service.runner.run.call_args_list)
