"""Candidate latency arithmetic preserves a shared native presentation clock."""

from dataclasses import FrozenInstanceError
from itertools import permutations
from uuid import UUID

import pytest

from shiri.domain import Room, SpeakerRef
from shiri.runtime.latency import latency_plan, room_buffer_ms, speaker_lead_ms


def room(*offsets, number=1, enabled=True, revision=1, protocol="airplay2"):
    return Room(
        id=str(UUID(int=number)), slot=number % 8, name=f"Room {number}",
        airplay_name=f"Room {number}", interface="eth0", enabled=enabled, revision=revision,
        local_audio_device="hw:CARD=KitchenDAC,DEV=0" if protocol in {"alsa", "pulseaudio"} else None,
        speakers=[SpeakerRef(id=str(index + 1), name=f"Speaker {index + 1}",
                             protocol=protocol, offset_ms=offset) for index, offset in enumerate(offsets)],
    )


@pytest.mark.parametrize(("offset", "expected"), [
    (-2000, 2500), (-1999, 2499), (-1000, 1500), (-251, 751), (-250, 750),
    (-249, 749), (-1, 501), (0, 500), (1, 500), (2000, 500),
])
def test_compensation_margin_and_route_floor(offset, expected):
    definition = room(offset)
    assert room_buffer_ms(definition) == expected
    plan = latency_plan([definition])
    assert plan.for_room(definition.id).output_buffer_ms == expected
    assert plan.common_horizon_ms == expected + 100


@pytest.mark.parametrize("protocol,lead", [("airplay1", 500), ("airplay2", 500), ("alsa", 40),
                                                ("pulseaudio", 250), ("chromecast", 250)])
def test_all_admitted_offsets_have_their_route_lead_and_smallest_buffer(protocol, lead):
    # Independent exhaustive acceptance invariant, rather than mirroring max/min.
    previous = 2501
    for offset in range(-2000, 2001):
        buffer_ms = room_buffer_ms(room(offset, protocol=protocol))
        assert lead <= buffer_ms <= 2500
        assert buffer_ms + offset >= lead
        if buffer_ms > lead:
            assert buffer_ms - 1 + offset < lead
        assert buffer_ms <= previous
        previous = buffer_ms


def test_every_enabled_zone_uses_one_presentation_horizon_and_distinct_input_anchor():
    definitions = [room(0, 2000, number=1, revision=8), room(-2000, number=2), room(-1000, number=3)]
    plan = latency_plan(definitions)
    assert plan.common_horizon_ms == 2600
    assert plan.common_horizon_ns == 2_600_000_000
    native_presentation_ms = 100_000
    for definition, expected_buffer in zip(definitions, (500, 2500, 1500), strict=True):
        decision = plan.for_room(definition.id)
        assert decision.output_buffer_ms == expected_buffer
        assert decision.revision == definition.revision
        input_anchor_ms = native_presentation_ms + plan.common_horizon_ms - decision.output_buffer_ms
        assert input_anchor_ms - native_presentation_ms >= 100
        for speaker in definition.speakers:
            actual_final_ms = input_anchor_ms + decision.output_buffer_ms + speaker.offset_ms
            assert actual_final_ms == native_presentation_ms + plan.common_horizon_ms + speaker.offset_ms


def test_no_speakers_and_positive_only_offsets_do_not_inflate_latency():
    assert room_buffer_ms(room()) == 40
    assert room_buffer_ms(room(2000, 250)) == 500
    assert room_buffer_ms(room(2000, -1000, -2000, 0)) == 2500


def test_disabled_compensation_cannot_change_running_group_horizon():
    active = room(0, number=1)
    inactive = room(-2000, number=2, enabled=False)
    plan = latency_plan([active, inactive])
    assert plan.common_horizon_ms == 600
    assert [decision.room_id for decision in plan.rooms] == [active.id]
    with pytest.raises(KeyError):
        plan.for_room(inactive.id)
    enabled = inactive.model_copy(update={"enabled": True, "revision": 2})
    assert latency_plan([active, enabled]).common_horizon_ms == 2600
    assert plan.common_horizon_ms == 600


@pytest.mark.parametrize("definitions", [[], [room(-2000, enabled=False)]])
def test_empty_or_disabled_group_has_an_explicit_local_default(definitions):
    plan = latency_plan(definitions)
    assert plan.common_horizon_ms == 140
    assert plan.rooms == ()


def test_plans_are_order_independent_and_never_retain_mutable_speaker_authority():
    definitions = [room(0, number=1), room(-1000, number=2), room(-2000, number=3)]
    original = [definition.model_dump() for definition in definitions]
    plan = latency_plan(definitions)
    assert all(latency_plan(order) == plan for order in permutations(definitions))
    assert [definition.model_dump() for definition in definitions] == original
    definitions[2].speakers.clear()
    assert plan.for_room(definitions[2].id).output_buffer_ms == 2500
    assert latency_plan(definitions).common_horizon_ms == 1600
    with pytest.raises(FrozenInstanceError):
        plan.common_horizon_ms = 4
    with pytest.raises(FrozenInstanceError):
        plan.rooms[0].output_buffer_ms = 4


