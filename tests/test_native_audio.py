import asyncio
from array import array
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from shiri.rpc import RpcError
from shiri.runtime.native import NativeController, NativeHandle, NativeMixer
from shiri.runtime.timing import Clock, FLAG_AIRPLAY2, Kind, Packet, TimingError, ZERO_UUID


class Writer:
    def __init__(self):
        self.owner = None
        self.packets = []
        self.written_bytes = self.dropped_bytes = 0
        self.reader_present = True
        self.closed = False

    def reset(self, owner):
        self.owner = owner

    def write(self, packet):
        assert (packet.incarnation, packet.session, packet.epoch, packet.generation) == self.owner
        self.packets.append(packet)
        self.written_bytes += len(packet.pcm)
        return True

    def close(self):
        self.closed = True


class Client:
    def __init__(self):
        self.requests = []
        self.block = None
        self.bad_ack = False

    async def request(self, method, path, *, json):
        self.requests.append((method,path,json))
        if self.block:
            await self.block.wait()
        return {**json,"generation":999} if self.bad_ack else dict(json)


def controller():
    writer = Writer()
    mixer = NativeMixer(Path('/unused'),writer=writer,now_ns=lambda:15_000_000_500)
    client = Client()
    return NativeController(str(uuid4()),mixer,client), writer, client


def begin():
    return Packet(Kind.BEGIN,uuid4().bytes,flags=FLAG_AIRPLAY2,group=uuid4().bytes)


def pcm(grant, *, value=1000, frames=480, **changes):
    return replace(replace(grant, kind=Kind.PCM, frames=frames, pcm=array('h',[value]*frames*2).tobytes(),
                   presentation_ns=10_150_000_000 + changes.get("frame_index",0)*1_000_000_000//48000, clock_sample_ns=10_000_000_000,
                   monotonic_before_ns=15_000_000_000, monotonic_after_ns=15_000_000_200,
                   clock=Clock.RAW), **changes)


class LateSpeech:
    def __init__(self, accepted=False):
        self.frames = []
        self.controls = []
        self.closed = False
        self.accepted = accepted

    def set_gain(self, gain):
        self.gain = gain

    def push(self, data, samples):
        self.frames.append((data, samples))
        return self.accepted  # Speech failure must leave the program intact.

    def control(self, active, gain):
        self.controls.append((active, gain))
        return False

    def health(self):
        return {"speech_mix": "owntone_player", "speech_output_error": "test_refusal"}

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_late_speech_delivery_failure_cannot_duck_retime_or_command_the_music_program():
    c, writer, client = controller()
    overlay = LateSpeech()
    c.mixer.speech_output = overlay
    c.mixer.relay_delay_ns = 1_000_000_000
    c.mixer.output_buffer_ms = 500
    await c.initialize()
    handle = NativeHandle(c)
    grant = await c.begin(begin(), handle)
    original = pcm(grant, value=2000)
    voice = array('h', [1200]*480).tobytes()
    with pytest.raises(RpcError):
        c.mixer.push_speech(voice, 480)
    c.mixer.tick(music_active=True, speech_active=True, duck_gain=0.2, elapsed=0.01)
    await c.message(original, handle)
    assert writer.packets[-1].pcm == original.pcm
    assert writer.packets[-1].presentation_ns == 16_150_000_100
    assert overlay.frames == [(voice, 480)] and overlay.controls == [(True, 0.2)]
    assert not c.mixer.speech and c.mixer._gain == 1
    assert len(client.requests) == 2 and not handle.closed
    assert c.mixer.health()['music_gain'] is None  # Backend gain needs output evidence.
    assert c.mixer.health()['timing_relay_delay_ms'] == 1000
    await c.close()
    c.mixer.close()  # AudioWorker owns mixer/channel teardown after the controller.
    assert overlay.closed


@pytest.mark.asyncio
async def test_late_idle_speech_starts_a_short_timed_silence_bed_without_the_music_relay_wait():
    c, writer, client = controller()
    overlay = LateSpeech(accepted=True)
    c.mixer.speech_output = overlay
    c.mixer.output_buffer_ms = 500
    await c.initialize()
    c.mixer.push_speech(array('h', [1200]*480).tobytes(), 480)
    c.mixer.tick(music_active=False, speech_active=True, duck_gain=0.2, elapsed=0.01)
    packet = writer.packets[-1]
    assert packet.pcm == bytes(3840) and packet.session == ZERO_UUID
    assert packet.presentation_ns-c.mixer.now_ns() == 600_000_000
    assert len(client.requests) == 1 and c.actor.snapshot()['owner'] is None
    await c.close()


