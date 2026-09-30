import uuid

import pytest
from pydantic import ValidationError

from shiri.domain import Room, RoomCreate, RoomPatch, SpeakerRef, local_audio_device_key, speaker_key


def test_strict_inputs_reject_coercion_and_unknown_fields():
    for payload in ({"enabled": "false"}, {"enabled": 1}, {"volume": "50"}, {"volume": 50.5},
                    {"volume": True}, {"slot": 1}, {"speakers": []}):
        with pytest.raises(ValidationError):
            RoomPatch.model_validate(payload)
    with pytest.raises(ValidationError):
        SpeakerRef(id=1, name="Speaker", protocol="airplay2")


@pytest.mark.parametrize("value", ["", "01", "-1", "one", str(1 << 64)])
def test_backend_ids_are_canonical_unsigned_decimals(value):
    with pytest.raises(ValidationError):
        SpeakerRef(id=value, name="Speaker", protocol="airplay2")


@pytest.mark.parametrize("name", ["", "x\ny", "é" * 26, "Room %H", "Room %v"])
def test_airplay_names_cannot_change_during_receiver_expansion(name):
    with pytest.raises(ValidationError):
        RoomCreate(name="Room", airplay_name=name, interface="eth0")


def test_display_name_may_be_long_with_explicit_receiver_name():
    room = RoomCreate(name="A" * 80, airplay_name="Short receiver", interface="eth0")
    assert len(room.name) == 80
    with pytest.raises(ValidationError):
        RoomCreate(name="A" * 80, interface="eth0")


@pytest.mark.parametrize("interface", ["lo", "", "eth0;rm", "eth0\n", "v" * 16, "../eth0"])
def test_interface_is_a_constrained_lan_device_name(interface):
    with pytest.raises(ValidationError):
        RoomCreate(name="Room", interface=interface)


def test_external_binding_is_exact_case_sensitive_and_never_slugged():
    created = RoomCreate(name="Room", interface="eth0", nobly_room_id="House/Living:ROOM-1")
    assert created.nobly_room_id == "House/Living:ROOM-1"
    for value in ("", " room", "room ", "room\n", "room\x00"):
        with pytest.raises(ValidationError):
            RoomCreate(name="Room", interface="eth0", nobly_room_id=value)


def test_patch_null_has_explicit_unbinding_semantics():
    assert RoomPatch().model_fields_set == set()
    patch = RoomPatch(nobly_room_id=None, airplay_name=None, local_audio_device=None)
    assert patch.model_dump(exclude_unset=True) == {"nobly_room_id": None, "airplay_name": None, "local_audio_device": None}
    for key in ("name", "interface", "enabled", "volume", "duck_gain"):
        with pytest.raises(ValidationError):
            RoomPatch.model_validate({key: None})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -0.01, 1.01])
def test_duck_gain_is_finite_and_bounded(value):
    with pytest.raises(ValidationError):
        RoomPatch(duck_gain=value)


def test_speaker_identity_cannot_be_duplicated_by_changing_protocol():
    with pytest.raises(ValidationError):
        Room(id=str(uuid.uuid4()), slot=0, name="Room", airplay_name="Room", interface="eth0",
             speakers=[SpeakerRef(id="1", name="Speaker", protocol="airplay1"),
                       SpeakerRef(id="1", name="Speaker", protocol="chromecast")])


def test_local_audio_device_preserves_exact_alsa_uri_and_rejects_controls():
    device = "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp"
    assert RoomCreate(name="Room", interface="eth0", local_audio_device=device).local_audio_device == device
    with pytest.raises(ValidationError):
        RoomCreate(name="Room", interface="eth0", local_audio_device="default\x00")


def test_local_speaker_needs_an_explicit_physical_audio_device():
    with pytest.raises(ValidationError):
        Room(id=str(uuid.uuid4()), slot=0, name="Room", airplay_name="Room", interface="eth0",
             speakers=[SpeakerRef(id="0", name="Local Output", protocol="alsa")])


@pytest.mark.parametrize("device", [
    "default", "null", "file:/etc/shadow", "file:FILE=/etc/shiri/api-token", "pulse",
    "hw:../device", "hw:Card;exec", "hw:CARD=Card,DEV=0,FILE=/etc/shadow",
    "hw:{type file file /etc/shadow}", "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp,SRV=attacker",
    "bluealsa:DEV=not-a-mac,PROFILE=a2dp", " hw:Card", "hw:Card\n", "hw:Card,999", "hw:999", "hw:01", "hw:Card,01",
])
def test_local_audio_rejects_arbitrary_root_executed_plugins_and_aliases(device):
    with pytest.raises(ValidationError):
        RoomCreate(name="Room", interface="eth0", local_audio_device=device)
    with pytest.raises(ValidationError):
        RoomPatch(local_audio_device=device)


@pytest.mark.parametrize("equivalents", [
    ["hw:SpeakerA", "plughw:SpeakerA", "hw:CARD=SpeakerA", "hw:SpeakerA,0,0", "plughw:CARD=SpeakerA,DEV=0"],
    ["hw:SpeakerA,2,1", "plughw:CARD=SpeakerA,DEV=2,SUBDEV=1"],
    ["bluealsa:DEV=aa:bb:cc:dd:ee:ff,PROFILE=a2dp", "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp"],
])
def test_equivalent_local_routes_have_one_physical_ownership_identity(equivalents):
    speaker = SpeakerRef(id="0", name="Local", protocol="alsa")
    keys = {speaker_key(speaker, endpoint) for endpoint in equivalents}
    assert len(keys) == 1
    for endpoint in equivalents:
        assert RoomCreate(name="Room", interface="eth0", local_audio_device=endpoint).local_audio_device == endpoint


def test_distinct_hardware_devices_keep_distinct_keys():
    assert local_audio_device_key("hw:SpeakerA,0") != local_audio_device_key("hw:SpeakerA,1")
    assert local_audio_device_key("hw:SpeakerA,0,0") != local_audio_device_key("hw:SpeakerA,0,1")
    assert local_audio_device_key("hw:SpeakerA") != local_audio_device_key("hw:SpeakerB")


def test_numeric_card_aliases_remain_normalizable_for_explicit_audit_only():
    assert local_audio_device_key("hw:7") == local_audio_device_key("plughw:CARD=7,DEV=0,SUBDEV=0")


@pytest.mark.parametrize("device", [
    "hw:0", "hw:7,1,2", "plughw:7,0,0", "hw:CARD=7", "plughw:CARD=7,DEV=0,SUBDEV=2",
    "hw:CARD=255,DEV=255,SUBDEV=255",
])
@pytest.mark.parametrize("model", [RoomCreate, RoomPatch, Room])
def test_room_models_reject_numeric_cards_with_actionable_named_endpoint(device, model):
    values = {"local_audio_device": device}
    if model is not RoomPatch:
        values.update(name="Room", interface="eth0")
    if model is Room:
        values.update(id=str(uuid.uuid4()), slot=0, airplay_name="Room")
    with pytest.raises(ValidationError, match="Numeric ALSA CARD.*stable named CARD.*KitchenDAC"):
        model.model_validate(values)


@pytest.mark.parametrize("value", [-2001, 2001, "50", 1.5, True])
def test_calibration_offsets_are_strict_bounded_integers(value):
    with pytest.raises(ValidationError):
        SpeakerRef(id="1", name="Speaker", protocol="airplay2", offset_ms=value)
