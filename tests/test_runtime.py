"""Runtime contracts: no test signals live PIDs or touches actual Linux links."""

import asyncio
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx
import pytest

from shiri.domain import Room, SpeakerRef
from shiri.rpc import RpcError, call_rpc, serve_rpc
from shiri.settings import Settings
from shiri.runtime import dhclient_hook
from shiri.runtime.backend import OwnToneClient, normalize_output
from shiri.runtime.broker import Broker, RuntimeRoom, Superseded
from shiri.runtime.configuration import backend_configs
from shiri.runtime.network import NetworkManager, stable_mac
from shiri.runtime.system import OwnedProcess, Runner, RuntimeFailure, process_args, process_executable


def room(**changes):
    data = {
        "id": str(uuid4()),
        "slot": 0,
        "name": "Kitchen",
        "airplay_name": "Kitchen input",
        "interface": "eth0",
    }
    data.update(changes)
    return Room.model_validate(data)


def broker(tmp_path):
    return Broker(
        Settings(
            state_dir=tmp_path / "api",
            runtime_state_dir=tmp_path / "runtime",
            runtime_dir=tmp_path / "run",
            runtime_socket=tmp_path / "run" / "runtime.sock",
        )
    )


def runtime_room(tmp_path, definition=None):
    definition = definition or room()
    return RuntimeRoom(definition, tmp_path / definition.id)


async def until(predicate):
    for _ in range(50):
        if predicate():
            return
        await asyncio.sleep(0)
    assert predicate()


def test_mac_identity_is_stable_unique_and_locally_administered():
    installation = str(uuid4())
    assert stable_mac(installation, "sender") == stable_mac(installation, "sender")
    assert stable_mac(installation, "sender") != stable_mac(installation, "receiver:room")
    assert stable_mac(installation, "sender").startswith("02:")
    assert stable_mac(installation, "sender") != stable_mac(str(uuid4()), "sender")


def test_service_allows_owned_network_setup_without_proc_ptrace_access():
    unit = (Path(__file__).parents[1] / "deploy" / "shiri-runtime.service").read_text()
    settings = dict(line.split("=", 1) for line in unit.splitlines() if "=" in line)
    capabilities = set(settings["CapabilityBoundingSet"].split())
    assert {"CAP_NET_ADMIN", "CAP_CHOWN"} <= capabilities
    assert "CAP_SYS_PTRACE" not in capabilities
    assert "AF_PACKET" in settings["RestrictAddressFamilies"].split()
    assert settings["User"] == "root" and settings["Group"] == "root"
    assert settings["PrivateMounts"] == "no"
    mount_sandbox_settings = {
        "ProtectHome",
        "ProtectKernelTunables",
        "ProtectKernelLogs",
        "ProtectControlGroups",
        "ProtectSystem",
        "ReadWritePaths",
        "ReadOnlyPaths",
        "InaccessiblePaths",
        "BindPaths",
        "BindReadOnlyPaths",
        "PrivateTmp",
        "PrivateDevices",
        "RootDirectory",
        "RootImage",
    }
    assert not mount_sandbox_settings.intersection(settings), (
        "Namespace bind mounts must survive broker replacement"
    )


