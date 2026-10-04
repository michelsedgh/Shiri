"""Ownership/event-order regressions, independent of any receiver transport."""

from itertools import permutations
from uuid import uuid4

from pydantic import ValidationError
import pytest

from shiri.domain import ValidationIssue
from shiri.source import (
    ApplySourceVolume, GrantInput, InputEnded, InputRequested, InputVolumeChanged,
    RevokeInput, SourceState, SourceToken,
    owns_input, permits_action, reduce_source,
)


@pytest.fixture
def zone():
    return SourceState(zone_id=str(uuid4()))


def request(state, protocol="airplay2", session="phone-A"):
    return reduce_source(state, InputRequested(zone_id=state.zone_id, protocol=protocol, session_id=session))


def music(state):
    return state.owner, state.epoch, state.volume, state.incarnation


def test_newest_phone_or_protocol_wins_with_identity_bound_handoff(zone):
    first = request(zone)
    assert first.state.owner.epoch == 1
    assert first.actions == (GrantInput(token=first.state.owner),)
    second = request(first.state, "chromecast", "phone-B")
    assert second.state.owner.protocol == "chromecast" and second.state.owner.epoch == 2
    assert second.actions == (RevokeInput(token=first.state.owner), GrantInput(token=second.state.owner))
    third = request(second.state, "airplay2", "phone-C")
    assert third.state.owner.session_id == "phone-C" and third.state.epoch == 3
    assert music(zone) == (None, 0, 50, zone.incarnation), "Reducer mutated its input"


def test_current_request_replay_is_idempotent_without_disconnect_or_epoch_bump(zone):
    first = request(zone)
    replay = request(first.state)
    assert replay.accepted and replay.reason == "already_owner"
    assert replay.state == first.state and replay.actions == ()


def test_fresh_connection_generation_is_new_admission_despite_reused_native_id(zone):
    first = request(zone, session="native-ID:connection-A")
    second = request(first.state, session="native-ID:connection-B")
    assert second.state.epoch == first.state.epoch + 1
    assert second.actions == (RevokeInput(token=first.state.owner), GrantInput(token=second.state.owner))
    stale = reduce_source(second.state, InputEnded(token=first.state.owner))
    assert not stale.accepted and stale.state == second.state


def test_same_native_session_id_in_other_protocol_is_a_new_owner(zone):
    first = request(zone, "airplay2", "same-native-id")
    second = request(first.state, "chromecast", "same-native-id")
    assert second.state.epoch == 2 and second.state.owner.protocol == "chromecast"


def test_session_reuse_after_end_is_fenced_by_epoch(zone):
    first = request(zone)
    ended = reduce_source(first.state, InputEnded(token=first.state.owner))
    assert ended.state.owner is None and ended.state.epoch == 1
    following = request(ended.state)
    assert following.state.owner.session_id == first.state.owner.session_id
    assert following.state.owner.epoch == 2
    for event in (InputEnded(token=first.state.owner), InputVolumeChanged(token=first.state.owner, volume=0)):
        stale = reduce_source(following.state, event)
        assert not stale.accepted and stale.reason == "stale"
        assert stale.state == following.state and stale.actions == ()


def test_every_order_of_previous_disconnect_and_volume_callbacks_preserves_successor(zone):
    previous = request(zone).state
    successor = request(previous, "chromecast", "phone-B").state
    delayed = (InputEnded(token=previous.owner), InputVolumeChanged(token=previous.owner, volume=0),
               InputVolumeChanged(token=previous.owner, volume=100))
    for ordering in permutations(delayed):
        current = successor
        for event in ordering:
            result = reduce_source(current, event)
            assert not result.accepted and result.actions == ()
            current = result.state
        assert current == successor


def test_current_volume_is_acknowledged_and_does_not_change_owner_or_epoch(zone):
    active = request(zone).state
    updated = reduce_source(active, InputVolumeChanged(token=active.owner, volume=0))
    assert updated.accepted and updated.state.volume == 0
    assert updated.state.owner == active.owner and updated.state.epoch == active.epoch
    assert updated.actions == (ApplySourceVolume(token=active.owner, volume=0),)


