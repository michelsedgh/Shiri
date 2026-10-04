"""Explicit1800-second digital music soak; no physical/phone interoperability claim.

Every sample follows one immutable native calendar. Only this mode extends the
same120ms deterministic code beyond20s. Route B/H is supplied explicitly after
qualification; no production defaults change. Retained PCM/memory is bounded.
"""

from __future__ import annotations

# Bounded explicit fixture/kernel/config reads.
# ruff: noqa: ASYNC240
import asyncio
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, replace
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import time
from uuid import UUID, uuid4

import numpy as np
from shiri.deadline import bounded
from shiri.domain import Room
from shiri.runtime.broker import Broker
from shiri.runtime.latency import LatencyPlan, latency_plan
from shiri.runtime.system import RuntimeFailure, atomic_json, root_directory
from shiri.runtime.timing import Kind, Packet, RATE


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


music = load("soak_original_music", "native_music_minimum.py")
streaming = load("soak_streaming", "native_soak_capture.py")
epoch, Phase, bind = music.epoch, music.Phase, music.bind
A, B = music.A, music.B
NATIVE_LEAD_NS, SEND_LATE_NS = music.NATIVE_LEAD_NS, music.SEND_LATE_NS
BODY_SECONDS = 1800
PRODUCER_SECONDS = 1830
WINDOW_SECONDS = 8
CADENCE_SECONDS = 30
MAX_WINDOWS = 64
MAX_PROGRESS_BYTES = 32 * 1024


def require(value, message):
    if not value:
        raise RuntimeFailure(message)


@dataclass(frozen=True)
class Policy:
    buffer_ms: int
    horizon_ms: int

    def __post_init__(self):
        require(
            type(self.buffer_ms) is int
            and 40 <= self.buffer_ms <= 4250
            and type(self.horizon_ms) is int
            and self.buffer_ms + 100 <= self.horizon_ms <= 5000,
            "Soak requires explicit route B/H, retained ALSA40ms floor and at least100ms timer lead",
        )


_policy = None


def configure(buffer_ms, horizon_ms):
    global _policy
    proposed = Policy(buffer_ms, horizon_ms)
    require(_policy is None or proposed == _policy, "Soak cannot change a frozen timing policy")
    _policy = proposed
    return proposed


def policy():
    require(_policy is not None, "Soak B/H must be explicitly bound before any admission")
    return _policy


def producer_profile():
    p = policy()
    return {
        "version": 1,
        "buffer_ms": p.buffer_ms,
        "horizon_ms": p.horizon_ms,
        "duration_seconds": PRODUCER_SECONDS,
        "continuous_code": True,
    }


def candidate_plan(definitions):
    definitions = list(definitions)
    rooms = latency_plan(definitions).rooms
    for definition in definitions:
        room = Room.model_validate(definition)
        if room.enabled:
            require(
                room.id in {A, B}
                and room.local_audio_device is not None
                and len(room.speakers) <= 1
                and all(s.id == "0" and s.protocol == "alsa" and s.offset_ms == 0 for s in room.speakers),
                "Soak allows only its exact enrolled zero-offset ALSA routes",
            )
    p = policy()
    return LatencyPlan(p.horizon_ms, tuple(replace(item, output_buffer_ms=p.buffer_ms) for item in rooms))


def room_buffer_ms(room):
    candidate_plan([room])
    return policy().buffer_ms


def broker_class(base):
    require(issubclass(base, Broker), "Soak requires actual isolated Broker")
    return type(
        "SoakBroker",
        (base,),
        {
            "reconcile": bind(Broker.reconcile, latency_plan=candidate_plan, room_buffer_ms=room_buffer_ms),
            "set_outputs": bind(
                Broker.set_outputs, latency_plan=candidate_plan, room_buffer_ms=room_buffer_ms
            ),
            "_material": bind(Broker._material, room_buffer_ms=room_buffer_ms),
        },
    )


def require_idle_worker(health):
    return bind(music.require_idle_worker, BUFFER_MS=policy().buffer_ms, HORIZON_MS=policy().horizon_ms)(
        health
    )


def admit_launch(room, duration, start, lead, frozen, frozen_type, *, lab):
    require(
        type(duration) is int and duration == PRODUCER_SECONDS,
        "Soak requires its separate1830-second producer",
    )
    return bind(music.admit_launch, BUFFER_MS=policy().buffer_ms, HORIZON_MS=policy().horizon_ms)(
        room, 90, start, lead, frozen, frozen_type, lab=lab
    )