@pytest.mark.asyncio
async def test_control_socket_group_and_peer_policy_are_explicit_for_root_group_broker(tmp_path):
    service = broker(tmp_path)
    service.config.runtime_dir.mkdir(parents=True)
    service.preflight = AsyncMock(side_effect=RuntimeFailure("Test preflight unavailable"))
    server = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
    with (
        patch("shiri.runtime.broker.sys.platform", "linux"),
        patch("shiri.runtime.broker.os.geteuid", return_value=0),
        patch("shiri.runtime.broker.os.fstat", return_value=SimpleNamespace(st_uid=0)),
        patch("shiri.runtime.broker.root_directory") as directories,
        patch("shiri.runtime.broker.os.chown") as ownership,
        patch("shiri.runtime.broker.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=123, pw_gid=998)),
        patch("shiri.runtime.broker.serve_rpc", new=AsyncMock(return_value=server)) as serve,
    ):
        try:
            snapshot = await service.start()
            assert not snapshot["ready"]
            directories.assert_any_call(service.config.runtime_dir, 0o750)
            ownership.assert_called_once_with(service.config.runtime_dir, 0, 998)
            assert serve.await_args.kwargs == {"allowed_uids": {0, 123}, "socket_gid": 998}
        finally:
            await service.close()


@pytest.mark.asyncio
async def test_rpc_sets_requested_group_without_weakening_private_worker_socket_mode():
    async def handler(*_):
        return {"ready": True}

    with TemporaryDirectory(prefix="shiri-control-group-", dir="/tmp") as directory:
        path = Path(directory) / "worker.sock"
        server = await serve_rpc(path, handler, socket_gid=os.getegid(), mode=0o600)
        try:
            assert path.stat().st_gid == os.getegid()
            assert path.stat().st_mode & 0o777 == 0o600
            assert await call_rpc(path, "health") == {"ready": True}
        finally:
            server.close()
            await server.wait_closed()


def test_corrupt_ownership_state_does_not_replace_installation_identity(tmp_path):
    (tmp_path / "ownership.json").write_text("invalid-json")
    with pytest.raises(RuntimeFailure, match="Cannot read runtime ownership"):
        NetworkManager(tmp_path, Mock())
    assert (tmp_path / "ownership.json").read_text() == "invalid-json"


@pytest.mark.asyncio
async def test_namespace_inode_change_blocks_destructive_cleanup(tmp_path):
    manager = NetworkManager(tmp_path, SimpleNamespace(run=AsyncMock()))
    record = manager.new_record("sender", "eth0")
    record["inode"] = 100
    manager.manifest["networks"]["sender"] = record
    manager.namespace_exists = AsyncMock(return_value=True)
    manager.namespace_inode = Mock(return_value=200)
    with pytest.raises(RuntimeFailure, match="identity changed"):
        await manager.remove("sender")
    assert "sender" in manager.manifest["networks"]
    manager.runner.run.assert_not_called()


@pytest.mark.asyncio
async def test_dhcp_release_never_uses_ipvlan_shared_host_mac(tmp_path):
    manager = NetworkManager(tmp_path, SimpleNamespace(run=AsyncMock()))
    record = manager.new_record("sender", "eth0")
    manager.owned_namespace = AsyncMock(return_value=True)
    manager.link = AsyncMock(
        return_value={
            "address": record["mac"],
            "ifalias": record["alias"],
            "linkinfo": {"info_kind": "ipvlan"},
        }
    )
    assert not await manager.release_dhcp(record)
    manager.runner.run.assert_not_called()


@pytest.mark.asyncio
async def test_dhcp_release_rejects_stale_host_pidfile(tmp_path):
    manager = NetworkManager(tmp_path / "state", SimpleNamespace(run=AsyncMock()))
    record = manager.new_record("sender", "eth0")
    lease, pid = tmp_path / "lease", tmp_path / "pid"
    lease.write_text("lease")
    pid.write_text("44")
    record.update(lease_file=str(lease), pid_file=str(pid))
    manager.owned_namespace = AsyncMock(return_value=True)
    manager.owned_macvlan = AsyncMock(return_value=True)
    manager.namespace_pids = AsyncMock(return_value=[44])
    with (
        patch("shiri.runtime.network.process_birth", return_value="100"),
        patch("shiri.runtime.network.process_args", return_value=["dhclient", "-pf", "/run/host.pid"]),
    ):
        assert not await manager.release_dhcp(record)
    manager.runner.run.assert_not_called()


@pytest.mark.asyncio
async def test_receiver_partial_startup_rolls_back_only_its_identity(tmp_path):
    manager = NetworkManager(tmp_path, Mock())
    definition = room()
    manager._create_lan = AsyncMock(side_effect=RuntimeFailure("DHCP failure"))
    manager.remove = AsyncMock()
    with pytest.raises(RuntimeFailure, match="DHCP failure"):
        await manager.create_receiver(definition)
    manager.remove.assert_awaited_once_with(f"receiver:{definition.id}")


@pytest.mark.asyncio
async def test_saved_process_birth_prevents_reused_pid_signal():
    with (
        patch("shiri.runtime.system.process_birth", return_value="200"),
        patch("shiri.runtime.system.os.killpg") as kill,
    ):
        await Runner().stop_saved({"pid": 44, "pgid": 44, "birth": "100"})
    kill.assert_not_called()


@pytest.mark.asyncio
async def test_stopping_one_room_preserves_other_sender_reservation(tmp_path):
    service = broker(tmp_path)
    service.network = SimpleNamespace(remove=AsyncMock())
    service._stop_sender = AsyncMock()
    first, second = runtime_room(tmp_path), runtime_room(tmp_path)
    service.sender_users = {first.desired.id, second.desired.id}
    await service._stop_room(first)
    service._stop_sender.assert_not_called()
    await service._stop_room(second)
    service._stop_sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_failure_keeps_error_status_after_cleanup_and_backs_off(tmp_path):
    service = broker(tmp_path)
    service.ready = True
    state = runtime_room(tmp_path, room(enabled=True))
    service._start_room = AsyncMock(side_effect=RuntimeFailure("gateway unreachable"))

    async def cleanup(current):
        current.status = "stopping"

    service._stop_room = AsyncMock(side_effect=cleanup)
    state.wake.set()
    task = asyncio.create_task(service._room_loop(state))
    await until(lambda: state.status == "error")
    assert state.error == "gateway unreachable"
    assert state.retry_at > asyncio.get_running_loop().time()
    assert state.failures == 1
    service._closing = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_startup_superseded_by_stop_rolls_back_without_running(tmp_path):
    service = broker(tmp_path)
    service.ready = True
    state = runtime_room(tmp_path, room(enabled=True))

    async def startup(current):
        current.desired = current.desired.model_copy(update={"enabled": False})
        raise Superseded()

    service._start_room = AsyncMock(side_effect=startup)
    service._stop_room = AsyncMock()
    state.wake.set()
    task = asyncio.create_task(service._room_loop(state))
    await until(lambda: service._stop_room.await_count >= 2)
    assert state.status == "stopped"
    assert state.error is None
    service._closing = True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_identical_reconcile_does_not_reapply_outputs_or_bypass_backoff(tmp_path):
    service = broker(tmp_path)
    service.ready = True
    definition = room(enabled=False)
    await service.reconcile({"rooms": [definition.model_dump()]})
    await asyncio.sleep(0)
    state = service.rooms[definition.id]
    state.wake.set = Mock(wraps=state.wake.set)
    await service.reconcile({"rooms": [definition.model_dump()]})
    state.wake.set.assert_not_called()
    await service.close()


@pytest.mark.asyncio
async def test_reconcile_rejects_same_speaker_across_rooms_before_starting(tmp_path):
    service = broker(tmp_path)
    speaker = SpeakerRef(id="123", name="Speaker", protocol="airplay2")
    first, second = (
        room(speakers=[speaker]),
        room(slot=1, airplay_name="Other room input", speakers=[speaker]),
    )
    with pytest.raises(RpcError, match="multiple rooms"):
        await service.reconcile({"rooms": [first.model_dump(), second.model_dump()]})
    assert service.rooms == {}


@pytest.mark.asyncio
async def test_phone_volume_can_be_saved_before_owntone_is_ready(tmp_path):
    service = broker(tmp_path)
    state = runtime_room(tmp_path)
    assert await service.set_volume(state, 0, pending=True) == {"ok": True, "volume": 0, "pending": True}
    update = json.loads((state.directory / "phone-volume.json").read_text())
    assert update["volume"] == 0 and update["base_revision"] == state.desired.revision
    with pytest.raises(RpcError, match="integer"):
        await service.set_volume(state, True, pending=True)


@pytest.mark.asyncio
async def test_closed_session_is_idempotent_after_room_has_stopped(tmp_path):
    service = broker(tmp_path)
    state = runtime_room(tmp_path)
    service.rooms[state.desired.id] = state
    result = await service.dispatch(
        "speech",
        {
            "room_id": state.desired.id,
            "session_id": "known-session",
            "request_id": "close-request",
            "action": "close",
        },
    )
    assert result == {"ok": True, "closed": True}


def test_dhcp_hook_refuses_host_namespace_and_bad_masks():
    with (
        patch.object(dhclient_hook.os, "readlink", return_value="net:[1]"),
        patch.dict(dhclient_hook.os.environ, {"SHIRI_HOST_NETNS": "net:[1]"}, clear=True),
        patch.object(dhclient_hook, "command") as command,
    ):
        with pytest.raises(RuntimeError, match="separate network namespace"):
            dhclient_hook.main()
        command.assert_not_called()
    environment = {
        "SHIRI_HOST_NETNS": "net:[1]",
        "interface": "sr123",
        "reason": "BOUND",
        "new_ip_address": "192.168.1.2",
        "new_subnet_mask": "255.0.255.0",
    }
    with (
        patch.object(dhclient_hook.os, "readlink", return_value="net:[2]"),
        patch.dict(dhclient_hook.os.environ, environment, clear=True),
        patch.object(dhclient_hook, "command") as command,
    ):
        with pytest.raises(ValueError):
            dhclient_hook.main()
        command.assert_not_called()


@pytest.mark.parametrize("host_netns", [None, "", "net:[0]", "net:[1]\n", "mnt:[1]", "net:[-1]"])
def test_dhcp_hook_refuses_missing_or_malformed_host_identity(host_netns):
    environment = {} if host_netns is None else {"SHIRI_HOST_NETNS": host_netns}
    with (
        patch.dict(dhclient_hook.os.environ, environment, clear=True),
        patch.object(dhclient_hook.os, "readlink") as readlink,
        patch.object(dhclient_hook, "command") as command,
    ):
        with pytest.raises(RuntimeError, match="host network namespace identity"):
            dhclient_hook.main()
        readlink.assert_not_called()
        command.assert_not_called()


def test_dhcp_hook_separate_namespace_requires_no_access_to_pid_one():
    environment = {
        "SHIRI_HOST_NETNS": "net:[1]",
        "interface": "sr123",
        "reason": "BOUND",
        "new_ip_address": "192.168.1.2",
        "new_subnet_mask": "255.255.255.0",
    }
    with (
        patch.dict(dhclient_hook.os.environ, environment, clear=True),
        patch.object(dhclient_hook.os, "readlink", return_value="net:[2]") as readlink,
        patch.object(dhclient_hook, "command") as command,
    ):
        dhclient_hook.main()
        readlink.assert_called_once_with("/proc/self/ns/net")
        assert any(
            call.args[:4] == ("-4", "addr", "replace", "192.168.1.2/24") for call in command.call_args_list
        )


@pytest.mark.asyncio
async def test_dhcp_acquire_and_release_pass_captured_host_namespace_identity(tmp_path):
    manager = NetworkManager(
        tmp_path / "state", SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(returncode=0)))
    )
    manager.host_netns = "net:[101]"
    record = manager.new_record("sender", "eth0")
    lease, pidfile, config = tmp_path / "lease", tmp_path / "pid", tmp_path / "config"
    lease.write_text("lease")
    record.update(lease_file=str(lease), pid_file=str(pidfile), dhcp_config=str(config))
    manager._prepare_dhcp_files = Mock(return_value=(lease, pidfile, config))
    manager.ipv4 = AsyncMock(return_value="192.168.1.2")
    manager.preflight_gateway = AsyncMock()
    manager.owned_namespace = AsyncMock(return_value=True)
    manager.owned_macvlan = AsyncMock(return_value=True)
    with (
        patch("shiri.runtime.network.DHCP_HOOK", lease),
        patch("shiri.runtime.network.os.access", return_value=True),
    ):
        assert await manager.acquire_dhcp(record) == "192.168.1.2"
        assert await manager.release_dhcp(record)
    assert manager.runner.run.await_count == 2
    for call in manager.runner.run.await_args_list:
        args = call.args[0]
        assert args[args.index("-e") + 1] == "SHIRI_HOST_NETNS=net:[101]"


