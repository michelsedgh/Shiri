"""Actual production minimum timing evidence for the existing isolated gates.

Import creates no actor or media resource. Volume-only revisions are allowed
by the Bluetooth gate; the admitted routes, offsets and actual H/B stay frozen.
"""
from dataclasses import dataclass
import time

from shiri.domain import Room
from shiri.runtime.latency import LatencyPlan, latency_plan
from shiri.runtime.system import RuntimeFailure

A = 'b6786543-7eb2-443d-83b1-65b984123a76'
B = '5356832c-c514-4472-bb4b-4f34ed86dd07'


def require(condition, message):
    if not condition:
        raise RuntimeFailure(message)


def route(room):
    room = Room.model_validate(room)
    # The production planner supplies all domain, identity and offset checks.
    latency_plan([room])
    require(room.enabled and room.id in {A, B} and room.local_audio_device is not None
            and len(room.speakers) == 1 and room.speakers[0].id == '0'
            and room.speakers[0].protocol == 'alsa' and room.speakers[0].offset_ms == 0,
            'Minimum coverage requires its exact enabled zero-offset local0 route')
    return room.id, room.local_audio_device, tuple((s.id, s.protocol, s.offset_ms) for s in room.speakers)


def decisions(plan):
    return tuple((item.room_id, item.output_buffer_ms) for item in plan.rooms)


@dataclass(frozen=True)
class MinimumTiming:
    plan: LatencyPlan
    routes: tuple
    frozen_monotonic_ns: int

    @property
    def horizon_ns(self):
        return self.plan.common_horizon_ns

    def receipt(self):
        return {'version': 1, 'policy': 'actual_production_minimum',
                'common_horizon_ns': self.horizon_ns, 'output_buffers_ms': dict(decisions(self.plan)),
                'saved_routes': [{'room_id': identifier, 'local_audio_device': device,
                                  'speakers': [{'id': sid, 'protocol': protocol, 'offset_ms': offset}
                                               for sid, protocol, offset in speakers]}
                                 for identifier, device, speakers in self.routes],
                'frozen_monotonic_ns': self.frozen_monotonic_ns, 'frozen_before_owner_or_pcm': True,
                'scope': 'Unchanged production planner and actual all-enabled worker H140/B40; exact zero-offset routes frozen before owner/PCM'}


def current_plan(definitions):
    plan = latency_plan(definitions)
    enabled = [Room.model_validate(room) for room in definitions if room.enabled]
    require({room.id for room in enabled} == {A, B} and len(enabled) == 2,
            'Minimum coverage requires exactly its two enabled rooms')
    routes = tuple(sorted(route(room) for room in enabled))
    require(plan.common_horizon_ms == 140
            and decisions(plan) == tuple((identifier, 40) for identifier in sorted((A, B))),
            'Minimum coverage must use actual production H140/B40')
    return plan, routes


def worker_health(health, *, before_pcm=False):
    source = health.get('source') if type(health) is dict else None
    require(type(health) is dict and health.get('ready') is True
            and 'error' in health and health['error'] is None
            and type(source) is dict and source.get('ready') is True and 'owner' in source
            and type(health.get('timing_relay_delay_ms')) is int and health['timing_relay_delay_ms'] == 140
            and type(health.get('output_buffer_ms')) is int and health['output_buffer_ms'] == 40,
            'Minimum coverage actual worker H/B or source readiness changed')
    if before_pcm:
        require(source['owner'] is None and type(health.get('native_blocks')) is int and health['native_blocks'] == 0,
                'Minimum timing freeze must precede native owner and PCM')


def freeze(definitions, healths):
    plan, routes = current_plan(definitions)
    require(set(healths) == {A, B}, 'Minimum coverage health omits an enabled room')
    for health in healths.values():
        worker_health(health, before_pcm=True)
    return MinimumTiming(plan, routes, time.monotonic_ns())


def require_timing(frozen, definitions, healths):
    require(type(frozen) is MinimumTiming, 'Minimum coverage has no exact frozen plan')
    plan, routes = current_plan(definitions)
    require(plan.common_horizon_ns == frozen.horizon_ns and decisions(plan) == decisions(frozen.plan)
            and routes == frozen.routes and set(healths) == {A, B},
            'Minimum coverage changed its frozen route or production plan')
    for health in healths.values():
        worker_health(health)


def require_room_timing(frozen, identifier, definition, health, *, before_pcm=False):
    require(type(frozen) is MinimumTiming and identifier in {A, B}, 'Minimum coverage lost its exact room plan')
    current = route(definition)
    require(current[0] == identifier and current == next(item for item in frozen.routes if item[0] == identifier),
            'Minimum room changed its frozen route or offset')
    worker_health(health, before_pcm=before_pcm)


def valid_receipt(receipt):
    """Outer supervisors additionally refuse a historical or absent plan claim."""
    if type(receipt) is not dict or set(receipt) != {
            'version', 'policy', 'common_horizon_ns', 'output_buffers_ms', 'saved_routes',
            'frozen_monotonic_ns', 'frozen_before_owner_or_pcm', 'scope'}:
        return False
    if (type(receipt['version']) is not int or receipt['version'] != 1
            or receipt['policy'] != 'actual_production_minimum'
            or type(receipt['common_horizon_ns']) is not int or receipt['common_horizon_ns'] != 140_000_000
            or receipt['output_buffers_ms'] != {A: 40, B: 40}
            or any(type(value) is not int for value in receipt['output_buffers_ms'].values())
            or type(receipt['frozen_monotonic_ns']) is not int or receipt['frozen_monotonic_ns'] <= 0
            or receipt['frozen_before_owner_or_pcm'] is not True):
        return False
    routes = receipt['saved_routes']
    return (type(routes) is list and len(routes) == 2
            and [item.get('room_id') for item in routes if type(item) is dict] == sorted((A, B))
            and all(set(item) == {'room_id', 'local_audio_device', 'speakers'}
                    and type(item['local_audio_device']) is str and item['local_audio_device']
                    and item['speakers'] == [{'id': '0', 'protocol': 'alsa', 'offset_ms': 0}]
                    and type(item['speakers'][0]['offset_ms']) is int for item in routes))
