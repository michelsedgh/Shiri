"""Pure zone music ownership policy; no receiver or runtime is integrated yet.

Adapters serialize admission requests through one zone actor. The newest
request wins; an identical request from the current producer is idempotent.
Admission session_id must identify the exact producer/negotiation incarnation,
not merely a reusable native ID. Adapters combine a native ID with a fresh
connection generation, keeping it stable only for replay of that admission.
The returned token must accompany that producer's end/volume callbacks and
all grant/revoke actions. Session IDs alone are insufficient after reuse.

Persist the epoch high-water mark before acknowledging grants, and begin a
new actor incarnation after restart. Do not restore an old live owner from
saved intent. A new incarnation prevents old callbacks and queued actions
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
Gain actions affect the mix, never phone playback or source transport. TTS
must not pause, seek, restart, reconnect or disconnect the music producer.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter, field_validator, model_validator

from .domain import Conflict, StrictModel, ValidationIssue

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


class OverlayToken(SessionToken):
    """Separate speech generation; it never becomes a music source token."""


class SourceState(ZoneIdentity):
    incarnation: str = Field(default_factory=lambda: str(uuid4()))
    epoch: int = Field(default=0, ge=0)
    owner: SourceToken | None = None
    volume: int = Field(default=50, ge=0, le=100)
    overlay: OverlayToken | None = None
    last_overlay: OverlayToken | None = None
    tts_epoch: int = Field(default=0, ge=0)
    music_gain: float = Field(default=1.0, ge=0, le=1, allow_inf_nan=False)

    _incarnation = field_validator("incarnation")(_uuid)

    @property
    def tts_session_id(self):
        return self.overlay.session_id if self.overlay else None

    @model_validator(mode="after")
    def consistent(self):
        if self.owner and (self.owner.zone_id != self.zone_id or self.owner.incarnation != self.incarnation or self.owner.epoch != self.epoch):
            raise ValueError("Live source token must match its zone, incarnation and latest epoch")
        if self.overlay and (self.overlay.zone_id != self.zone_id or self.overlay.incarnation != self.incarnation or self.overlay.epoch != self.tts_epoch):
            raise ValueError("Speech overlay token must match its zone, incarnation and speech epoch")
        if self.last_overlay and (self.last_overlay.zone_id != self.zone_id or self.last_overlay.incarnation != self.incarnation or self.last_overlay.epoch != self.tts_epoch):
            raise ValueError("Latest speech token must match its zone, incarnation and speech epoch")
        if self.overlay and self.overlay != self.last_overlay:
            raise ValueError("Active speech overlay must be the latest speech token")
        if self.overlay is None and self.music_gain != 1.0:
            raise ValueError("Music gain must be restored when no speech overlay owns it")
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


class TtsStarted(ZoneIdentity):
    event: Literal["tts_started"] = "tts_started"
    session_id: str
    duck_gain: float = Field(default=0.28, ge=0, le=1, allow_inf_nan=False)

    _identity = field_validator("session_id")(_session)


class TtsEnded(StrictModel):
    event: Literal["tts_ended"] = "tts_ended"
    token: OverlayToken


SourceEvent = Annotated[InputRequested | InputEnded | InputVolumeChanged | TtsStarted | TtsEnded, Field(discriminator="event")]
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


class SetMusicGain(StrictModel):
    action: Literal["set_music_gain"] = "set_music_gain"
    token: OverlayToken
    gain: float = Field(ge=0, le=1, allow_inf_nan=False)


SourceAction = GrantInput | RevokeInput | ApplySourceVolume | SetMusicGain


class SourceChange(StrictModel):
    state: SourceState
    accepted: bool
    reason: Literal["granted", "already_owner", "ended", "volume_changed", "overlay_started", "overlay_ended", "already_active", "stale"]
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
    if isinstance(action, ApplySourceVolume):
        return owns_input(state, token) and action.volume == state.volume
    return token == state.last_overlay and action.gain == state.music_gain


def reduce_source(state: SourceState, event: SourceEvent | dict) -> SourceChange:
    """Return the entire next state/actions without mutating input or doing I/O.

    Only InputRequested is admission authority; an asynchronous started/status
    callback must not be converted into a fresh admission request. Session IDs
    include producer generation and are case sensitive. Cross-zone messages
    are errors, never implicit routing.
    """
    state = SourceState.model_validate(state)
    event = EVENTS.validate_python(event)
    event_zone = event.token.zone_id if isinstance(event, (InputEnded, InputVolumeChanged, TtsEnded)) else event.zone_id
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
    if isinstance(event, (InputEnded, InputVolumeChanged)):
        if event.token != state.owner:
            return change("stale", accepted=False)
        if isinstance(event, InputEnded):
            return change("ended", owner=None, actions=(RevokeInput(token=event.token),))
        return change("volume_changed", volume=event.volume,
                      actions=(ApplySourceVolume(token=event.token, volume=event.volume),))
    if isinstance(event, TtsStarted):
        if state.tts_session_id:
            if state.tts_session_id != event.session_id:
                raise Conflict("Another speech overlay owns this zone")
            if state.music_gain != event.duck_gain:
                raise Conflict("Active speech overlay gain differs from the replayed request")
            return change("already_active")
        overlay = OverlayToken(zone_id=state.zone_id, incarnation=state.incarnation,
                               session_id=event.session_id, epoch=state.tts_epoch + 1)
        return change("overlay_started", overlay=overlay, last_overlay=overlay, tts_epoch=overlay.epoch, music_gain=event.duck_gain,
                      actions=(SetMusicGain(token=overlay, gain=event.duck_gain),))
    if event.token != state.overlay:
        return change("stale", accepted=False)
    return change("overlay_ended", overlay=None, music_gain=1.0,
                  actions=(SetMusicGain(token=event.token, gain=1.0),))
