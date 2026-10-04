"""Pure music ownership policy used by each native zone actor.

Adapters serialize admission requests through one zone actor. The newest
request wins; an identical request from the current producer is idempotent.
Admission session_id must identify the exact producer/negotiation incarnation,
not merely a reusable native ID. Adapters combine a native ID with a fresh
connection generation, keeping it stable only for replay of that admission.
The returned token must accompany that producer's end/volume callbacks and
all grant/revoke actions. Session IDs alone are insufficient after reuse.

Begin a fresh actor incarnation after restart. Do not restore an old live
owner or incarnation from saved intent. If an adapter retains an incarnation
across a crash, it must durably persist its epoch high-water mark before
acknowledging grants. A fresh incarnation prevents old callbacks and queued actions
from matching a fresh session even if native IDs or epochs are reused.

Commit the returned state before applying actions in actor order. Revoke only
its exact token; grant/apply-volume only if their token remains current. Before
new PCM is admitted, acknowledge quiescence of the old route or atomically
fence it and discard its buffered PCM. Gate every PCM write by its token:
stopping a previous producer may be slow, and media may precede its admission
callback. The reducer alone does not make physical takeover atomic.
Use permits_action immediately before asynchronous effects and owns_input at
each media write, under the same zone actor/lock. Effects on a backend without
its own token fence must remain serialized through bounded acknowledgment;
checking only at initiation cannot prevent a reordered late write. A revoke
still requires an
adapter handle bound to that exact old token, never "current AirPlay source".
Speech ownership and ducking belong to the audio worker and native mixer.
They do not participate in this music-source reducer.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .domain import StrictModel, ValidationIssue

InputProtocol = Literal["airplay2", "chromecast"]


def _uuid(value: str) -> str:
    if str(UUID(value)) != value:
        raise ValueError("Zone/incarnation identity must be a canonical lowercase UUID")
    return value


def _session(value: str) -> str:
    if not value or len(value) > 128 or value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Session identity must be exact, bounded and contain no control characters")
    return value


class ZoneIdentity(StrictModel):
    zone_id: str

    _zone = field_validator("zone_id")(_uuid)


class SessionToken(ZoneIdentity):
    incarnation: str
    session_id: str
    epoch: int = Field(ge=1)

    _incarnation = field_validator("incarnation")(_uuid)
    _identity = field_validator("session_id")(_session)


class SourceToken(SessionToken):
    protocol: InputProtocol


class SourceState(ZoneIdentity):
    incarnation: str = Field(default_factory=lambda: str(uuid4()))
    epoch: int = Field(default=0, ge=0)
    owner: SourceToken | None = None
    volume: int = Field(default=50, ge=0, le=100)
    _incarnation = field_validator("incarnation")(_uuid)

    @model_validator(mode="after")
    def consistent(self):
        if self.owner and (self.owner.zone_id != self.zone_id or self.owner.incarnation != self.incarnation or self.owner.epoch != self.epoch):
            raise ValueError("Live source token must match its zone, incarnation and latest epoch")
        return self


class InputRequested(ZoneIdentity):
    event: Literal["input_requested"] = "input_requested"
    protocol: InputProtocol
    session_id: str

    _identity = field_validator("session_id")(_session)


class InputEnded(StrictModel):
    event: Literal["input_ended"] = "input_ended"
    token: SourceToken


class InputVolumeChanged(StrictModel):
    event: Literal["input_volume_changed"] = "input_volume_changed"
    token: SourceToken
    volume: int = Field(ge=0, le=100)


SourceEvent = Annotated[InputRequested | InputEnded | InputVolumeChanged, Field(discriminator="event")]
EVENTS = TypeAdapter(SourceEvent)


class GrantInput(StrictModel):
    action: Literal["grant_input"] = "grant_input"
    token: SourceToken


class RevokeInput(StrictModel):
    action: Literal["revoke_input"] = "revoke_input"
    token: SourceToken


class ApplySourceVolume(StrictModel):
    action: Literal["apply_source_volume"] = "apply_source_volume"
    token: SourceToken
    volume: int = Field(ge=0, le=100)


SourceAction = GrantInput | RevokeInput | ApplySourceVolume


class SourceChange(StrictModel):
    state: SourceState
    accepted: bool
    reason: Literal["granted", "already_owner", "ended", "volume_changed", "stale"]
    actions: tuple[SourceAction, ...] = ()


def owns_input(state: SourceState, token: SourceToken) -> bool:
    """PCM admission fence; check each write, not only its start notification."""
    return state.owner == token


def permits_action(state: SourceState, action: SourceAction) -> bool:
    """Reject queued effects made obsolete by newer state; this performs no I/O.

    The caller must serialize this check and the effect's initiation. Granting
    audible PCM still requires the quiescence/fencing barrier described above.
    Await un-fenced backend effects in order through their bounded completion.
    """
    token = action.token
    if token.zone_id != state.zone_id or token.incarnation != state.incarnation:
        return False
    if isinstance(action, RevokeInput):
        return token != state.owner and token.epoch <= state.epoch
    if isinstance(action, GrantInput):
        return owns_input(state, token)
    return owns_input(state, token) and action.volume == state.volume


def reduce_source(state: SourceState, event: SourceEvent | dict) -> SourceChange:
    """Return the entire next state/actions without mutating input or doing I/O.

    Only InputRequested is admission authority; an asynchronous started/status
    callback must not be converted into a fresh admission request. Session IDs
    include producer generation and are case sensitive. Cross-zone messages
    are errors, never implicit routing.
    """
    state = SourceState.model_validate(state)
    event = EVENTS.validate_python(event)
    event_zone = event.token.zone_id if isinstance(event, (InputEnded, InputVolumeChanged)) else event.zone_id
    if event_zone != state.zone_id:
        raise ValidationIssue("Source event belongs to another exact zone")

    def change(reason, *, accepted=True, actions=(), **updates):
        next_state = SourceState.model_validate({**state.model_dump(), **updates}) if updates else state
        return SourceChange(state=next_state, accepted=accepted, reason=reason, actions=actions)

    if isinstance(event, InputRequested):
        if state.owner and (state.owner.protocol, state.owner.session_id) == (event.protocol, event.session_id):
            return change("already_owner")
        owner = SourceToken(zone_id=state.zone_id, incarnation=state.incarnation,
                            protocol=event.protocol, session_id=event.session_id, epoch=state.epoch + 1)
        actions = ((RevokeInput(token=state.owner),) if state.owner else ()) + (GrantInput(token=owner),)
        return change("granted", owner=owner, epoch=owner.epoch, actions=actions)
    if event.token != state.owner:
        return change("stale", accepted=False)
    if isinstance(event, InputEnded):
        return change("ended", owner=None, actions=(RevokeInput(token=event.token),))
    return change("volume_changed", volume=event.volume,
                  actions=(ApplySourceVolume(token=event.token, volume=event.volume),))