def test_configuration_uses_modern_fixed_audio_parameters_and_activity_hooks(tmp_path):
    definition = room(airplay_name='Kitchen "speaker"')
    receiver = {"interface": "sr123"}
    sender = {"api_host_ip": "10.190.1.1", "api_ip": "10.190.1.2"}
    airplay, own = backend_configs(
        definition,
        tmp_path,
        receiver,
        sender,
        broker_socket=tmp_path / "broker.sock",
        all_receiver_names=[definition.airplay_name],
        password="private-secret",
    )
    text = airplay.read_text()
    assert "output_rate = 48000;" in text and "output_channels = 2;" in text
    assert 'output_format = "S16_LE";' in text
    assert "music-start" in text and "music-stop" in text
    assert "audio_backend_latency_offset_in_seconds = 0.0;" in text
    assert 'Kitchen \\"speaker\\"' in text
    assert 'type = "disabled"' in own.read_text()
    assert "trusted_networks" in own.read_text()
    assert 'enabled = "yes";' in text and 'include_cover_art = "no";' in text
    assert "shairport.pipe" in text and "pipe_timeout = 100;" in text
    assert [pipe.name for pipe in (tmp_path / "pipes").iterdir()] == ["audio.pipe"]


@pytest.mark.asyncio
async def test_http_204_is_acknowledged_but_rejected_mutation_is_not():
    async def responder(request):
        return httpx.Response(204 if request.url.path.endswith("/volume") else 503)

    client = OwnToneClient("http://private", transport=httpx.MockTransport(responder))
    assert await client.volume(50) == {"ok": True}
    with pytest.raises(RuntimeFailure, match="HTTP 503"):
        await client.request("PUT", "/api/outputs/set", json={"outputs": []})
    await client.close()