@pytest.mark.asyncio
async def test_real_adapter_callbacks_preserve_timestamps_and_overlay_only_target_zone():
    a, wa, ca = controller()
    b, wb, cb = controller()
    await a.initialize()
    await b.initialize()
    ha, hb = NativeHandle(a), NativeHandle(b)
    group = uuid4().bytes
    pa = await a.begin(replace(begin(),group=group),ha)
    pb = await b.begin(replace(begin(),group=group),hb)
    await a.message(pcm(pa),ha)
    # Independent native connections observe the SAME RAW phone deadline at
    # different arrival times, with unrelated RTP origins. No receive-origin
    # clock is used to establish the shared presentation timeline.
    b.mixer.now_ns = lambda:15_050_000_500
    await b.message(pcm(pb,rtp=99999,clock_sample_ns=10_050_000_000,
                        monotonic_before_ns=15_050_000_000,monotonic_after_ns=15_050_000_200),hb)
    assert wa.packets[-1].presentation_ns == wb.packets[-1].presentation_ns == 15_290_000_100
    assert wa.packets[-1].group == wb.packets[-1].group == group
    # Distinct modeled transports need explicit per-speaker compensation;
    # backend timestamp preservation alone cannot prove physical Bluetooth sync.
    fast_delay,slow_delay,fast_compensation = 100_000_000,300_000_000,200_000_000
    assert wa.packets[-1].presentation_ns+fast_delay+fast_compensation == wb.packets[-1].presentation_ns+slow_delay
    token = ha.token
    a.mixer.push_speech(array('h',[1000]*1920).tobytes(),1920)
    a.mixer.tick(music_active=True,speech_active=True,duck_gain=.2,elapsed=.01)
    await a.message(pcm(pa,frames=1920,sequence=1,frame_index=480),ha)
    await b.message(pcm(pb,frames=1920,sequence=1,frame_index=480),hb)
    assert a.actor.snapshot()['owner'] == token.model_dump()
    assert ha.closed is False
    assert len(ca.requests) == len(cb.requests) == 2 # idle + grant, no overlay flush/player command
    aa, bb = array('h',wa.packets[-1].pcm), array('h',wb.packets[-1].pcm)
    assert aa[-1] == 1200 and bb[-1] == 1000
    assert wa.packets[-1].presentation_ns == wb.packets[-1].presentation_ns
    a.mixer.tick(music_active=True,speech_active=False,duck_gain=.2,elapsed=.01)
    for i in range(2,27):
        await a.message(pcm(pa,sequence=i,frame_index=2400+(i-2)*480),ha)
    assert a.mixer._gain == pytest.approx(1)
    assert len(ca.requests) == 2
    await a.close()
    await b.close()


@pytest.mark.asyncio
async def test_source_takeover_blocks_old_and_idle_final_writes_until_exact_backend_ack():
    c,w,client = controller()
    await c.initialize()
    old = NativeHandle(c)
    old_grant = await c.begin(begin(),old)
    await c.message(pcm(old_grant),old)
    next_handle = NativeHandle(c)
    client.block = asyncio.Event()
    admission = asyncio.create_task(c.begin(begin(),next_handle))
    for _ in range(20):
        if c.actor.snapshot()['transitioning']:
            break
        await asyncio.sleep(0)
    assert c.actor.snapshot()['transitioning']
    count = len(w.packets)
    assert c.mixer.accept(pcm(old_grant,sequence=1,frame_index=480),old.token) is False
    assert c.actor.write_idle(lambda:w.packets.append('bad')) is False
    assert len(w.packets) == count
    client.block.set()
    new_grant = await admission
    await asyncio.sleep(0)
    assert old.closed
    await c.message(pcm(new_grant),next_handle)
    with pytest.raises(TimingError):
        await c.message(replace(old_grant,kind=Kind.END),old)
    assert c.actor.snapshot()['owner']['session_id'] == new_grant.session_id
    assert w.packets[-1].session == new_grant.session
    await c.close()


@pytest.mark.asyncio
async def test_flush_generation_barrier_and_delayed_volume_end_cannot_affect_successor():
    c,w,client = controller()
    await c.initialize()
    h = NativeHandle(c)
    grant = await c.begin(begin(),h)
    token = h.token
    await c.message(pcm(grant),h)
    flushed = await c.message(replace(grant,kind=Kind.FLUSH,generation=2),h)
    assert h.token == token
    assert client.requests[-1][2]['epoch'] == token.epoch
    assert client.requests[-1][2]['generation'] == 2
    for kind in (Kind.PCM,Kind.VOLUME,Kind.END):
        packet = pcm(grant) if kind is Kind.PCM else replace(grant,kind=kind)
        with pytest.raises(TimingError):
            await c.message(packet,h)
    await c.message(pcm(flushed),h)
    await c.message(replace(flushed,kind=Kind.VOLUME,frames=64),h)
    event = c.health()['native_volume_events'][0]
    assert event['generation'] == 2 and event['token'] == token.model_dump()
    assert event['volume'] == 64
    await c.message(replace(flushed,kind=Kind.VOLUME,frames=65),h)
    await c.message(replace(flushed,kind=Kind.VOLUME,frames=66),h)
    assert [e['volume'] for e in c.events] == [64,66]
    c.acknowledge_volume(event['event_id'])
    assert [e['volume'] for e in c.events] == [66]
    await c.close()