def test_fresh_incarnation_rejects_late_callbacks_even_with_reused_native_id_and_epoch(zone):
    old = request(zone).state
    restarted = SourceState(zone_id=zone.zone_id, epoch=0)
    fresh = request(restarted).state
    assert fresh.owner.session_id == old.owner.session_id and fresh.owner.epoch == old.owner.epoch
    assert fresh.owner.incarnation != old.owner.incarnation
    for event in (InputEnded(token=old.owner), InputVolumeChanged(token=old.owner, volume=0)):
        stale = reduce_source(fresh, event)
        assert stale.state == fresh and not stale.accepted and not stale.actions
    # Normal recovery carries the previous durable high-water and increments it.
    recovered = request(SourceState(zone_id=zone.zone_id, epoch=old.epoch)).state
    assert recovered.epoch > old.epoch


def test_events_for_another_zone_cannot_route_implicitly(zone):
    another = SourceState(zone_id=str(uuid4()))
    foreign = request(another).state.owner
    events = [InputRequested(zone_id=another.zone_id, protocol="chromecast", session_id="B"),
              InputEnded(token=foreign), InputVolumeChanged(token=foreign, volume=0)]
    for event in events:
        with pytest.raises(ValidationIssue, match="another exact zone"):
            reduce_source(zone, event)
    assert zone.owner is None and zone.epoch == 0


@pytest.mark.parametrize("event", [
    {"event": "input_requested", "protocol": "alsa", "session_id": "A"},
    {"event": "input_requested", "protocol": "airplay2", "session_id": " A"},
    {"event": "input_requested", "protocol": "airplay2", "session_id": "A\x00"},
    {"event": "input_requested", "protocol": "airplay2", "session_id": "A", "command": "pause"},
])
def test_event_contract_rejects_unknown_protocols_fields_and_invalid_identity(zone, event):
    with pytest.raises(ValidationError):
        reduce_source(zone, {"zone_id": zone.zone_id, **event})


@pytest.mark.parametrize("volume", [True, "50", 50.5, -1, 101])
def test_volume_events_require_strict_bounded_integer(zone, volume):
    active = request(zone).state
    with pytest.raises(ValidationError):
        reduce_source(active, {"event": "input_volume_changed", "token": active.owner.model_dump(), "volume": volume})


def test_serialized_state_and_exact_events_preserve_tokens(zone):
    requested = {"event": "input_requested", "zone_id": zone.zone_id, "protocol": "chromecast", "session_id": "Cast-ID/UPPER"}
    active = reduce_source(zone, requested).state
    restored = SourceState.model_validate_json(active.model_dump_json())
    assert restored == active
    ended = reduce_source(restored, {"event": "input_ended", "token": active.owner.model_dump()})
    assert ended.state.owner is None and ended.state.epoch == active.epoch


def test_invalid_state_cannot_smuggle_another_zone_or_epoch_owner(zone):
    owner = request(zone).state.owner
    for change in ({"zone_id": str(uuid4())}, {"epoch": 2}, {"incarnation": str(uuid4())}):
        wrong = SourceToken.model_validate({**owner.model_dump(), **change})
        with pytest.raises(ValidationError):
            SourceState.model_validate({**zone.model_dump(), "owner": wrong, "epoch": 1})


def test_delayed_actions_and_every_old_pcm_write_are_fenced_after_takeover(zone):
    first = request(zone)
    assert permits_action(first.state, first.actions[0]) and owns_input(first.state, first.state.owner)
    successor = request(first.state, "chromecast", "phone-B")
    assert not permits_action(successor.state, first.actions[0])
    assert not owns_input(successor.state, first.state.owner)
    assert owns_input(successor.state, successor.state.owner)
    assert all(permits_action(successor.state, action) for action in successor.actions)
    assert not permits_action(successor.state, RevokeInput(token=successor.state.owner))
    restarted = SourceState(zone_id=zone.zone_id, epoch=successor.state.epoch)
    assert not any(permits_action(restarted, action) for action in successor.actions)


def test_queued_same_source_volume_does_not_replay_over_newer_state(zone):
    active = request(zone).state
    previous = reduce_source(active, InputVolumeChanged(token=active.owner, volume=65))
    newest = reduce_source(previous.state, InputVolumeChanged(token=active.owner, volume=0))
    assert not permits_action(newest.state, previous.actions[0])
    assert permits_action(newest.state, newest.actions[0])


def test_action_fences_also_reject_another_exact_zone(zone):
    another = request(SourceState(zone_id=str(uuid4())))
    assert not owns_input(zone, another.state.owner)
    assert not any(permits_action(zone, action) for action in another.actions)
