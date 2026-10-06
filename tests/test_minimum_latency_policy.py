"""Explicit minimum route candidates retain corrected shared P and admission."""
from itertools import permutations
from pathlib import Path
from uuid import UUID

import pytest

from shiri.domain import Room, SpeakerRef
from shiri.runtime.configuration import backend_configs
from shiri.runtime.latency import buffered_audio_advance_ms, latency_plan, room_buffer_ms, speaker_lead_ms
from shiri.runtime.native import NativeMixer
from shiri.runtime.system import RuntimeFailure
from test_native_audio import Writer


def room(offset=0, *, protocol="alsa", number=1, enabled=True, device="hw:CARD=KitchenDAC,DEV=0"):
    return Room(id=str(UUID(int=number)), slot=number%8, name=f"Exact room{number}", airplay_name=f"Exact room{number}",
        interface="eth0", enabled=enabled, local_audio_device=device if protocol in {"alsa","pulseaudio"} else None,
        speakers=[SpeakerRef(id="0" if protocol in {"alsa","pulseaudio"} else str(number),
                             name="Selected output",protocol=protocol,offset_ms=offset)])


@pytest.mark.parametrize("protocol,lead",[("alsa",40),("pulseaudio",250),("chromecast",250),("airplay1",500),("airplay2",500)])
def test_every_saved_offset_has_required_route_lead_and_cannot_reduce_a_nonfloor_buffer(protocol,lead):
    for offset in range(-2000,2001):
        definition=room(offset,protocol=protocol)
        value=room_buffer_ms(definition)
        assert lead <= value <= 2500
        assert value+offset >= lead
        assert value == lead or value-1+offset < lead
        assert speaker_lead_ms(definition.speakers[0]) == lead


@pytest.mark.parametrize("protocol,buffer,horizon",[("alsa",40,140),("pulseaudio",250,350),
                                                    ("chromecast",250,350),("airplay1",500,600),("airplay2",500,600)])
def test_zero_offset_qualification_plan_is_the_same_exact_production_policy(protocol,buffer,horizon):
    definition=room(protocol=protocol)
    plan=latency_plan([definition])
    assert plan.for_room(definition.id).output_buffer_ms == buffer and plan.common_horizon_ms == horizon
    assert latency_plan([definition]) == plan


def test_mixed_rooms_keep_original_source_p_one_common_h_and_exact_selected_offsets():
    definitions=[room(-2000),room(-2000,protocol="airplay2",number=2),room(0,protocol="chromecast",number=3)]
    plan=latency_plan(definitions)
    assert [item.output_buffer_ms for item in plan.rooms] == [2040,2500,250]
    assert plan.common_horizon_ms == 2600
    source_p=100000
    for definition in definitions:
        buffer=plan.for_room(definition.id).output_buffer_ms
        anchor=source_p+plan.common_horizon_ms-buffer
        assert anchor-source_p >= 100
        for speaker in definition.speakers:
            assert anchor+buffer+speaker.offset_ms == source_p+plan.common_horizon_ms+speaker.offset_ms
    assert all(latency_plan(order) == plan for order in permutations(definitions))
    definitions[1].speakers.clear()
    assert plan.for_room(definitions[1].id).output_buffer_ms == 2500


def test_corrected_mixed_endpoint_room_requires_every_exact_route_margin():
    local=room(-2000)
    local=local.model_copy(update={"speakers":[*local.speakers,
        SpeakerRef(id="2",name="AirPlay",protocol="airplay2",offset_ms=-2000)]})
    assert room_buffer_ms(local) == 2500
    assert latency_plan([local]).common_horizon_ms == 2600


@pytest.mark.parametrize('horizon,residual', [(140, 0), (350, 0), (599, 0), (600, 0), (601, 1), (2600, 2000)])
def test_buffered_source_alignment_is_continuous_and_preserves_downstream_lead(horizon, residual):
    phone_p = 100_000
    advance = buffered_audio_advance_ms(horizon)
    receiver_p = phone_p - advance
    assert receiver_p + horizon == phone_p + residual
    # Receivers in the same plan keep one final calendar despite different B.
    for buffer in (40, 250, 500, 2500):
        if buffer + 100 > horizon:
            continue
        release = receiver_p - 150
        input_target = receiver_p + horizon - buffer
        assert input_target - release >= 250
        assert input_target + buffer == phone_p + residual


def test_bluealsa_group_uses_one_local_candidate_and_disabled_rooms_do_not_charge_live_horizon():
    local=room(device="bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp")
    disabled=room(-2000,protocol="airplay2",number=2,enabled=False)
    plan=latency_plan([local,disabled])
    assert len(plan.rooms) == 1 and plan.common_horizon_ms == 140
    assert latency_plan([]).common_horizon_ms == 140


