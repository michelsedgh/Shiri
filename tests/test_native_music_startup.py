"""Private opt-in startup policy and actual native callbacks; no units/devices."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from shiri.domain import Room, SpeakerRef
from shiri.runtime.broker import Broker
from shiri.runtime.native import NativeHandle
from shiri.runtime.timing import Kind, Packet
from shiri.runtime.latency import latency_plan
from shiri.runtime.system import RuntimeFailure
from test_native_audio import controller

pytest.importorskip('numpy')
pytest.importorskip('aiortc')
ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('music_actual_group', ROOT/'tests/linux/check_native_grouping.py')
group = importlib.util.module_from_spec(spec)
spec.loader.exec_module(group)
music = group.load_music_module()


def definitions():
    return [Room(id=identifier, slot=slot, name=f'Music{slot}', airplay_name=f'Music{slot}',
        interface='fixture0', enabled=True, local_audio_device=f'hw:CARD=Loopback,DEV={device},SUBDEV=7',
        speakers=[SpeakerRef(id='0', name='Local', protocol='alsa')])
        for identifier, slot, device in ((group.A,6,1),(group.B,7,0))]


def healthy():
    return {'ready':True,'error':None,'source':{'ready':True,'owner':None},'native_blocks':0,
            'timing_relay_delay_ms':750,'output_buffer_ms':500}


def test_private_planner_reaches_both_actual_broker_seams_without_mutating_global_policy():
    original = Broker.reconcile.__globals__['latency_plan']
    candidate = music.broker_class(group.IsolatedBroker)
    rooms = definitions()
    assert candidate.reconcile.__globals__['latency_plan'](rooms).common_horizon_ms == 750
    assert candidate.set_outputs.__globals__['latency_plan'](rooms).common_horizon_ms == 750
    assert candidate.reconcile.__code__ is Broker.reconcile.__code__
    assert candidate.set_outputs.__code__ is Broker.set_outputs.__code__
    assert Broker.reconcile.__globals__['latency_plan'] is original is latency_plan
    assert Broker.set_outputs.__globals__['latency_plan'] is original
    assert latency_plan(rooms).common_horizon_ms == 140
    assert music.candidate_plan(rooms).for_room(group.A).output_buffer_ms == 500
    assert candidate.reconcile.__globals__['room_buffer_ms'](rooms[0]) == 500
    assert candidate.set_outputs.__globals__['room_buffer_ms'](rooms[0]) == 500
    assert candidate._material.__globals__['room_buffer_ms'](rooms[0]) == 500
    assert candidate._start_room.__globals__['backend_configs'].keywords == {'minimum_latency':False}


@pytest.mark.parametrize('change', ['offset','network_route','other_room','duplicate_route','disabled_b','empty_b'])
def test_prebegin_final_plan_rejects_changed_selection_room_or_offset(change):
    rooms = definitions()
    if change == 'offset':
        rooms[0] = rooms[0].model_copy(update={'speakers':[rooms[0].speakers[0].model_copy(update={'offset_ms':1})]})
    elif change == 'network_route':
        rooms[0] = rooms[0].model_copy(update={'speakers':[SpeakerRef(id='1',name='Wifi',protocol='airplay2')]})
    elif change == 'other_room':
        rooms[0] = rooms[0].model_copy(update={'id':str(uuid4())})
    elif change == 'duplicate_route':
        rooms[0] = rooms[0].model_copy(update={'speakers':[rooms[0].speakers[0],SpeakerRef(id='1',name='Additional',protocol='airplay2')]})
    elif change == 'disabled_b':
        rooms[1] = rooms[1].model_copy(update={'enabled':False})
    else:
        rooms[1] = rooms[1].model_copy(update={'speakers':[]})
    with pytest.raises(RuntimeFailure):
        music.plan_receipt(music.Phase(str(uuid4()),0,'native'), rooms,
                           {group.A:healthy(),group.B:healthy()},before_pcm=True)


@pytest.mark.parametrize('key,value', [('ready',False),('error','fault'),('error','missing'),
    ('source',{}),('source',{'ready':True}),('source',{'ready':True,'owner':{}}),
    ('native_blocks',True),('native_blocks',1),('timing_relay_delay_ms',1000),
    ('timing_relay_delay_ms',750.0),('output_buffer_ms',True),('output_buffer_ms',2250)])
def test_actual_worker_must_agree_before_any_original_receiver_stop(key,value):
    health = healthy()
    if value == 'missing':
        health.pop(key)
    else:
        health[key] = value
    with pytest.raises(RuntimeFailure):
        music.require_idle_worker(health)


@pytest.mark.parametrize('mutant', ['missing_lab','wrong_h','wrong_b','other_type','nonzero_start','wrong_lead','other_duration'])
def test_receiver_replacement_admission_requires_the_exact_frozen_candidate(mutant):
    frozen = group.FrozenWorkerTiming(750_000_000,tuple((key,500) for key in sorted((group.A,group.B))))
    values = [definitions()[0],90,0,150_000_000,frozen,group.FrozenWorkerTiming]
    lab = object()
    if mutant == 'missing_lab':
        lab = None
    elif mutant == 'wrong_h':
        values[4] = frozen._replace(horizon_ns=1_000_000_000)
    elif mutant == 'wrong_b':
        values[4] = frozen._replace(buffers_ms=((group.A,500),(group.B,750)))
    elif mutant == 'other_type':
        values[4] = tuple(frozen)
    elif mutant == 'nonzero_start':
        values[2] = 1
    elif mutant == 'wrong_lead':
        values[3] = 220_000_000
    else:
        values[1] = 480
    with pytest.raises(RuntimeFailure):
        music.admit_launch(*values,lab=lab)


@pytest.mark.parametrize('change', [{'generation':True},{'generation':3},{'action':'run'},
    {'available_ns':0},{'available_ns':True},{'common_start_ns':1},{'unrecognized':1}])
def test_one_integer_calendar_has_exact150ms_native_lead(change):
    action = {'generation':2,'action':'begin','available_ns':15_000_000_001,
              'common_start_ns':15_150_000_001}
    assert music.calendar(action) == (15_000_000_001,15_150_000_001)
    with pytest.raises(RuntimeFailure):
        music.calendar({**action,**change})


class ProducerDriver:
    """Actual producer/native actor/mixer. OS credentials and transport mocked."""
    def __init__(self, monkeypatch, tmp_path, *, delay=0, reply_change=None, calendar_change=None):
        self.c, self.writer, self.client = controller()
        self.ns = 15_000_000_000
        self.c.mixer.now_ns = lambda:self.ns
        self.c.mixer.relay_delay_ns = 750_000_000
        self.c.mixer.output_buffer_ms = 500
        self.handle = NativeHandle(self.c)
        self.stop = None
        self.sent, self.connections, self.closed = [], [], 0
        self.delay, self.reply_change = delay, reply_change
        self.command, self.status = tmp_path/'command.json',tmp_path/'status.json'
        self.command.write_text(json.dumps({'generation':1,'action':'wait'}))
        self.config = {'music_startup':deepcopy(music.PRODUCER_PROFILE),'uid':1234,'gid':1235,
            'status':str(self.status),'command':str(self.command),'socket':'/unopened/native.sock',
            'group':group.GROUP,'leader':True,'wide_bracket':False,'common_start_ns':0,'arrival_lead_ns':150_000_000}
        monkeypatch.setattr(music.os,'geteuid',lambda:1234)
        monkeypatch.setattr(music.os,'getegid',lambda:1235)
        monkeypatch.setattr(music.time,'monotonic_ns',lambda:self.ns)
        original_write = music.atomic_json
        def publish(path,value):
            original_write(path,value)
            if value['stage'] == 'ready_before_begin':
                assert not self.sent and not self.connections and not self.c.actor.snapshot()['owner']
                self.ns += 1
                action = {'generation':2,'action':'begin','available_ns':self.ns,
                          'common_start_ns':self.ns+150_000_000}
                if calendar_change:
                    action.update(calendar_change)
                self.command.write_text(json.dumps(action))
        monkeypatch.setattr(music,'atomic_json',publish)
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop,'add_signal_handler',lambda _signal,callback:setattr(self,'stop',callback))
        def new_socket(*_args):
            return SimpleNamespace(setblocking=lambda _value:None, close=self.close)
        monkeypatch.setattr(music.socket,'socket',new_socket)
        monkeypatch.setattr(loop,'sock_connect',self.connect)
        monkeypatch.setattr(loop,'sock_sendall',self.send)
        monkeypatch.setattr(loop,'sock_recv',self.recv)

    async def connect(self,connection,path):
        self.connections.append(path)
        assert self.command.exists() and self.command.read_text().find('common_start_ns') >= 0

    async def send(self,connection,data):
        packet = Packet.decode(data)
        self.sent.append(packet)
        if packet.kind is Kind.BEGIN:
            self.answer = await self.c.begin(packet,self.handle)
        else:
            assert packet.kind is Kind.PCM
            await self.c.message(packet,self.handle)
            self.stop()

    async def recv(self,*_args):
        self.ns += self.delay
        answer = self.answer
        if self.reply_change:
            answer = replace(answer,**self.reply_change)
        return answer.encode()

    def close(self):
        self.closed += 1

    async def run(self):
        await self.c.initialize()
        return await music.producer(self.config,group.__dict__)


@pytest.mark.asyncio
@pytest.mark.parametrize('delay',[0,80_000_000,149_999_999])
async def test_actual_begin_grant_first_native_pcm_preserve_fixed_calendar_source_and_h750(monkeypatch,tmp_path,delay):
    driver = ProducerDriver(monkeypatch,tmp_path,delay=delay)
    try:
        await driver.run()
        state = json.loads(driver.status.read_text())
        assert [item.kind for item in driver.sent] == [Kind.BEGIN,Kind.PCM]
        native = driver.sent[1]
        assert state['first_pcm_sequence'] == state['first_pcm_frame'] == native.sequence == native.frame_index == 0
        assert native.frames == 960 and state['first_pcm_lateness_ns'] == delay
        assert state['common_start_ns'] == state['available_ns']+150_000_000
        assert state['ready_before_begin_ns'] < state['available_ns'] <= state['begin_attempt_ns'] <= state['grant_received_ns']
        assert len(driver.writer.packets) == 1
        output = driver.writer.packets[0]
        # NativeMixer retains native shared P and adds only the fixed H; the
        # actual C pipe input subtracts B at the existing OwnTone timer seam.
        assert output.presentation_ns == state['common_start_ns']+750_000_000
        assert output.session == native.session and output.epoch == native.epoch and output.generation == 1
        assert driver.c.actor.snapshot()['owner']['session_id'] == native.session_id
        assert len(driver.client.requests) == 2  # idle arm + exact owner; no pause/seek.
        assert driver.closed == 1 and state['frames'] == 960
    finally:
        await driver.c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('delay',[150_000_000,400_000_000])
async def test_delayed_grant_refuses_delivery_at_original_deadline_without_retiming_or_advancing(monkeypatch,tmp_path,delay):
    driver = ProducerDriver(monkeypatch,tmp_path,delay=delay)
    try:
        with pytest.raises(RuntimeFailure,match='bounded native delivery cadence'):
            await driver.run()
        state = json.loads(driver.status.read_text())
        assert [item.kind for item in driver.sent] == [Kind.BEGIN]
        assert state['frames'] == 0 and state['first_pcm_lateness_ns'] == delay
        assert state['common_start_ns'] == state['available_ns']+150_000_000
        assert not driver.writer.packets and driver.closed == 1
    finally:
        await driver.c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',[{'available_ns':14_000_000_000,'common_start_ns':14_150_000_000},
    {'available_ns':16_000_000_000,'common_start_ns':16_150_000_000},
    {'generation':3},{'common_start_ns':15_150_000_002}])
async def test_stale_future_or_replayed_calendar_refuses_before_actual_begin(monkeypatch,tmp_path,change):
    driver = ProducerDriver(monkeypatch,tmp_path,calendar_change=change)
    try:
        with pytest.raises(RuntimeFailure):
            await driver.run()
        assert not driver.sent and not driver.connections and not driver.writer.packets
        assert driver.c.actor.snapshot()['owner'] is None
        assert json.loads(driver.status.read_text())['frames'] == 0
    finally:
        await driver.c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change',[{'kind':Kind.END},{'session':uuid4().bytes},{'group':uuid4().bytes},
    {'generation':2},{'epoch':0},{'incarnation':bytes(16)},{'flags':0}])
async def test_wrong_native_grant_cannot_open_a_clock_or_first_frame(monkeypatch,tmp_path,change):
    driver = ProducerDriver(monkeypatch,tmp_path,reply_change=change)
    try:
        with pytest.raises(RuntimeFailure,match='exact fresh source grant'):
            await driver.run()
        assert len(driver.sent) == 1 and not driver.writer.packets and driver.closed == 1
        assert json.loads(driver.status.read_text())['frames'] == 0
    finally:
        await driver.c.close()


@pytest.mark.asyncio
async def test_original_observer_stays_live_during_analysis_and_late_failure_is_sticky():
    entered, release = asyncio.Event(), asyncio.Event()
    broken = False
    async def check():
        entered.set()
        if broken:
            raise RuntimeFailure('original source changed during bounded analysis')
    guards = [SimpleNamespace(end_at=lambda _:None,evidence=lambda:{'checked':True}) for _ in range(2)]
    context = SimpleNamespace(evidence={},pcm_guards=dict(zip((group.A,group.B),guards,strict=True)))
    observer = music.MusicObserver(context,check)
    await observer.initialize()
    async def analysis():
        await release.wait()
    task = asyncio.create_task(analysis())
    await entered.wait()
    broken = True
    await asyncio.sleep(.08)
    release.set()
    await task
    with pytest.raises(RuntimeFailure,match='original source changed'):
        await observer.check()
    with pytest.raises(RuntimeFailure,match='original source changed'):
        await observer.close()
    assert observer.watcher.done() and not observer.stopped and 'stop_boundary' not in context.evidence


@pytest.mark.asyncio
@pytest.mark.parametrize('mutant', ['selection','extra_selection','offset','pin','unit','unit_dead','owner',
    'generation','group','producer_generation','dropped','speech','bool_blocks'])
async def test_actual_original_actor_check_rejects_backend_or_grant_changes(mutant):
    rooms = definitions()
    grants = {room.id:{'session_id':str(uuid4()),'epoch':1,'incarnation':str(uuid4()),'generation':1}
              for room in rooms}
    outputs = {room.id:[{'id':'0','selected':True,'offset_ms':0}] for room in rooms}
    healths = {room.id:{**healthy(), 'source':{'ready':True,'owner':{key:value for key,value in grants[room.id].items()
                                                    if key != 'generation'}},
                       'native_blocks':1,'native_generation':1,'native_group_id':group.GROUP,
                       'dropped_bytes':0,'speech_dropped_frames':0,'speech_output_error':None,
                       'speech_output_errno':None,'speech_session_id':None} for room in rooms}
    units = {room.id:{'shairport':{'invocation':'original'}} for room in rooms}
    states = {}
    handles = {room.id:{'status':deepcopy(grants[room.id])} for room in rooms}
    def pin():
        if mutant == 'pin':
            raise RuntimeFailure('held output was replaced')
    for room in rooms:
        async def actual(_names,identifier=room.id): return outputs[identifier]
        unit = SimpleNamespace(identity=lambda:{'invocation':'original'},alive=True)
        states[room.id] = SimpleNamespace(desired=room, status='running', selected_ids=['0'],
            processes={'shairport':unit},local_pin=SimpleNamespace(validate=pin),client=SimpleNamespace(outputs=actual))
    context = SimpleNamespace(states=states,broker=SimpleNamespace(rooms=states),producers=handles,
                              group=SimpleNamespace(producer_status=lambda handle:handle['status'],GROUP=group.GROUP))
    selected = outputs[group.A]
    health = healths[group.A]
    if mutant == 'selection':
        selected[0]['selected'] = False
    elif mutant == 'extra_selection':
        selected.append({'id':'1','selected':True,'offset_ms':0})
    elif mutant == 'offset':
        selected[0]['offset_ms'] = -1
    elif mutant == 'unit':
        states[group.A].processes['shairport'].identity = lambda:{'invocation':'successor'}
    elif mutant == 'unit_dead':
        states[group.A].processes['shairport'].alive = False
    elif mutant == 'owner':
        health['source']['owner']['epoch'] = 2
    elif mutant == 'generation':
        health['native_generation'] = 2
    elif mutant == 'group':
        health['native_group_id'] = str(uuid4())
    elif mutant == 'producer_generation':
        handles[group.A]['status']['generation'] = 2
    elif mutant == 'dropped':
        health['dropped_bytes'] = 1
    elif mutant == 'speech':
        health['speech_session_id'] = str(uuid4())
    elif mutant == 'bool_blocks':
        health['native_blocks'] = True
    with pytest.raises(RuntimeFailure):
        await music.check_original_actors(context,healths,units,grants)


@pytest.mark.asyncio
async def test_cancel_during_grant_closes_exact_producer_transport_without_pcm(monkeypatch,tmp_path):
    driver = ProducerDriver(monkeypatch,tmp_path)
    entered = asyncio.Event()
    async def blocked(*_args):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(asyncio.get_running_loop(),'sock_recv',blocked)
    task = asyncio.create_task(driver.run())
    try:
        await asyncio.wait_for(entered.wait(),1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert driver.closed == 1 and not driver.writer.packets
        assert json.loads(driver.status.read_text())['frames'] == 0
    finally:
        await driver.c.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux native SEQPACKET frames')
async def test_real_linux_native_connection_grant_first_pcm_and_eof_retire_exact_source(monkeypatch,tmp_path):
    from test_native_ingress_diagnostics import serve,until
    c, writer, client = controller()
    c.mixer.now_ns = music.time.monotonic_ns
    c.mixer.relay_delay_ns = 750_000_000
    c.mixer.output_buffer_ms = 500
    await c.initialize()
    peer, task = serve(c)
    stop = None
    original = music.atomic_json
    config = {'music_startup':deepcopy(music.PRODUCER_PROFILE),'uid':1234,'gid':1235,
        'status':str(tmp_path/'status.json'),'command':str(tmp_path/'command.json'),'socket':'/already-connected',
        'group':group.GROUP,'leader':True,'wide_bracket':False,'common_start_ns':0,'arrival_lead_ns':150_000_000}
    await asyncio.to_thread(Path(config['command']).write_text,json.dumps({'generation':1,'action':'wait'}))
    def publish(path,value):
        original(path,value)
        if value['stage'] == 'ready_before_begin':
            at = music.time.monotonic_ns()
            Path(config['command']).write_text(json.dumps({'generation':2,'action':'begin','available_ns':at,
                                                          'common_start_ns':at+150_000_000}))
        if value['stage'] == 'streaming':
            stop()
    def handler(_signal,callback):
        nonlocal stop
        stop = callback
    monkeypatch.setattr(music.os,'geteuid',lambda:1234)
    monkeypatch.setattr(music.os,'getegid',lambda:1235)
    monkeypatch.setattr(music,'atomic_json',publish)
    monkeypatch.setattr(music.socket,'socket',lambda *_args:peer)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop,'add_signal_handler',handler)
    async def connected(*_args): pass
    monkeypatch.setattr(loop,'sock_connect',connected)
    try:
        await music.producer(config,group.__dict__)
        await until(lambda:len(writer.packets) == 1)
        await asyncio.wait_for(task,1)
        state = json.loads(await asyncio.to_thread(Path(config['status']).read_text))
        packet = writer.packets[0]
        assert packet.frame_index == packet.sequence == 0 and packet.frames == 960
        assert abs(packet.presentation_ns-state['common_start_ns']-750_000_000) <= 1
        assert state['first_pcm_native_presentation_ns'] == state['common_start_ns']
        assert state['first_pcm_lateness_ns'] < 150_000_000
        assert c.actor.snapshot()['owner'] is None and not c.handles
        assert len(client.requests) == 3  # idle, original owner, exact EOF release.
    finally:
        peer.close()
        await c.close()


@pytest.fixture(scope='module')
def exact_music_prefix():
    return music.complete_music_reference(group.program_pcm)


def prefix_snapshot(reference, *, onset=12, offset_ns=0):
    # Actual per-buffer stamps, no receipt rebase or fitted phase.
    first = 10_000_000_000
    raw = bytes(onset*4)+reference+bytes(960*4)
    chunks = [raw[start:start+3840] for start in range(0,len(raw),3840)]
    times = [index*.02 for index in range(len(chunks))]
    absolute = {at:first+index*20_000_000+offset_ns for index,at in enumerate(times)}
    capture=SimpleNamespace(chunks=chunks,captured_at=times,absolute=absolute,poll=lambda:None)
    return group.capture_snapshot(capture), first+onset*1_000_000_000//48000


def test_full_music_prefix_verifies_every_lossless_sample_and_original_absolute_origin(exact_music_prefix):
    snapshot,origin = prefix_snapshot(exact_music_prefix)
    receipt = music.verify_complete_music(snapshot,group.program_pcm,origin,
        {'capture_offset_ms':0,'horizon_half_width_ms':27})
    assert receipt['verified_frames'] == 21*48000 and receipt['coded_frames'] == 20*48000
    assert receipt['constant_tail_frames'] == 48000 and receipt['corrected_horizon_error_ms'] == 0


@pytest.mark.parametrize('mutant',['missing_prefix','missing_tail','sample_gap','single_lsb','foreign_voice',
                                  'repeated_program','late_origin','missing_stamp'])
def test_full_reference_cannot_hide_first_frame_loss_tail_gap_voice_or_retime(exact_music_prefix,mutant):
    import numpy as np
    reference = exact_music_prefix
    if mutant == 'missing_prefix':
        reference = reference[960*4:]
    elif mutant == 'missing_tail':
        reference = reference[:-960*4]
    elif mutant == 'sample_gap':
        reference = reference[:48000*4]+bytes(384*4)+reference[(48000+384)*4:]
    elif mutant == 'single_lsb':
        reference = reference[:10000]+bytes([reference[10000]^1])+reference[10001:]
    elif mutant == 'foreign_voice':
        values = np.frombuffer(reference,dtype='<i2').copy().reshape(-1,2)
        indexes = np.arange(240)
        values[48000:48240] += (80*np.sin(2*np.pi*880*indexes/48000)).astype('<i2')[:,None]
        reference = values.tobytes()
    elif mutant == 'repeated_program':
        reference = reference+reference
    snapshot,origin = prefix_snapshot(reference,offset_ns=30_000_000 if mutant == 'late_origin' else 0)
    if mutant == 'missing_stamp':
        snapshot.absolute.clear()
    with pytest.raises(RuntimeFailure):
        music.verify_complete_music(snapshot,group.program_pcm,origin,
            {'capture_offset_ms':0,'horizon_half_width_ms':27})


def music_supervisor():
    spec = importlib.util.spec_from_file_location('music_actual_supervisor', ROOT/'tests/linux/run_native_music_startup.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_music_supervisor_uses_distinct_artifact_and_held_work_mode_under_private_umask(monkeypatch,tmp_path):
    import os
    import stat
    supervisor = music_supervisor()
    assert supervisor.RESULT.name == 'shiri-v2-native-music-startup-supervisor-result.json'
    assert supervisor.INNER_RESULT.name == 'shiri-v2-native-music-startup-result.json'
    work = tmp_path/'work'
    original_stat,original_lstat = supervisor.os.fstat,Path.lstat
    def root_info(info):
        return SimpleNamespace(st_dev=info.st_dev,st_ino=info.st_ino,st_mode=info.st_mode,st_uid=0)
    monkeypatch.setattr(supervisor,'root_directory',lambda path,mode:path.mkdir(mode=mode))
    monkeypatch.setattr(supervisor.os,'fstat',lambda descriptor:root_info(original_stat(descriptor)))
    monkeypatch.setattr(Path,'lstat',lambda path:root_info(original_lstat(path)))
    old = os.umask(0o077)
    try:
        supervisor.prepare_work(work)
    finally:
        os.umask(old)
    assert stat.S_IMODE(original_lstat(work).st_mode) == 0o755


@pytest.mark.parametrize('mutant',['nonroot','replaced','writable'])
def test_music_supervisor_never_publishes_work_permission_on_wrong_directory(monkeypatch,tmp_path,mutant):
    supervisor = music_supervisor()
    work = tmp_path/'work'
    original_stat = supervisor.os.fstat
    monkeypatch.setattr(supervisor,'root_directory',lambda path,mode:path.mkdir(mode=mode))
    def held(descriptor):
        info=original_stat(descriptor)
        return SimpleNamespace(st_dev=info.st_dev,st_ino=info.st_ino,st_mode=info.st_mode|(0o020 if mutant=='writable' else 0),
                               st_uid=1234 if mutant=='nonroot' else 0)
    monkeypatch.setattr(supervisor.os,'fstat',held)
    if mutant=='replaced':
        original_lstat=Path.lstat
        def current(path):
            info=original_lstat(path)
            return SimpleNamespace(st_dev=info.st_dev,st_ino=info.st_ino+1,st_mode=info.st_mode,st_uid=0)
        monkeypatch.setattr(Path,'lstat',current)
    writes=[]
    monkeypatch.setattr(supervisor.os,'fchmod',lambda *_args:writes.append(True))
    with pytest.raises(RuntimeFailure,match='exact root-owned'):
        supervisor.prepare_work(work)
    assert not writes


@pytest.mark.asyncio
async def test_cancelled_child_observer_is_sticky_failure_without_cancelling_outer_retirement():
    async def check():
        return True
    context=SimpleNamespace(evidence={},pcm_guards={})
    observer=music.MusicObserver(context,check)
    await observer.initialize()
    observer.watcher.cancel()
    await asyncio.gather(observer.watcher,return_exceptions=True)
    with pytest.raises(RuntimeFailure,match='unexpectedly cancelled'):
        await observer.check()
    retired=[]
    try:
        await observer.close()
    except Exception as exc:
        # Same parent guard: a cancelled CHILD is a failed observation, not
        # cancellation of the fixture's producer/capture/broker retirement.
        assert type(exc) is RuntimeFailure
        retired.append('observer_failed')
    retired.extend(['producer','capture','broker','lan'])
    assert retired == ['observer_failed','producer','capture','broker','lan']
    assert observer.done.is_set() and 'stop_boundary' not in context.evidence


@pytest.mark.asyncio
async def test_music_mode_refuses_missing_lab_before_resource_setup(monkeypatch):
    monkeypatch.setattr(group,'NATIVE_LAB',None)
    with pytest.raises(RuntimeFailure,match='explicit clean lab'):
        await group.run_check(music_startup=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('other',['speech_stress','zone_faults','latency_probe'])
async def test_music_mode_cannot_mix_with_other_experiments_before_resource_setup(other):
    with pytest.raises(RuntimeFailure,match='mutually exclusive'):
        await group.run_check(music_startup=True,**{other:True})



@pytest.mark.asyncio
async def test_complete_tail_waits_for_real_delayed_buffer_under_original_observer(exact_music_prefix):
    snapshot,origin=prefix_snapshot(exact_music_prefix,offset_ns=12_000_000)
    # Include the declared upper capture uncertainty, not just nominal21s.
    capture=SimpleNamespace(chunks=list(snapshot.chunks[:-2]),captured_at=list(snapshot.captured_at[:-2]),
        absolute=dict(snapshot.absolute),poll=lambda:None)
    checks=0
    async def check():
        nonlocal checks
        checks+=1
        if checks==3:
            capture.chunks.extend(snapshot.chunks[-2:])
            capture.captured_at.extend(snapshot.captured_at[-2:])
    observer=SimpleNamespace(check=check)
    context=SimpleNamespace(group=group,captures={group.A:capture},report={
        'independent_capture_baselines':{group.A:{'capture_offset_ms':12,'horizon_half_width_ms':1}}})
    frozen=await music.freeze_complete_music(context,observer,origin,timeout=.3)
    assert checks >= 4 and type(frozen[group.A].chunks) is tuple
    receipt=music.verify_complete_music(frozen[group.A],group.program_pcm,origin,
        context.report['independent_capture_baselines'][group.A])
    assert receipt['verified_frames']==21*48000 and receipt['corrected_horizon_error_ms']==0


@pytest.mark.asyncio
async def test_missing_tail_wait_is_bounded_and_never_catches_sticky_guard_failure(exact_music_prefix):
    snapshot,origin=prefix_snapshot(exact_music_prefix)
    capture=SimpleNamespace(chunks=list(snapshot.chunks[:-2]),captured_at=list(snapshot.captured_at[:-2]),
        absolute=dict(snapshot.absolute),poll=lambda:None)
    context=SimpleNamespace(group=group,captures={group.A:capture},report={
        'independent_capture_baselines':{group.A:{'capture_offset_ms':0,'horizon_half_width_ms':27}}})
    async def healthy(): pass
    with pytest.raises(RuntimeFailure,match='complete original music tail'):
        await music.freeze_complete_music(context,SimpleNamespace(check=healthy),origin,timeout=.01)
    async def failed():
        raise RuntimeFailure('exact music gap in delayed tail')
    with pytest.raises(RuntimeFailure,match='exact music gap'):
        await music.freeze_complete_music(context,SimpleNamespace(check=failed),origin,timeout=.01)