def backend_transport(*, changed=True, bad_readback=False, reject_resume=False):
    state = {"player": "play", "offset": 0, "selected": False, "requests": []}

    async def responder(request):
        path, method = request.url.path, request.method
        state["requests"].append((method, path))
        if path == "/api/player":
            return httpx.Response(200, json={"state": state["player"]})
        if path == "/api/player/pause":
            state["player"] = "pause"
            return httpx.Response(204)
        if path == "/api/player/play":
            state["player"] = "play"
            return httpx.Response(503 if reject_resume else 204)
        if path == "/api/outputs/123":
            if method == "PUT":
                state["offset"] = json.loads(request.content)["offset_ms"]
                return httpx.Response(204)
            return httpx.Response(200, json={"offset_ms": 0 if bad_readback else state["offset"]})
        if path == "/api/outputs/set":
            state["selected"] = "123" in json.loads(request.content)["outputs"]
            return httpx.Response(204)
        if path == "/api/outputs":
            return httpx.Response(
                200,
                json={
                    "outputs": [
                        {
                            "id": "123",
                            "name": "Speaker",
                            "type": "AirPlay 2",
                            "selected": state["selected"],
                            "offset_ms": state["offset"],
                        }
                    ]
                },
            )
        raise AssertionError((method, path))

    return state, httpx.MockTransport(responder)