_plan_receipt = bind(epoch.plan_receipt, latency_plan=candidate_plan)


def plan_receipt(phase, definitions, healths, *, before_pcm):
    require(
        type(phase) is Phase and phase.phase == "native" and phase.offset_ms == 0,
        "Soak requires its exact zero-offset native phase",
    )
    return _plan_receipt(phase, definitions, healths, before_pcm=before_pcm)


def envelope(frame):
    # Identical original bit mixer; only this explicit mode continues the code.
    chip = frame // (RATE * 120 // 1000)
    value = (chip + 1) * 0x9E3779B1 & 0xFFFFFFFF
    value ^= value >> 16
    value = value * 0x85EBCA6B & 0xFFFFFFFF
    value ^= value >> 13
    return 1.0 if value & 1 else 0.65


def program_pcm(frame_index, frames=960, frequency=440):
    # Vectorize only the approved continuous SOAK stimulus. The arithmetic and
    # oscillator operation order match the original function byte for byte.
    positions = np.arange(frame_index, frame_index + frames)
    chips = (positions // (RATE * 120 // 1000)).astype(np.uint64)
    values = ((chips + 1) * np.uint64(0x9E3779B1)) & np.uint64(0xFFFFFFFF)
    values ^= values >> np.uint64(16)
    values = (values * np.uint64(0x85EBCA6B)) & np.uint64(0xFFFFFFFF)
    values ^= values >> np.uint64(13)
    gains = np.where(values & np.uint64(1), 1.0, 0.65)
    mono = (8192 * gains * np.sin(2 * np.pi * frequency * positions / RATE)).astype("<i2")
    return np.repeat(mono[:, None], 2, axis=1).tobytes()


capture_type = streaming.capture_type


def guard_type(base):
    class SoakGuard(base):
        def check(self, *, live=True):
            super().check(live=live)
            c = self.capture
            if c.reference_pcm is not None:
                # Teardown callbacks still retain every original caps/PTS fence.
                # Reference verification stops at the same explicit stop boundary.
                through = len(c.chunks)
                if self.end_requested_ns is not None:
                    through = c.reference_index
                    while through < len(c.chunks) and c.captured_at[through] * 1e9 <= self.end_requested_ns:
                        through += 1
                c.verify_reference(through=through)
                c.evict_verified(self.index)

    return SoakGuard


async def join_owned(task):
    async def consume():
        return await asyncio.gather(task, return_exceptions=True)

    join = asyncio.create_task(consume(), name="soak-owned-join")
    cancellation = None
    while not join.done():
        try:
            await asyncio.shield(join)
        except asyncio.CancelledError as exc:
            cancellation = exc
    result = join.result()
    if cancellation is not None:
        raise cancellation
    return result


async def analysis(function, *args, **kwargs):
    # Cancellation cannot leave an owned analysis task using snapshots after
    # cleanup. A running thread finishes its finite<=8s input before propagation.
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs), name="soak-window-analysis")
    try:
        await asyncio.wait({task})
        return task.result()
    finally:
        if not task.done():
            await join_owned(task)
        elif not task.cancelled():
            # A caller cancellation may race with a finished failed child.
            # Retrieve the owned error without replacing the caller's failure.
            task.exception()


class SoakObserver(music.MusicObserver):
    async def watch(self):
        try:
            while not self.done.is_set():
                await self.sample()
                try:
                    await bounded(self.done.wait(), 0.06)
                except asyncio.TimeoutError:
                    pass
        except Exception as exc:
            self.error = str(exc)[:2000]
            raise

    async def close(self):
        if self.stopped:
            return
        failure = None
        try:
            await self.check()
        except BaseException as exc:
            failure = exc
        finally:
            self.done.set()
            if self.watcher is not None:
                self.watcher.cancel()
                try:
                    results = await join_owned(self.watcher)
                    if results and isinstance(results[0], Exception) and failure is None:
                        failure = results[0]
                except asyncio.CancelledError as exc:
                    if failure is not None:
                        self.context.evidence["observer_retirement_primary_failure_type"] = type(
                            failure
                        ).__name__
                        raise exc from failure
                    raise
                except BaseException as exc:
                    if failure is None:
                        failure = exc
        if failure is not None:
            raise failure
        requested = time.monotonic_ns()
        self.context.evidence["stop_boundary"] = {
            "requested_monotonic_ns": requested,
            "scope": "Original source/control/music guards through fixture stop; later PCM is teardown",
        }
        for guard in self.context.pcm_guards.values():
            guard.end_at(requested)
        self.context.evidence["per_buffer_evidence"] = {
            key: guard.evidence() for key, guard in self.context.pcm_guards.items()
        }
        self.stopped = True


