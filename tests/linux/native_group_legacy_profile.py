"""Explicit two-local-zone historical grouping policy: zero-offset H1000/B500.

The actual broker and generated config use this policy together. It does not
change production defaults or accept worker health with different buffers.
"""
from functools import partial
from types import FunctionType

from shiri.domain import Room
from shiri.runtime.broker import Broker
from shiri.runtime.configuration import backend_configs
from shiri.runtime.latency import LatencyPlan, RoomBuffer, latency_plan
from shiri.runtime.system import RuntimeFailure

A = 'b6786543-7eb2-443d-83b1-65b984123a76'
B = '5356832c-c514-4472-bb4b-4f34ed86dd07'


def _room(definition):
    room = Room.model_validate(definition)
    if room.enabled and (room.id not in {A, B} or room.local_audio_device is None
            or len(room.speakers) > 1 or any(speaker.id != '0' or speaker.protocol != 'alsa'
                                         for speaker in room.speakers)):
        raise RuntimeFailure('Historical grouping profile requires its exact two local rooms')
    return room


def buffer(definition):
    room = _room(definition)
    return max(500, max((250 - min(0, speaker.offset_ms) for speaker in room.speakers), default=0))


def plan(definitions):
    baseline = latency_plan(definitions)  # Full identity/domain/bounds validation.
    rooms = {_room(definition).id: definition for definition in definitions}
    decisions = tuple(RoomBuffer(item.room_id, item.revision, buffer(rooms[item.room_id])) for item in baseline.rooms)
    return LatencyPlan(max((item.output_buffer_ms for item in decisions), default=500) + 500, decisions)


def bind(function, **names):
    result = FunctionType(function.__code__, {**function.__globals__, **names},
                          function.__name__, function.__defaults__, function.__closure__)
    result.__kwdefaults__ = function.__kwdefaults__
    return result


def broker_class(base):
    if not issubclass(base, Broker):
        raise RuntimeFailure('Historical grouping profile requires the actual isolated Broker')
    return type('HistoricalGroupingBroker', (base,), {
        'reconcile': bind(Broker.reconcile, latency_plan=plan, room_buffer_ms=buffer),
        'set_outputs': bind(Broker.set_outputs, latency_plan=plan, room_buffer_ms=buffer),
        '_material': bind(Broker._material, room_buffer_ms=buffer),
        '_start_room': bind(Broker._start_room, backend_configs=partial(backend_configs, minimum_latency=False)),
    })