@pytest.mark.asyncio
async def test_active_calibration_pauses_sets_verifies_selects_and_resumes():
    state, transport = backend_transport()
    client = OwnToneClient("http://private", transport=transport)
    speaker = SpeakerRef(id="123", name="Speaker", protocol="airplay2", offset_ms=125)
    outputs = [normalize_output({"id": "123", "name": "Speaker", "type": "AirPlay 2", "offset_ms": 0}, set())]
    result = await client.select([speaker], outputs)
    assert result == {"ok": True, "playback_restarted": True, "offsets_applied": True}
    assert state["requests"] == [
        ("GET", "/api/player"),
        ("PUT", "/api/player/pause"),
        ("GET", "/api/player"),
        ("PUT", "/api/outputs/123"),
        ("GET", "/api/outputs/123"),
        ("PUT", "/api/outputs/set"),
        ("GET", "/api/outputs"),
        ("PUT", "/api/player/play"),
    ]
    await client.close()


@pytest.mark.asyncio
async def test_failed_calibration_resumes_prior_playback_without_claiming_applied():
    state, transport = backend_transport(bad_readback=True)
    client = OwnToneClient("http://private", transport=transport)
    speaker = SpeakerRef(id="123", name="Speaker", protocol="airplay2", offset_ms=125)
    outputs = [normalize_output({"id": "123", "name": "Speaker", "type": "AirPlay 2", "offset_ms": 0}, set())]
    with pytest.raises(RuntimeFailure, match="did not retain"):
        await client.select([speaker], outputs)
    assert state["requests"][-1] == ("PUT", "/api/player/play")
    assert ("PUT", "/api/outputs/set") not in state["requests"]
    assert state["player"] == "play"
    await client.close()