async def prepare(context):
    require(
        context.group.NATIVE_LAB is not None
        and context.report.get("mode") == "music_soak"
        and not context.producers
        and not context.captures,
        "Soak requires an explicit cold clean lab",
    )
    context.group.program_pcm = program_pcm
    context.group.declared_capture = bind(
        context.group.declared_capture, program_pcm=context.group.program_pcm
    )
    frozen = plan_receipt(
        context.phase,
        [s.desired for s in context.broker.rooms.values()],
        await epoch.worker_health(context),
        before_pcm=True,
    )
    for identifier, state in context.states.items():
        state.local_pin.validate()
        require(
            state.local_pin.manifest["device"] == context.group.ZONES[identifier]["device"]
            and state.local_pin.manifest["subdevice"] == 7,
            "Soak route escaped its exact fixture device",
        )
        selected = [item for item in await state.client.outputs(set()) if item["selected"]]
        require(
            state.selected_ids == ["0"]
            and len(selected) == 1
            and selected[0]["id"] == "0"
            and type(selected[0].get("offset_ms")) is int
            and selected[0]["offset_ms"] == 0,
            "Soak actual/saved output selection changed",
        )
        require(
            (await state.client.request("GET", "/api/player"))["state"] == "stop",
            "Soak cold output already playing",
        )
        context.latency.playback_closed(state.local_pin.manifest)
    context.frozen_plan, context.declared_horizon_ns = frozen, frozen["common_horizon_ns"]
    context.evidence = {
        **context.phase.receipt(),
        "passed": False,
        "frozen_plan": deepcopy(frozen),
        "mode": "music_soak",
        "speech_exercised": False,
        "physical_output_verified": False,
        "native_receiver_callback_verified": False,
        "minimum_latency_qualification_claimed": False,
        "scope": "1800s real elapsed digital final PCM; synthetic exact-UID ingress; explicit frozen B/H",
        "producer_seconds": PRODUCER_SECONDS,
        "body_seconds": BODY_SECONDS,
        "timing_windows": [],
        "window_seconds": WINDOW_SECONDS,
        "window_cadence_seconds": CADENCE_SECONDS,
        "streaming_limits": {
            "bytes": streaming.MAX_BYTES,
            "blocks": streaming.MAX_BLOCKS,
            "retained_seconds": streaming.RETAIN_SECONDS,
        },
        "delivery_envelope": {
            "native_callback_lead_ns": NATIVE_LEAD_NS,
            "strict_send_lateness_ns": SEND_LATE_NS,
        },
    }
    context.report["music_soak"] = context.report["latency_epoch"] = context.evidence
    context.report["latency_probe"] = {"preparations": []}
    context.evidence["independent_capture_baselines"] = deepcopy(
        context.report["independent_capture_baselines"]
    )
    return frozen


calendar = music.calendar
check_original_actors = music.check_original_actors


def observe_delivery(state, stage, frame, target_ns, *, lateness_ns=None):
    """One bounded observation; it never changes the source calendar."""
    observed = time.monotonic_ns()
    state["delivery_observation"] = {
        "stage": stage,
        "frame": frame,
        "target_ns": target_ns,
        "observed_ns": observed,
        "lateness_ns": observed - target_ns if lateness_ns is None else lateness_ns,
    }


def progress_json(path, state):
    """Atomic transient progress, without durability waits on audio delivery.

    Initial and final ownership evidence still uses durable atomic_json. This
    bounds bytes and operations; ordinary filesystem calls have no wall-time
    guarantee and the original send deadline continues to reject their delay.
    """
    started = time.monotonic_ns()
    previous = state.get("progress_publication", {})
    temporary = None
    passed = False
    primary = None
    try:
        data = json.dumps(state, allow_nan=False, indent=2).encode()
        require(len(data) <= MAX_PROGRESS_BYTES, "Soak transient progress exceeded its32KiB bound")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
        os.replace(temporary, path)
        passed = True
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup_error = None
        if temporary is not None:
            try:
                with suppress(FileNotFoundError):
                    os.unlink(temporary)
            except BaseException as error:
                cleanup_error = error
        finished = time.monotonic_ns()
        state["progress_publication"] = {
            "started_ns": started,
            "finished_ns": finished,
            "duration_ns": finished - started,
            "max_duration_ns": max(previous.get("max_duration_ns", 0), finished - started),
            "count": previous.get("count", 0) + 1,
            "passed": passed,
        }
        if cleanup_error is not None:
            if primary is not None:
                raise primary from cleanup_error
            raise cleanup_error