@pytest.mark.parametrize("invalid",[None,True,{},"rooms",iter([]),[object()]])
def test_minimum_candidate_keeps_bounded_exact_domain_admission(invalid):
    with pytest.raises(ValueError):
        latency_plan(invalid)


@pytest.mark.parametrize("change",[{"offset_ms":True},{"offset_ms":2001},{"protocol":"ALSA"}])
def test_minimum_route_helper_rejects_bypassed_speaker_validation(change):
    with pytest.raises(ValueError):
        speaker_lead_ms(room().speakers[0].model_copy(update=change))


def render(directory,definition,buffer=None):
    return backend_configs(definition,directory,{"interface":"receiver0"},
        all_receiver_names=[],password="private-test-only",audio_uid=1234,own_username="shiri-output-1",view_directory=Path("/run/shiri-worker"),
        output_buffer_ms=buffer)


@pytest.mark.parametrize("protocol,offset,buffer",[("alsa",0,40),("alsa",-2000,2040),
    ("chromecast",0,250),("chromecast",-2000,2250),("airplay2",0,500),("airplay2",-2000,2500)])
def test_native_explicit_profile_renders_exact_buffer_and_refuses_one_ms_less_before_file_creation(tmp_path,protocol,offset,buffer):
    definition=room(offset,protocol=protocol)
    _receiver,own=render(tmp_path/"accepted",definition,buffer)
    assert f"start_buffer_ms = {buffer}\n" in own.read_text()
    with pytest.raises(RuntimeFailure):
        render(tmp_path/"refused",definition,buffer-1)
    assert not (tmp_path/"refused").exists()


@pytest.mark.parametrize("protocol,floor",[("alsa",40),("pulseaudio",250),("chromecast",250),
                                         ("airplay1",500),("airplay2",500)])
def test_positive_saved_offset_cannot_admit_a_buffer_below_qualified_protocol_floor(tmp_path,protocol,floor):
    definition=room(2000,protocol=protocol)
    assert latency_plan([definition]).for_room(definition.id).output_buffer_ms == floor
    with pytest.raises(RuntimeFailure):
        render(tmp_path/"refused",definition,floor-1)
    assert not (tmp_path/"refused").exists()
    _receiver,own=render(tmp_path/"accepted",definition,floor)
    assert f"start_buffer_ms = {floor}\n" in own.read_text()




@pytest.mark.parametrize("buffer,horizon",[(39,140),(0,140),(True,140),(40,0),(40,39),(40,True)])
def test_mixer_rejects_insufficient_period_capacity_zero_horizon_and_invalid_types(buffer,horizon):
    with pytest.raises(ValueError):
        NativeMixer(Path("/unused"),writer=Writer(),output_buffer_ms=buffer,relay_delay_ns=horizon*1000000)


def test_native_mixer_accepts_two_production_periods_with_explicit100ms_timer_margin():
    mixer=NativeMixer(Path("/unused"),writer=Writer(),output_buffer_ms=40,relay_delay_ns=140000000)
    assert mixer.health()["output_buffer_ms"] == 40 and mixer.health()["timing_relay_delay_ms"] == 140


def test_native_mixer_default_and_native_config_default_use_real_local_buffers(tmp_path):
    mixer=NativeMixer(Path("/unused"),writer=Writer())
    assert mixer.output_buffer_ms == 40 and mixer.relay_delay_ns == 140_000_000
    _receiver, own=render(tmp_path/"native",room())
    assert "start_buffer_ms = 40\n" in own.read_text()


@pytest.mark.parametrize("buffer,accepted",[(39,False),(40,True),(500,True)])
def test_actual_audio_worker_cli_admits_two_period_native_candidate_and_refuses_less(monkeypatch,buffer,accepted):
    import sys
    from shiri.runtime import audio
    seen=[]
    async def run(args):
        seen.append((args.output_buffer_ms,args.relay_delay_ms))
    monkeypatch.setattr(audio,"run",run)
    monkeypatch.setattr(sys,"argv",["audio","--socket","/private/audio.sock","--room-dir","/private/room",
        "--room-id",str(UUID(int=1)),"--native-uid","1234","--own-url","http://127.0.0.1:3869",
        "--own-password-file","/private/password","--speech-socket","/private/speech.sock",
        "--speech-launch-generation","1234567890abcdef1234567890abcdef","--output-uid","1235",
        "--output-buffer-ms",str(buffer),"--relay-delay-ms",str(buffer+100)])
    if accepted:
        audio.main()
        assert seen == [(buffer,buffer+100)]
    else:
        with pytest.raises(SystemExit):
            audio.main()
        assert not seen