@pytest.mark.asyncio
async def test_same_offset_does_not_interrupt_playback():
    state, transport = backend_transport()
    client = OwnToneClient("http://private", transport=transport)
    speaker = SpeakerRef(id="123", name="Speaker", protocol="airplay2", offset_ms=0)
    outputs = [normalize_output({"id": "123", "name": "Speaker", "type": "AirPlay 2", "offset_ms": 0}, set())]
    await client.select([speaker], outputs)
    assert not any(path.startswith("/api/player") for _, path in state["requests"])
    await client.close()


@pytest.mark.asyncio
async def test_reused_output_id_with_different_name_never_receives_audio():
    state, transport = backend_transport()
    client = OwnToneClient("http://private", transport=transport)
    speaker = SpeakerRef(id="123", name="Expected speaker", protocol="airplay2")
    outputs = [normalize_output({"id": "123", "name": "Wrong speaker", "type": "AirPlay 2"}, set())]
    with pytest.raises(RuntimeFailure, match="identity changed"):
        await client.select([speaker], outputs)
    assert state["requests"] == []
    await client.close()


@pytest.mark.asyncio
async def test_phone_slider_coalesces_durably_without_changing_the_event_being_committed(tmp_path):
    service = broker(tmp_path)
    state = runtime_room(tmp_path, room(volume=50, revision=1))
    await service.set_volume(state, 10, pending=True)
    first = dict(state.phone_volume_update)
    await service.set_volume(state, 20, pending=True)
    await service.set_volume(state, 30, pending=True)
    following = dict(state.phone_volume_next)
    assert state.snapshot()["phone_volume_update"] == first
    assert state.current_volume == 30
    saved = json.loads((state.directory / "phone-volume.json").read_text())
    assert saved["id"] == first["id"] and saved["next"] == following
    await service.ack_phone_volume(
        state, {"update_id": first["id"], "accepted": True, "volume": 10, "committed_revision": 2}
    )
    assert state.current_volume == 30
    assert state.phone_volume_update == {**following, "base_revision": 2}
    assert state.phone_volume_next is None
    assert state.desired.revision == 2 and state.desired.volume == 10
    assert json.loads((state.directory / "phone-volume.json").read_text()) == state.phone_volume_update


@pytest.mark.asyncio
async def test_replayed_phone_receipt_never_advances_past_a_newer_user_revision(tmp_path):
    service = broker(tmp_path)
    state = runtime_room(tmp_path, room(volume=50, revision=1))
    await service.set_volume(state, 10, pending=True)
    first = dict(state.phone_volume_update)
    await service.set_volume(state, 30, pending=True)
    state.desired = state.desired.model_copy(update={"revision": 3, "volume": 80})
    state.current_volume = 80
    await service.ack_phone_volume(
        state, {"update_id": first["id"], "accepted": True, "volume": 10, "committed_revision": 2}
    )
    assert state.desired.volume == 80 and state.desired.revision == 3
    assert state.phone_volume_update["base_revision"] == 2
    await service.ack_phone_volume(
        state,
        {
            "update_id": state.phone_volume_update["id"],
            "accepted": False,
            "volume": 80,
            "committed_revision": 3,
        },
    )
    assert state.phone_volume_update is None
    assert state.current_volume == 80
    assert json.loads((state.directory / "phone-volume.json").read_text()) is None


@pytest.mark.asyncio
async def test_phone_queue_survives_restart_and_stale_acknowledgment_cannot_clear_new_event(tmp_path):
    service = broker(tmp_path)
    definition = room(volume=50, revision=1)
    state = RuntimeRoom(definition, service.config.runtime_state_dir / "rooms" / definition.id)
    await service.set_volume(state, 10, pending=True)
    first = dict(state.phone_volume_update)
    await service.set_volume(state, 35, pending=True)
    service.ready = True
    await service.reconcile({"rooms": [definition.model_dump()]})
    restored = service.rooms[definition.id]
    assert restored.current_volume == 35
    assert restored.phone_volume_update == first
    assert restored.phone_volume_next["volume"] == 35
    await service.ack_phone_volume(
        restored, {"update_id": "older-event", "accepted": False, "volume": 80, "committed_revision": 2}
    )
    assert restored.phone_volume_update == first and restored.current_volume == 35
    await service.close()