async def producer(config, group):
    """Real BEGIN/GRANT/PCM; zero calendar rebasing and zero frame skipping."""
    profile = config.get("music_soak")
    require(
        type(profile) is dict
        and set(profile) == {"version", "buffer_ms", "horizon_ms", "duration_seconds", "continuous_code"}
        and type(profile["version"]) is int
        and profile["version"] == 1
        and type(profile["duration_seconds"]) is int
        and profile["duration_seconds"] == PRODUCER_SECONDS
        and profile["continuous_code"] is True,
        "Soak producer needs exact separate continuous-code profile",
    )
    configure(profile["buffer_ms"], profile["horizon_ms"])
    program = program_pcm
    group = {**group, "bracketed_packet": bind(group["bracketed_packet"], program_pcm=program)}
    require(
        type(config.get("music_soak")) is dict
        and config["music_soak"] == producer_profile()
        and all(
            type(config["music_soak"][key]) is int
            for key in ("version", "buffer_ms", "horizon_ms", "duration_seconds")
        )
        and config.get("common_start_ns") == 0
        and type(config.get("common_start_ns")) is int
        and type(config.get("arrival_lead_ns")) is int
        and config["arrival_lead_ns"] == NATIVE_LEAD_NS
        and config.get("duration_seconds") == PRODUCER_SECONDS
        and type(config.get("duration_seconds")) is int
        and type(config.get("leader")) is bool
        and type(config.get("wide_bracket")) is bool
        and os.geteuid() == config["uid"] != 0
        and os.getegid() == config["gid"],
        "Soak producer lost its exact credential/calendar/profile admission",
    )
    loop, stop = asyncio.get_running_loop(), asyncio.Event()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stop.set)
    path, command = Path(config["status"]), Path(config["command"])
    session, group_id = uuid4().bytes, UUID(config["group"]).bytes
    flags = group["FLAGS"] | (group["FLAG_GROUP_LEADER"] if config["leader"] else 0)
    state = {
        "uid": os.geteuid(),
        "gid": os.getegid(),
        "pid": os.getpid(),
        "stage": "ready_before_begin",
        "ready_before_begin_ns": time.monotonic_ns(),
        "frames": 0,
        "session_id": str(UUID(bytes=session)),
        "commands": [],
        "finished": False,
        "max_send_lateness_ns": 0,
        "max_clock_bracket_ns": 0,
        "clock_sample_attempts": 0,
        "clock_sample_retries": 0,
        "max_attempt_clock_bracket_ns": 0,
    }
    connection = None
    primary = None
    try:
        atomic_json(path, state)
        deadline = time.monotonic() + 8
        action = None
        while not stop.is_set():
            action = json.loads(command.read_text())
            if action == {"generation": 1, "action": "wait"}:
                require(
                    time.monotonic() < deadline, "Soak calendar was not released within preparation budget"
                )
                await asyncio.sleep(0.002)
                continue
            available, start = calendar(action)
            break
        else:
            raise RuntimeFailure("Soak stopped before BEGIN")
        attempted = time.monotonic_ns()
        require(
            state["ready_before_begin_ns"] < available <= attempted and attempted - available < SEND_LATE_NS,
            "Soak calendar is stale, reversed or beyond the strict delivery envelope before BEGIN",
        )
        state.update(
            available_ns=available,
            common_start_ns=start,
            calendar_generation=2,
            begin_attempt_ns=attempted,
            stage="connecting",
        )
        atomic_json(path, state)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        connection.setblocking(False)
        await bounded(loop.sock_connect(connection, config["socket"]), 3)
        state["begin_sent_ns"] = time.monotonic_ns()
        begin = Packet(Kind.BEGIN, session, group=group_id, flags=flags)
        await bounded(loop.sock_sendall(connection, begin.encode()), 0.3)
        grant = Packet.decode(await bounded(loop.sock_recv(connection, 4096), 5))
        require(
            grant.kind is Kind.GRANT
            and grant.session == session
            and grant.group == group_id
            and grant.epoch > 0
            and grant.incarnation != bytes(16)
            and grant.generation == 1
            and grant.flags == flags,
            "Soak ingress omitted its exact fresh source grant",
        )
        state.update(
            stage="granted",
            grant_received_ns=time.monotonic_ns(),
            epoch=grant.epoch,
            incarnation=str(UUID(bytes=grant.incarnation)),
            generation=grant.generation,
        )
        atomic_json(path, state)
        frame, sequence, next_status = 0, 0, 0
        while not stop.is_set() and frame < RATE * PRODUCER_SECONDS:
            observe_delivery(state, "command_read", frame, available + frame * 1_000_000_000 // RATE)
            current = json.loads(command.read_text())
            require(current == action, "Soak calendar changed after original BEGIN")
            target = available + frame * 1_000_000_000 // RATE
            now = time.monotonic_ns()
            if now < target:
                try:
                    await bounded(stop.wait(), (target - now) / 1e9)
                except asyncio.TimeoutError:
                    pass
                continue
            lateness = time.monotonic_ns() - target
            if frame == 0:
                state["first_pcm_attempt_ns"] = time.monotonic_ns()
                state["first_pcm_lateness_ns"] = lateness
            observe_delivery(state, "preclock", frame, target, lateness_ns=lateness)
            require(lateness < SEND_LATE_NS, "Synthetic producer missed bounded native delivery cadence")
            observe_delivery(state, "clock_sample", frame, target)
            packet = group["bracketed_packet"](
                grant, group_id, frame, sequence, start, config["wide_bracket"], 440, stats=state
            )
            observe_delivery(state, "native_send", frame, target)
            lateness = await group["send_native_packet"](loop, connection, packet.encode(), target, stats=state)
            if frame == 0:
                state.update(
                    first_pcm_sent_ns=time.monotonic_ns(),
                    first_pcm_frame=packet.frame_index,
                    first_pcm_sequence=packet.sequence,
                    first_pcm_native_presentation_ns=start,
                )
            state["max_send_lateness_ns"] = max(state["max_send_lateness_ns"], lateness)
            state["max_clock_bracket_ns"] = max(
                state["max_clock_bracket_ns"], packet.monotonic_after_ns - packet.monotonic_before_ns
            )
            frame += packet.frames
            sequence += 1
            state.update(stage="streaming", frames=frame, presentation_ns=packet.presentation_ns)
            if now >= next_status:
                observe_delivery(state, "progress_publish", packet.frame_index, target)
                progress_json(path, state)
                next_status = now + 250_000_000
        state.update(stage="finished", finished=True)
    except BaseException as exc:
        primary = exc
        state["error"] = group["observation"].redact_exception(exc)
        raise
    finally:
        group["retire_producer"]([connection] if connection is not None else [], path, state, primary, publish=atomic_json)


async def exercise(context):
    """Only original cold music; no offer/priming/TTS or route mutation."""
    group = context.group
    before = await epoch.worker_health(context)
    require(
        plan_receipt(
            context.phase, [s.desired for s in context.broker.rooms.values()], before, before_pcm=True
        )
        == context.frozen_plan,
        "Epoch plan changed immediately before original receiver preparation",
    )
    monitor = context.broker._monitor
    require(
        monitor is not None and not monitor.done(),
        "Production health monitor is absent before epoch preparation",
    )
    try:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        for identifier in (A, B):
            state = context.states[identifier]
            root = state.directory / "native-validation-epoch" / context.phase.epoch_id
            root_directory(root)
            context.producers[identifier] = await group.launch_producer(
                context.broker,
                state,
                root,
                0,
                duration_seconds=PRODUCER_SECONDS,
                arrival_lead_ns=NATIVE_LEAD_NS,
                frozen_timing=group.FrozenWorkerTiming(
                    policy().horizon_ms * 1_000_000,
                    tuple((key, policy().buffer_ms) for key in sorted((A, B))),
                ),
                music_soak=True,
            )
    finally:
        context.broker._monitor = asyncio.create_task(
            context.broker._health_monitor(), name="native-epoch-runtime-health"
        )

    async def ready_before_begin():
        values = {
            identifier: group.producer_status(handle) for identifier, handle in context.producers.items()
        }
        return (
            values
            if all(
                value
                and value["stage"] == "ready_before_begin"
                and value["frames"] == 0
                and "begin_attempt_ns" not in value
                for value in values.values()
            )
            else None
        )

    ready = await group.observation.base.eventually(
        ready_before_begin, "both exact receiver units before BEGIN", timeout=8
    )
    context.evidence["producer_ready_before_begin"] = deepcopy(ready)
    units = {
        identifier: {name: unit.identity() for name, unit in state.processes.items()}
        for identifier, state in context.states.items()
    }
    context.evidence["initial_unit_identities"] = deepcopy(units)
    initial_players = {}
    for identifier in context.producers:
        state = context.states[identifier]
        require(
            (await state.client.request("GET", "/api/player"))["state"] == "stop",
            "Cold OwnTone started before the immutable native calendar",
        )
        context.latency.playback_closed(state.local_pin.manifest)
        pin = state.local_pin.manifest
        capture = context.capture_factory(
            f"hw:{pin['card_index']},{1 - pin['device']},7",
            context.clock,
            context.base_time,
            context.clock_offset,
        )
        context.captures[identifier] = capture
        context.pcm_guards[identifier] = guard_type(group.FinalPcmGuard)(capture)
        capture.start()
    require(
        plan_receipt(
            context.phase,
            [s.desired for s in context.broker.rooms.values()],
            await epoch.worker_health(context),
            before_pcm=True,
        )
        == context.frozen_plan,
        "Actual plan/source changed before original BEGIN release",
    )
    require(
        context.broker._monitor is not None and not context.broker._monitor.done(),
        "Production health observer was not restored before calendar release",
    )
    available = time.monotonic_ns()
    start = available + NATIVE_LEAD_NS
    command = {"generation": 2, "action": "begin", "available_ns": available, "common_start_ns": start}
    context.evidence.update(
        common_presentation_ns=start,
        nominal_first_available_ns=available,
        calendar_frozen_before_begin=True,
        captures_armed_before_begin=True,
        production_health_monitor_restored_before_begin=True,
    )
    for handle in context.producers.values():
        group.publish_command(handle["command"], command, handle["account"])

    async def granted():
        values = {
            identifier: group.producer_status(handle) for identifier, handle in context.producers.items()
        }
        for value in values.values():
            if value and value.get("begin_attempt_ns"):
                require(
                    value["available_ns"] == available
                    and value["common_start_ns"] == start
                    and value["ready_before_begin_ns"] < available <= value["begin_attempt_ns"],
                    "A producer replaced or preceded its frozen pre-BEGIN calendar",
                )
        return (
            values
            if all(value and value["stage"] in {"granted", "streaming"} for value in values.values())
            else None
        )

    grants = await group.observation.base.eventually(
        granted, "exact original grants and immutable calendar", timeout=8
    )
    context.evidence["producer_grants"] = deepcopy(grants)
    indexes, progress = {}, {}

    async def initial_pcm():
        healths = await epoch.worker_health(context)
        require(
            plan_receipt(
                context.phase, [s.desired for s in context.broker.rooms.values()], healths, before_pcm=False
            )
            == context.frozen_plan,
            "Actual worker plan changed after first PCM",
        )
        await check_original_actors(context, healths, units, grants)
        for identifier, capture in context.captures.items():
            context.pcm_guards[identifier].check()
            if identifier not in indexes:
                index = group.music_onset_index(capture)
                if index is not None:
                    indexes[identifier] = index
                    context.pcm_guards[identifier].begin(index)
            if identifier in indexes:
                player = await context.states[identifier].client.request("GET", "/api/player")
                require(
                    player["state"] == "play"
                    and player.get("volume") == context.states[identifier].current_volume == 100,
                    "Initial coded program stopped or changed its output volume",
                )
                if identifier not in initial_players:
                    initial_players[identifier] = deepcopy(player)
                else:
                    require(
                        player["item_id"] == initial_players[identifier]["item_id"],
                        "Initial coded program changed its original OwnTone item",
                    )
                npt, now = player.get("item_progress_ms"), time.monotonic()
                require(type(npt) is int and npt >= 0, "Music startup NPT is invalid")
                if identifier not in progress:
                    progress[identifier] = [now, npt, npt]
                first_at, first_npt, previous = progress[identifier]
                require(
                    npt >= previous and abs(npt - first_npt - (now - first_at) * 1000) < 1500,
                    "Original music NPT stalled, sought or moved backwards",
                )
                progress[identifier][2] = npt
        return len(indexes) == len(context.producers)

    await epoch.observed_wait(
        context, None, initial_pcm, "Epoch did not establish all actual final native PCM", 20
    )
    observer = SoakObserver(context, initial_pcm)
    context.observer = observer
    await observer.initialize()

    async def waveform_ready():
        await observer.check()
        ready = True
        for capture in context.captures.values():
            if capture.reference_pcm is None:
                ready = capture.establish_reference(group.program_pcm) and ready
        return ready

    await epoch.observed_wait(
        context, observer, waveform_ready, "Actual final PCM lost its complete unique first-second prefix", 10
    )
    for identifier, capture in context.captures.items():
        baseline = context.report["independent_capture_baselines"][identifier]
        displacement = (capture.music_first_pts_ns - start - context.declared_horizon_ns) / 1_000_000
        require(
            abs(displacement - baseline["capture_offset_ms"]) <= baseline["horizon_half_width_ms"],
            "Soak actual first music frame missed its immutable calendar/capture uncertainty",
        )
    began_mono, began_wall = time.monotonic_ns(), time.time_ns()
    context.evidence.update(
        body_started_monotonic_ns=began_mono,
        body_started_wall_ns=began_wall,
        initial_players=deepcopy(initial_players),
        first_music_origins={key: c.music_first_pts_ns for key, c in context.captures.items()},
    )
    next_window = began_mono + 10_000_000_000
    while time.monotonic_ns() - began_mono < BODY_SECONDS * 1_000_000_000:
        await observer.check()
        now = time.monotonic_ns()
        if now >= next_window:
            await record_window(context, observer)
            next_window += CADENCE_SECONDS * 1_000_000_000
            require(
                time.monotonic_ns() < next_window, "Soak timing analysis missed its bounded window cadence"
            )
        await asyncio.sleep(0.03)
    await observer.check()
    await record_window(context, observer)
    finished_mono, finished_wall = time.monotonic_ns(), time.time_ns()
    require_elapsed(
        began_mono,
        finished_mono,
        began_wall,
        finished_wall,
        {key: c.music_verified_frames for key, c in context.captures.items()},
    )
    final = {key: group.producer_status(handle) for key, handle in context.producers.items()}
    for value in final.values():
        require(
            value["available_ns"] == available
            and value["common_start_ns"] == start
            and value["first_pcm_frame"] == value["first_pcm_sequence"] == 0
            and value["first_pcm_native_presentation_ns"] == start
            and value["first_pcm_lateness_ns"] < SEND_LATE_NS
            and value["max_send_lateness_ns"] < SEND_LATE_NS
            and value["stage"] == "streaming"
            and value["finished"] is False
            and value["frames"] >= RATE * BODY_SECONDS
            and value["frames"] <= RATE * PRODUCER_SECONDS,
            "Soak producer lost exact first-frame calendar, continuous cadence or finite source bounds",
        )
    context.evidence.update(
        producer_final=deepcopy(final),
        body_finished_monotonic_ns=finished_mono,
        body_finished_wall_ns=finished_wall,
        elapsed_monotonic_ns=finished_mono - began_mono,
        elapsed_wall_ns=finished_wall - began_wall,
        minimum_verified_music_frames=RATE * BODY_SECONDS,
        lifetime_capture_evidence={key: c.poll() for key, c in context.captures.items()},
        music_calendar_verified=True,
        uninterrupted_music_verified=True,
        measured_signal_timeline_cleanup_passed=False,
        speech_exercised=False,
        speech_latency_performance_passed=False,
        speech_latency_performance_status="not_exercised",
        cold_utterance_completeness_passed=False,
        cold_utterance_completeness_status="not_exercised",
    )
    return observer


def require_elapsed(start_mono, end_mono, start_wall, end_wall, frames):
    require(
        all(type(value) is int and value > 0 for value in (start_mono, end_mono, start_wall, end_wall))
        and end_mono - start_mono >= BODY_SECONDS * 1_000_000_000
        and end_wall - start_wall >= BODY_SECONDS * 1_000_000_000
        and set(frames) == {A, B}
        and all(type(value) is int and value >= RATE * BODY_SECONDS for value in frames.values()),
        "Soak requires thirty real monotonic and wall minutes plus at least86.4M verified music frames per zone",
    )


async def record_window(context, observer):
    require(
        len(context.evidence["timing_windows"]) < MAX_WINDOWS,
        "Soak exceeded its bounded analysis-window count",
    )
    group = context.group
    await observer.check()
    end = min(c.previous_anchor for c in context.captures.values()) - 1_000_000_000
    start = end - WINDOW_SECONDS * 1_000_000_000
    snapshots = {key: c.snapshot(start, end) for key, c in context.captures.items()}
    row = {
        "passed": False,
        "start_monotonic_ns": start,
        "end_monotonic_ns": end,
        "window_seconds": WINDOW_SECONDS,
        "calendars": {},
        "lifetime": {key: s.lifetime for key, s in snapshots.items()},
    }
    # Preserve failed measurements before assertion so diagnostics cannot hide rejection.
    context.evidence["timing_windows"].append(row)
    series = {}
    for key, snapshot in snapshots.items():
        origin = context.evidence["common_presentation_ns"] + context.declared_horizon_ns
        first_frame = max(0, (start - origin) * RATE // 1_000_000_000 // 960 * 960)
        last_frame = (end - origin) * RATE // 1_000_000_000 + 960
        actual = await analysis(group.modulation, snapshot, start, end)
        reference = group.declared_capture(origin, first_frame, last_frame)
        declared = await analysis(group.modulation, reference, start, end)
        displacement = await analysis(
            group.align_series, declared, actual, maximum_delay_ms=None, search_ms=1000
        )
        row["calendars"][key] = {
            "alignment": displacement,
            "frame_continuity": snapshot.frame_continuity,
            "retained_block_range": snapshot.retained_block_range,
        }
        row["calendars"][key]["horizon"] = group.validate_horizon(
            displacement, context.report["independent_capture_baselines"][key]
        )
        series[key] = actual
        await observer.check()
    await analysis(
        group.record_group_alignment,
        row,
        [series[A], series[B]],
        frame_continuity={key: s.frame_continuity for key, s in snapshots.items()},
    )
    alignment = row["group_alignment"]
    initial = context.evidence["timing_windows"][0]["group_alignment"]["relative_offset_ms"]
    row["drift_from_initial_ms"] = alignment["relative_offset_ms"] - initial
    require(
        abs(row["drift_from_initial_ms"]) <= 2, "Soak relative group timing drifted beyond original2ms guard"
    )
    row["passed"] = True
    await observer.check()


def finalize_capture(context, identifier, capture):
    require(
        context.observer is not None and context.observer.stopped,
        "Soak lacks exact original observation stop boundary",
    )
    capture.poll()
    guard = context.pcm_guards[identifier]
    guard.check(live=False)
    artifact = retain_failed_capture(
        capture, context.temporary, f"soak-{context.phase.epoch_id}-{identifier}"
    )
    context.evidence.setdefault("tail", {}).setdefault("captures", {})[identifier] = {
        "capture": capture.poll(),
        "pcm": guard.evidence(),
        "retained_window": artifact,
    }
    if set(context.evidence["tail"]["captures"]) == {A, B}:
        context.evidence["tail"].update(
            both_captures_null=True,
            pre_stop_pcm_verified=True,
            frame_sequences_verified=True,
            teardown_labeled=True,
        )


def retain_failed_capture(capture, directory, identifier):
    # Preserve already observed bounded PCM even if a sticky content/metadata
    # rejection prevents another poll. Failure artifacts confer no success.
    require(
        isinstance(capture, streaming.StreamingEvidence),
        "Soak artifact requires its explicit bounded observer",
    )
    chunks, ats = tuple(capture.chunks), tuple(capture.captured_at)
    require(
        len(chunks) == len(ats)
        and sum(map(len, chunks)) <= streaming.MAX_BYTES
        and len(chunks) <= streaming.MAX_BLOCKS,
        "Soak retained failure window exceeded its declared bound",
    )
    path = directory / f"{identifier}-retained-final.pcm"
    digest = hashlib.sha256()
    with path.open("xb") as stream:
        for data in chunks:
            digest.update(data)
            stream.write(data)
    timings = path.with_suffix(".timestamps.json")
    atomic_json(
        timings,
        [
            {
                "absolute_pts_monotonic_ns": capture.absolute[at],
                "frames": len(data) // 4,
                "metadata": deepcopy(capture.buffer_metadata[at]),
            }
            for at, data in zip(ats, chunks, strict=True)
        ],
    )
    return {
        "scope": "Bounded retained window only; lifetime counts/digests separate; failure artifact grants no success",
        "path": str(path),
        "timing_path": str(timings),
        "sha256": digest.hexdigest(),
        "blocks": len(chunks),
        "bytes": sum(map(len, chunks)),
        "retained_block_range": [capture.chunks.first, len(capture.chunks)],
        "lifetime_frame_continuity": deepcopy(capture.sequence.evidence()),
        "lifetime": capture.streaming_receipt(),
        "sticky_error": capture.error,
    }