@pytest.mark.asyncio
async def test_bad_backend_ack_fails_closed_and_no_packet_can_reopen_route():
    c,w,client = controller()
    await c.initialize()
    client.bad_ack = True
    h = NativeHandle(c)
    with pytest.raises(TimingError):
        await c.begin(begin(),h)
    assert c.actor.snapshot()['ready'] is False
    assert c.actor.write_idle(lambda:w.packets.append('bad')) is False
    assert not w.packets
    await c.close()


@pytest.mark.asyncio
async def test_idle_short_speech_uses_zero_music_identity_and_no_extra_backend_commands():
    c,w,client = controller()
    await c.initialize()
    c.mixer.push_speech(array('h',[1200]*480).tobytes(),480)
    c.mixer.tick(music_active=False,speech_active=True,duck_gain=.2,elapsed=.01)
    assert len(w.packets) == 1
    assert w.packets[0].session == ZERO_UUID and w.packets[0].epoch == 0
    assert c.actor.snapshot()['owner'] is None
    assert len(client.requests) == 1
    assert array('h',w.packets[0].pcm)[:960] == array('h',[1200]*960)
    await c.close()


@pytest.mark.asyncio
async def test_cancelled_quiesce_closes_exact_connection_but_never_a_new_handle():
    class Connection:
        closed = False
        def close(self): self.closed = True
    c,_,_ = controller()
    await c.initialize()
    connection = Connection()
    old = NativeHandle(c,connection)
    await c.begin(begin(),old)
    token = old.token
    newer = NativeHandle(c,Connection())
    await c.begin(begin(),newer)
    await old.quiesce(token)
    cancelled = asyncio.create_task(old.quiesce(token))
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    old.abort(token)  # actor retirement completion also handles cancellation-before-start
    assert connection.closed and not newer.connection.closed
    old.abort(newer.token)
    assert not newer.connection.closed
    await c.close()


@pytest.mark.asyncio
async def test_native_voice_queue_bounded_and_malformed_clock_does_not_consume_speech():
    c,w,_ = controller()
    await c.initialize()
    h = NativeHandle(c)
    grant = await c.begin(begin(),h)
    data = array('h',[500]*9600).tobytes()
    c.mixer.push_speech(data,9600)
    c.mixer.push_speech(data,9600)
    assert len(c.mixer.speech) == 12000 and c.mixer.speech_dropped_frames == 7200
    with pytest.raises(TimingError):
        await c.message(pcm(grant,clock_sample_ns=0),h)
    assert len(c.mixer.speech) == 12000 and not w.packets
    await c.close()


@pytest.mark.asyncio
async def test_volume_revision_captured_before_admission_wait_and_only_monotonic_control_updates():
    c,_,_ = controller()
    await c.initialize()
    h = NativeHandle(c)
    grant = await c.begin(begin(),h)
    original = c.actor.volume_native
    entered,release = asyncio.Event(),asyncio.Event()
    async def delayed(token,volume):
        entered.set()
        await release.wait()
        return await original(token,volume)
    c.actor.volume_native=delayed
    pending=asyncio.create_task(c.message(replace(grant,kind=Kind.VOLUME,frames=40),h))
    await entered.wait()
    assert c.control_intent(9)=={'revision':9}
    assert c.control_intent(2)=={'revision':9}
    release.set()
    await pending
    assert c.events[0]['base_revision']==1
    with pytest.raises(Exception,match='Control revision'):
        c.control_intent(True)
    await c.close()


