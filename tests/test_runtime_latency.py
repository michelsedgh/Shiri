"""Administrative timing changes cannot silently split a grouped timeline."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from shiri.domain import Room, SpeakerRef
from shiri.runtime.broker import Broker, RuntimeRoom, Superseded
from shiri.runtime.latency import latency_plan, room_buffer_ms
from shiri.settings import Settings


def definition(slot, *, revision=1, offset=0, enabled=True):
    return Room(id=str(uuid4()), slot=slot, name=f'Room {slot}', airplay_name=f'Input {slot}',
                interface='eth0', revision=revision, enabled=enabled,
                speakers=[SpeakerRef(id=str(100+slot), name=f'Speaker {slot}',
                                     protocol='airplay2', offset_ms=offset)])


def setup(tmp_path, definitions):
    service = Broker(Settings(state_dir=tmp_path/'api', runtime_state_dir=tmp_path/'runtime',
                              runtime_dir=tmp_path/'run', runtime_socket=tmp_path/'run/runtime.sock'))
    service.ready = True
    plan = latency_plan(definitions)
    for item in definitions:
        timing = (room_buffer_ms(item), plan.common_horizon_ms)
        room = RuntimeRoom(item, tmp_path/item.id, status='running', applied=item,
                           backend_definition=item, timing=timing, active_timing=timing if item.enabled else None)
        room.client = SimpleNamespace(base_url='http://private-test')
        service.rooms[item.id] = room
    return service


async def test_negative_compensation_retires_every_old_common_horizon_even_with_a_stale_api_revision(tmp_path):
    a, b = definition(0, revision=3), definition(1)
    service = setup(tmp_path, [a, b])
    stale_a = a.model_copy(update={'revision': 2, 'volume': 99})
    changed_b = b.model_copy(update={'revision': 2, 'speakers': [b.speakers[0].model_copy(update={'offset_ms': -2000})]})
    await service.reconcile({'rooms': [stale_a.model_dump(), changed_b.model_dump()]})
    ra, rb = service.rooms[a.id], service.rooms[b.id]
    assert ra.desired == a and ra.current_volume != 99
    # Selected AirPlay compensation retains500ms receiver lead; every room
    # coordinates the resulting2600ms horizon without rewriting active music.
    assert ra.timing == (500, 2600) and rb.timing == (2500, 2600)
    assert ra.restart_required and rb.restart_required and ra.wake.is_set() and rb.wake.is_set()
    with pytest.raises(Superseded):
        service._check_start(ra, a)
    assert ra.active_timing == (500, 600)  # No midstream clock mutation.


async def test_volume_revision_and_positive_offsets_preserve_the_common_clock_and_qualified_buffer(tmp_path):
    a, b = definition(0), definition(1)
    service = setup(tmp_path, [a, b])
    changed_a = a.model_copy(update={'revision': 2, 'volume': 25})
    changed_b = b.model_copy(update={'revision': 2, 'speakers': [b.speakers[0].model_copy(update={'offset_ms': 2000})]})
    await service.reconcile({'rooms': [changed_a.model_dump(), changed_b.model_dump()]})
    assert service.rooms[a.id].timing == service.rooms[a.id].active_timing == (500, 600)
    assert service.rooms[b.id].timing == service.rooms[b.id].active_timing == (500, 600)
    assert not any(room.restart_required for room in service.rooms.values())
    assert service.rooms[a.id].current_volume == 25


async def test_disabled_compensated_room_cannot_charge_running_music_for_unused_buffer(tmp_path):
    a, b = definition(0), definition(1, offset=-2000, enabled=False)
    service = setup(tmp_path, [a, b])
    service.rooms[b.id].client = None
    service.rooms[b.id].status = 'stopped'
    await service.reconcile({'rooms': [a.model_dump(), b.model_dump()]})
    assert service.rooms[a.id].timing == (500, 600)
    assert not service.rooms[a.id].restart_required


async def test_bluealsa_compensation_retains_local_margin_without_charging_a_zero_offset_airplay_peer(tmp_path):
    a, b = definition(0), definition(1)
    b = Room.model_validate(b.model_copy(update={
        "local_audio_device": "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp",
        "speakers": [SpeakerRef(id="0", name="Pinned local output", protocol="alsa")],
    }))
    service = setup(tmp_path, [a, b])
    changed_b = b.model_copy(update={"revision": 2, "speakers": [
        b.speakers[0].model_copy(update={"offset_ms": -2000})]})
    await service.reconcile({"rooms": [a.model_dump(), changed_b.model_dump()]})
    assert service.rooms[a.id].timing == (500, 2140)
    assert service.rooms[b.id].timing == (2040, 2140)
    assert all(room.restart_required for room in service.rooms.values())
    assert service.rooms[a.id].active_timing == (500, 600)
    assert service.rooms[b.id].active_timing == (40, 600)