def test_numeric_and_named_sound_card_aliases_have_one_live_identity(tmp_path):
    service = broker(tmp_path)
    speaker = SpeakerRef(id="0", name="Local speaker", protocol="alsa")
    # Numeric aliases are retained only for explicit physical-key analysis;
    # validated room definitions and actual playback reject numeric CARD.
    historical = room(local_audio_device="hw:Speakers,0").model_copy(update={"local_audio_device": "hw:2,0"})
    with patch("shiri.runtime.broker.Path.read_text", return_value="Speakers\n"):
        indexed = service._speaker_key(historical, speaker)
    named = service._speaker_key(room(local_audio_device="plughw:CARD=Speakers,DEV=0"), speaker)
    assert indexed == named == ("local", "hw:CARD=Speakers,DEV=0,SUBDEV=0")


def test_bluetooth_configuration_uses_reverse_loopback_and_private_owntone_bus(tmp_path):
    definition = room(local_audio_device="bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp", slot=7)
    _, own = backend_configs(
        definition,
        tmp_path,
        {"interface": "sr123"},
        {"api_host_ip": "10.190.1.1", "api_ip": "10.190.1.2"},
        broker_socket=tmp_path / "broker.sock",
        all_receiver_names=[],
        password="private",
    )
    assert 'card = "hw:Loopback,1,7"' in own.read_text()
    assert (tmp_path / "cache").is_dir()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Actual /proc exec identity requires Linux")
async def test_ready_process_manifest_tracks_final_exec_not_its_launcher(tmp_path):
    service = broker(tmp_path)
    service.network = NetworkManager(tmp_path / "state", service.runner)
    gate, ready = tmp_path / "exec-gate", tmp_path / "launcher-ready"
    final = "import time; time.sleep(60)"
    launcher = (
        "import os, sys, time\nfrom pathlib import Path\n"
        "Path(sys.argv[2]).write_text('ready')\n"
        "while not Path(sys.argv[1]).exists(): time.sleep(0.01)\n"
        f"os.execv(sys.executable, [sys.executable, '-c', {final!r}])\n"
    )
    owned = await service._start_process(
        "room:exec", "exec", [sys.executable, "-c", launcher, str(gate), str(ready)], tmp_path
    )
    try:

        async def wait_for(predicate):
            while not predicate():  # noqa: ASYNC110 - observe the actual bounded child transition.
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for(ready.exists), timeout=2)
        assert launcher in service.network.manifest["processes"]["room:exec"]["argv"]
        gate.write_text("execute")
        expected = [sys.executable, "-c", final]
        await asyncio.wait_for(wait_for(lambda: process_args(owned.process.pid) == expected), timeout=2)
        await service._refresh_processes("room", {"exec": owned})
        persisted = json.loads(service.network.manifest_path.read_text())["processes"]["room:exec"]
        assert persisted["argv"] == expected
        assert persisted["executable"] == process_executable(owned.process.pid)
        assert persisted["boot_id"] and persisted["birth"]
    finally:
        await owned.stop()