@pytest.mark.asyncio
async def test_volume_bridge_retains_exact_identity_until_durable_ack_and_coalesces_bounded_queue():
    c,_,_ = controller()
    await c.initialize()
    h = NativeHandle(c)
    grant = await c.begin(begin(),h)
    await c.message(replace(grant,kind=Kind.VOLUME,frames=40),h)
    first = c.events[0].copy()
    entered,release,completed=asyncio.Event(),asyncio.Event(),asyncio.Event()
    calls=[]
    async def rpc(path,operation,payload,timeout):
        assert path=='/private/events.sock' and operation=='native-volume' and timeout==2
        assert set(payload)=={'launch_generation','event_id','base_revision','token','generation','volume'}
        calls.append(payload.copy())
        if len(calls)==1:
            entered.set()
            await release.wait()
            raise TimeoutError('ACK lost')
        if len(calls)==2:
            return {'ok':True,'durable':True,'acknowledged':True,'accepted':True,'event_id':str(uuid4())}
        if payload['volume']==60:
            completed.set()
        return {'ok':True,'durable':True,'acknowledged':True,'accepted':False,'event_id':payload['event_id']}
    await c.start_volume_bridge('/private/events.sock','a'*32,rpc=rpc,retry_seconds=.001)
    await entered.wait()
    await c.message(replace(grant,kind=Kind.VOLUME,frames=50),h)
    await c.message(replace(grant,kind=Kind.VOLUME,frames=60),h)
    assert [e['volume'] for e in c.events]==[40,60]
    release.set()
    await asyncio.wait_for(completed.wait(),1)
    for _ in range(20):
        if not c.events:
            break
        await asyncio.sleep(0)
    assert not c.events
    assert [e['event_id'] for e in calls[:3]]==[first['event_id']]*3
    assert calls[-1]['volume']==60 and c.volume_bridge_error is None
    await c.close()
    assert c._volume_task.done()


@pytest.mark.asyncio
async def test_cancelled_volume_forwarder_preserves_unacknowledged_event_and_music_owner():
    c,_,_ = controller()
    await c.initialize()
    h=NativeHandle(c)
    grant=await c.begin(begin(),h)
    await c.message(replace(grant,kind=Kind.VOLUME,frames=20),h)
    token=h.token
    started=asyncio.Event()
    async def blocked(*args,**kwargs):
        started.set()
        await asyncio.Event().wait()
    await c.start_volume_bridge('/private/events.sock','b'*32,rpc=blocked)
    await started.wait()
    c._volume_task.cancel()
    await asyncio.gather(c._volume_task,return_exceptions=True)
    assert len(c.events)==1 and c.actor.owns(token) and not h.closed
    await c.close()


@pytest.mark.asyncio
async def test_cancelled_pending_admission_synchronously_closes_exact_candidate_connection():
    class Connection:
        closed=False
        def close(self):
            self.closed=True
    c,_,client=controller()
    await c.initialize()
    connection=Connection()
    handle=NativeHandle(c,connection)
    client.block=asyncio.Event()
    admission=asyncio.create_task(c.begin(begin(),handle))
    for _ in range(30):
        if handle.token is not None:
            break
        await asyncio.sleep(0)
    assert handle.token is not None
    admission.cancel()
    with pytest.raises(asyncio.CancelledError):
        await admission
    for _ in range(30):
        if connection.closed:
            break
        await asyncio.sleep(0)
    assert connection.closed and handle.closed
    assert c.actor.snapshot()['ready'] is False
    await c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('newer_ui',[False,True])
async def test_latest_native_volume_survives_flush_race_using_original_revision_without_overwriting_ui(newer_ui):
    c,_,client=controller()
    await c.initialize()
    h=NativeHandle(c)
    grant=await c.begin(begin(),h)
    await c.message(replace(grant,kind=Kind.VOLUME,frames=33),h)
    first=c.events[0].copy()
    waiting,release,completed=asyncio.Event(),asyncio.Event(),asyncio.Event()
    requests=[]
    backend_volume=80 if newer_ui else 50
    async def rpc(path,op,event,timeout):
        nonlocal backend_volume
        requests.append(event.copy())
        if len(requests)==1:
            waiting.set()
            await release.wait()
        # The actual backend tests independently reject old generations; this
        # bridge double models that fenced response and revision admission.
        accepted=event['generation']==h.generation and event['base_revision']==c.control_revision
        if accepted:
            backend_volume=event['volume']
        if len(requests)==2:
            completed.set()
        return {'ok':True,'durable':True,'acknowledged':True,'accepted':accepted,'event_id':event['event_id']}
    await c.start_volume_bridge('/private/events.sock','d'*32,rpc=rpc,retry_seconds=.001)
    await waiting.wait()
    if newer_ui:
        c.control_intent(9)
    flushed=await c.message(replace(grant,kind=Kind.FLUSH,generation=2),h)
    assert flushed.generation==2 and len(client.requests)==3 # only idle/grant/native FLUSH
    assert [event['generation'] for event in c.events]==[1,2]
    assert c.events[1]['event_id']!=first['event_id']
    assert c.events[1]['base_revision']==first['base_revision']==1
    release.set()
    await asyncio.wait_for(completed.wait(),1)
    for _ in range(20):
        if not c.events:
            break
        await asyncio.sleep(0)
    assert not c.events
    assert backend_volume==(80 if newer_ui else 33)
    assert requests[0]['generation']==1 and requests[1]['generation']==2
    assert c.actor.owns(h.token) and not h.closed
    await c.close()