@pytest.mark.parametrize("offset", [True, False, -2001, 2001, 1.5, "-2000", None, float("nan"), float("inf")])
def test_bypassed_or_mutated_domain_validation_cannot_admit_bad_offsets(offset):
    definition = room(0)
    definition.speakers[0] = definition.speakers[0].model_copy(update={"offset_ms": offset})
    with pytest.raises(ValueError):
        room_buffer_ms(definition)
    with pytest.raises(ValueError):
        latency_plan([definition])


@pytest.mark.parametrize(("field", "value"), [
    ("enabled", 1), ("enabled", "true"), ("revision", True), ("revision", 0),
    ("id", "invalid"), ("id", str(UUID(int=10)).upper()), ("speakers", [object()]),
])
def test_room_identity_and_revision_are_validated_even_if_disabled(field, value):
    definition = room(number=10, enabled=False).model_copy(update={field: value})
    with pytest.raises(ValueError):
        latency_plan([definition])


@pytest.mark.parametrize("definitions", [True, None, {}, "rooms", b"rooms", iter([]), [object()]])
def test_policy_refuses_unbounded_or_non_domain_inputs(definitions):
    with pytest.raises(ValueError):
        latency_plan(definitions)


def test_room_and_identity_bounds_are_explicit():
    assert len(latency_plan([room(number=number) for number in range(1, 9)]).rooms) == 8
    with pytest.raises(ValueError, match="at most 8"):
        latency_plan([room(number=number) for number in range(1, 10)])
    definition = room()
    with pytest.raises(ValueError, match="unique room"):
        latency_plan([definition, definition.model_copy(update={"enabled": False, "revision": 2})])
    with pytest.raises(ValueError):
        room_buffer_ms(500)


@pytest.mark.parametrize("protocol,lead", [("airplay1", 500), ("airplay2", 500), ("alsa", 40),
                                           ("pulseaudio", 250), ("chromecast", 250)])
def test_per_speaker_admission_helper_is_strict_and_route_specific(protocol, lead):
    speaker = SpeakerRef(id="1", name="Exact endpoint", protocol=protocol, offset_ms=-2000)
    assert speaker_lead_ms(speaker) == lead
    for field, invalid in (("protocol", "AIRPLAY2"), ("protocol", True), ("offset_ms", True),
                           ("offset_ms", -2001), ("offset_ms", "-100")):
        with pytest.raises(ValueError):
            speaker_lead_ms(speaker.model_copy(update={field: invalid}))
    with pytest.raises(ValueError):
        speaker_lead_ms({"protocol": protocol})


@pytest.mark.parametrize("airplay", ["airplay1", "airplay2"])
@pytest.mark.parametrize("other", ["alsa", "pulseaudio", "chromecast"])
def test_all_offsets_in_mixed_room_satisfy_both_constraints_without_universal_inflation(airplay, other):
    # A local endpoint needs2040 for-2000; an AirPlay endpoint independently
    # crosses that floor only when its500ms lead requires it. Verify minimality
    # against every selected endpoint, rather than duplicating the max formula.
    for offset in range(-2000, 2001):
        definition = room(offset, protocol=airplay)
        definition = definition.model_copy(update={"speakers": [*definition.speakers,
            SpeakerRef(id="0" if other in {"alsa", "pulseaudio"} else "2", name="Local endpoint",
                       protocol=other, offset_ms=-2000)],
            "local_audio_device": "hw:CARD=KitchenDAC,DEV=0" if other in {"alsa", "pulseaudio"} else None})
        buffer_ms = room_buffer_ms(definition)
        lead = 40 if other == "alsa" else 250
        assert 2000 + lead <= buffer_ms <= 2500
        assert buffer_ms + offset >= 500
        assert buffer_ms - 2000 >= lead
        assert buffer_ms - 1 + offset < 500 or buffer_ms - 1 - 2000 < lead


def test_local_and_bluealsa_profiles_use_minimum_buffer_while_airplay_peer_uses_one_calendar():
    for device in ("hw:CARD=KitchenDAC,DEV=0", "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp"):
        local = room(-2000, protocol="alsa").model_copy(update={"local_audio_device": device})
        peer = room(0, number=2)
        plan = latency_plan([local, peer])
        assert plan.for_room(local.id).output_buffer_ms == 2040
        assert plan.for_room(peer.id).output_buffer_ms == 500
        assert plan.common_horizon_ms == 2140