@pytest.mark.asyncio
async def test_cancelled_partial_selection_keeps_lease_until_verified_backend_stop(tmp_path):
    service = broker(tmp_path)
    service.network = SimpleNamespace(remove=AsyncMock(), forget_process=Mock())
    speaker = SpeakerRef(id="123", name="Physical speaker", protocol="airplay2")
    first = runtime_room(tmp_path, room(enabled=True))
    second = runtime_room(tmp_path, room(enabled=True, slot=1, airplay_name="Bedroom input"))
    output = normalize_output({"id": "123", "name": speaker.name, "type": "AirPlay 2"}, set())
    first.client = SimpleNamespace(
        outputs=AsyncMock(return_value=[output]),
        select=AsyncMock(side_effect=asyncio.CancelledError),
        close=AsyncMock(),
    )
    second.client = SimpleNamespace(
        outputs=AsyncMock(return_value=[output]), select=AsyncMock(return_value={"ok": True})
    )
    first.status = second.status = "running"
    first.processes["owntone"] = SimpleNamespace(
        stop=AsyncMock(side_effect=RuntimeFailure("termination not proven"))
    )
    service.rooms = {first.desired.id: first, second.desired.id: second}
    with pytest.raises(asyncio.CancelledError):
        await service.set_outputs(first, [speaker])
    assert first.desired.speakers == []
    assert service.speaker_leases[("owntone", "123")] == first.desired.id
    second.desired = second.desired.model_copy(update={"speakers": [speaker]})
    await service._restore_outputs(second)
    assert second.selected_ids == []
    assert all(call.args[0] == [] for call in second.client.select.await_args_list)
    with pytest.raises(RuntimeFailure, match="termination not proven"):
        await service._stop_room(first)
    assert service.speaker_leases[("owntone", "123")] == first.desired.id
    first.processes["owntone"].stop.side_effect = None
    await service._stop_room(first)
    await service._restore_outputs(second)
    assert second.selected_ids == [speaker.id]
    assert service.speaker_leases[("owntone", "123")] == second.desired.id


@pytest.mark.asyncio
async def test_identity_snapshot_waits_through_empty_and_changing_exec_fields(tmp_path):
    process = OwnedProcess(
        "backend", SimpleNamespace(pid=44, returncode=None), "100", Mock(), tmp_path / "backend.log"
    )
    with (
        patch("shiri.runtime.system.boot_id", return_value="boot-A"),
        patch("shiri.runtime.system.process_birth", return_value="100"),
        patch(
            "shiri.runtime.system.process_executable",
            side_effect=[
                None,
                "/usr/bin/unshare",
                "/usr/bin/backend",
                "/usr/bin/backend",
                "/usr/bin/backend",
            ],
        ),
        patch(
            "shiri.runtime.system.process_args",
            side_effect=[[], ["unshare"], ["backend", "-f"], ["backend", "-f"]],
        ),
        patch("shiri.runtime.system.asyncio.sleep", new=AsyncMock()),
    ):
        identity = await process.coherent_identity()
    assert identity["executable"] == "/usr/bin/backend" and identity["argv"] == ["backend", "-f"]
    assert identity["birth"] == "100" and identity["boot_id"] == "boot-A"


@pytest.mark.asyncio
async def test_expired_speech_session_health_releases_only_its_room_reservations(tmp_path):
    service = broker(tmp_path)
    state = runtime_room(tmp_path, room(enabled=True))
    state.status = "running"
    state.receiver = {"namespace": "owned"}
    state.processes = {"audio": SimpleNamespace(alive=True)}
    state.client = SimpleNamespace(request=AsyncMock(return_value={"state": "stop"}))
    service.network = SimpleNamespace(healthy=AsyncMock(return_value=True))
    service.sessions = {
        "expired": state.desired.id,
        "active": state.desired.id,
        "negotiating": state.desired.id,
        "other-room": str(uuid4()),
    }
    service.pending_sessions = {"negotiating"}
    with patch(
        "shiri.runtime.broker.call_rpc",
        new=AsyncMock(return_value={"ready": True, "speech_session_id": "active"}),
    ):
        await service._probe_room(state)
    assert set(service.sessions) == {"active", "negotiating", "other-room"}


def test_startup_failure_names_the_exited_backend_and_signal(tmp_path):
    service = broker(tmp_path)
    failed = SimpleNamespace(alive=False, process=SimpleNamespace(returncode=-11))
    with pytest.raises(RuntimeFailure, match="shairport: exit code -11 \\(SIGSEGV\\)"):
        service._require_alive({"shairport": failed}, "during startup")


@pytest.mark.asyncio
async def test_reimported_room_recreates_completed_actor_before_monitor_removes_tombstone(tmp_path):
    service = broker(tmp_path)
    service.ready = True
    definition = room(enabled=False)
    state = runtime_room(tmp_path, definition)
    state.removing = True
    state.task = asyncio.create_task(asyncio.sleep(0))
    await state.task
    previous = state.task
    service.rooms[definition.id] = state
    await service.reconcile({"rooms": [definition.model_dump()]})
    assert state.task is not previous and not state.task.done() and not state.removing
    await service.close()
