#!/usr/bin/env python3
"""Manual real-kernel v5 bridge/socket publication proof; no Bluetooth or audio.

Requires the stopped, empty b265 candidate, its provisioned v2 identity map and
trusted installed Python sources/helper. Run under an external systemd watchdog.
This exercises real transient services, mount views, O_PATH/socket inodes and
SO_PEERCRED. It neither mocks those boundaries nor opens a BlueALSA/PCM device.
"""
from __future__ import annotations

# Manual filesystem observations are the actual kernel evidence.
# ruff: noqa: ASYNC240

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import traceback
from uuid import uuid4

from shiri.runtime.bind_policy import trusted_file
from shiri.runtime.identities import DaemonIdentities
from shiri.runtime.network import NetworkManager
from shiri.runtime.socket_publication import prepare, publish
from shiri.runtime.system import Runner, atomic_json, process_birth, root_directory
from shiri.runtime.units import Bind, UnitManager, UnitSpec, VIEW, new_unit

BASELINE_SPEC = importlib.util.spec_from_file_location('manual_host_preservation', Path(__file__).with_name('host_preservation.py'))
BASELINE_MODULE = importlib.util.module_from_spec(BASELINE_SPEC)
BASELINE_SPEC.loader.exec_module(BASELINE_MODULE)
compare_baseline = BASELINE_MODULE.compare_baseline

STATE = Path('/var/lib/shiri-v2-test-runtime')
RUNTIME = Path('/run/shiri-v2-test')
WORK = Path('/var/lib/shiri-v2-bluetooth-publication-review')
MAP = Path('/etc/shiri-v2-test/daemon-identities.json')
HIDDEN = ('/run/dbus/system_bus_socket', str(MAP.parent))
HELPER = Path('/opt/shiri-v2-next4-deps/libexec/shiri-bind-policy')
INSTALLATION = 'b265eb7d-18fa-4756-8bf6-5277bdb0ef60'
OWNER = '84e10887-e9ad-4b76-a8d8-c49e9c1d0327'
LEGACY_PID = 2444

BRIDGE = r'''import json,os,select,signal,socket,stat,struct,time
from pathlib import Path
state=Path('/run/shiri-worker/state')
status={line.split(':',1)[0]:line.split(':',1)[1].strip()
        for line in Path('/proc/self/status').read_text().splitlines() if ':' in line}
checks={'zero_capabilities':all(int(status[k],16)==0 for k in ['CapEff','CapPrm','CapInh','CapBnd','CapAmb']),
        'no_new_privileges':status.get('NoNewPrivs')=='1'}
try: probe=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
except OSError: checks['inet_socket_denied']=True
else: probe.close();checks['inet_socket_denied']=False
try:
    probe=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    probe.connect('/run/dbus/system_bus_socket')
except OSError: checks['host_bus_denied']=True
else: checks['host_bus_denied']=False
finally: probe.close()
for label,path in [('device',os.environ['DEVICE_NODE']),('api_secret',os.environ['API_SECRET'])]:
    try: fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK)
    except OSError: checks[label+'_denied']=True
    else: os.close(fd);checks[label+'_denied']=False
secret=state/'secret';secret.write_text('Disposable fixture secret')
secret.chmod(0o600)
path=state/'final-pcm.sock'
listener=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET)
listener.bind(str(path));path.chmod(0o600);listener.listen(4)
original=path.stat()
(state/'ready.json').write_text(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'pid':os.getpid(),
    'socket_dev':original.st_dev,'socket_inode':original.st_ino,'checks':checks}))
replacement=None
running=True
def stopping(*_):
    global running
    running=False
signal.signal(signal.SIGTERM,stopping)
try:
    while running and not (state/'replace.json').exists():time.sleep(.01)
    if running:
        assert not path.exists(), 'Broker has not published the admitted listener'
        replacement=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET)
        replacement.bind(str(path));path.chmod(0o600);replacement.listen(4)
        replacement_info=path.stat()
        assert replacement_info.st_ino!=original.st_ino
        (state/'replacement.json').write_text(json.dumps({'socket_dev':replacement_info.st_dev,
                                                        'socket_inode':replacement_info.st_ino}))
    while running:
        ready,_,_=select.select([listener,*([replacement] if replacement else [])],[],[],.1)
        for active in ready:
            client,_=active.accept()
            try:
                peer=struct.unpack('3i',client.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                channel='admitted' if active is listener else 'replacement'
                receipt={'channel':channel,'peer':peer,'uid':os.getuid(),'gid':os.getgid(),'pid':os.getpid()}
                client.settimeout(1)
                assert client.recv(128)==b'publication-kernel-probe'
                client.send(json.dumps(receipt).encode())
                (state/'accepted.json').write_text(json.dumps(receipt))
            finally:client.close()
finally:
    listener.close()
    if replacement:
        replacement.close()
        try: now=path.lstat()
        except FileNotFoundError: pass
        else:
            if stat.S_ISSOCK(now.st_mode) and (now.st_dev,now.st_ino)==(replacement_info.st_dev,replacement_info.st_ino):
                path.unlink()
'''

OUTPUT = r'''import json,os,signal,socket,struct,time
from pathlib import Path
path=Path('/run/shiri-worker/bridge/final-pcm.sock')
checks={}
status={line.split(':',1)[0]:line.split(':',1)[1].strip()
        for line in Path('/proc/self/status').read_text().splitlines() if ':' in line}
checks['zero_capabilities']=all(int(status[k],16)==0 for k in ['CapEff','CapPrm','CapInh','CapBnd','CapAmb'])
checks['no_new_privileges']=status.get('NoNewPrivs')=='1'
for name,operation in [('chmod',lambda:os.chmod(path,0o600)),('unlink',lambda:path.unlink())]:
    try: operation()
    except OSError: checks['published_'+name+'_denied']=True
    else: checks['published_'+name+'_denied']=False
for label,private in [('bridge_secret',os.environ['BRIDGE_SECRET']),('api_secret',os.environ['API_SECRET']),
                      ('peer_mount',os.environ['PEER_SECRET']),('device',os.environ['DEVICE_NODE'])]:
    try: fd=os.open(private,os.O_RDONLY|os.O_NONBLOCK)
    except OSError: checks[label+'_denied']=True
    else: os.close(fd);checks[label+'_denied']=False
for label,bus in [('direct_bus','/run/dbus/system_bus_socket'),('peer_mount_bus',os.environ['PEER_BUS'])]:
    probe=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    try: probe.connect(bus)
    except OSError: checks[label+'_denied']=True
    else: checks[label+'_denied']=False
    finally:probe.close()
client=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET);client.settimeout(2)
client.connect(str(path))
peer=struct.unpack('3i',client.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
client.send(b'publication-kernel-probe');receipt=json.loads(client.recv(2048));client.close()
Path('/run/shiri-worker/state/observed.json').write_text(json.dumps({'uid':os.getuid(),'gid':os.getgid(),
    'pid':os.getpid(),'peer':peer,'receipt':receipt,'checks':checks}))
running=True
def stopping(*_):
    global running
    running=False
signal.signal(signal.SIGTERM,stopping)
while running:time.sleep(.1)
'''


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def capability_admission():
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    fields = {key: int(status[key].strip(), 16) for key in ('CapEff', 'CapPrm', 'CapInh', 'CapBnd', 'CapAmb')}
    require(all(not mask & (1 << 19) for mask in fields.values()),
            'CAP_SYS_PTRACE must be absent from every broker capability set')
    required = sum(1 << index for index in (0, 1, 3, 4, 5, 6, 7, 10, 12, 13, 21, 23))
    require(all(not mask & ~required for mask in fields.values()),
            'The fixture broker has capabilities outside the production set')
    require(fields['CapEff'] == fields['CapBnd'] == required,
            'Run this fixture with the reviewed production broker capabilities, including CAP_FOWNER and CAP_FSETID')
    return {key: hex(value) for key, value in fields.items()}


def private_state(path, account):
    path.mkdir(mode=0o700)
    os.chown(path, account['uid'], account['gid'])
    return path


async def observed(path, unit):
    for _ in range(150):
        if path.exists():
            return json.loads(path.read_text())
        require(unit.alive, 'An exact probe unit exited before its kernel observation')
        await asyncio.sleep(.02)
    raise RuntimeError('The bounded kernel observation did not arrive')


def acquire_lock():
    root_directory(RUNTIME)
    path = RUNTIME/'broker.lock'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and not info.st_mode & 0o077, 'Candidate broker lock is not a protected root-owned regular file')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        named = path.lstat()
        require((named.st_dev, named.st_ino) == (info.st_dev, info.st_ino), 'Candidate broker lock changed')
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


async def baseline(runner):
    links = (await runner.run(['/usr/sbin/ip', '-j', 'link'])).stdout
    started = asyncio.get_running_loop().time()
    addresses = (await runner.run(['/usr/sbin/ip', '-j', 'address'])).stdout
    finished = asyncio.get_running_loop().time()
    snapshot = {
        'links': links, 'addresses': addresses,
        'legacy_birth': process_birth(LEGACY_PID),
        'legacy_loopback': {
            f'{direction}/sub{sub}': '\n'.join(line for line in
                Path(f'/proc/asound/Loopback/{direction}/sub{sub}/status').read_text().splitlines()
                if line.strip() == 'closed' or line.startswith(('state:', 'owner_pid')))
            for direction in ['pcm0p', 'pcm0c', 'pcm1p', 'pcm1c'] for sub in [0, 2]},
        'dhclient_births': {
            directory.name: process_birth(int(directory.name))
            for directory in Path('/proc').iterdir() if directory.name.isdecimal()
            and (directory/'comm').exists() and (directory/'comm').read_text().strip() == 'dhclient'},
    }
    snapshot['observation_window'] = {'started': started, 'finished': finished}
    require(len(json.dumps(snapshot)) <= 2*1024*1024, 'Host preservation snapshot exceeded its evidence bound')
    return snapshot


async def cgroup_proof(manager, unit):
    actual = await manager.inspect(unit.entry['unit'])
    require(actual is not None, 'The exact service disappeared before proof capture')
    manager.verify(unit.entry, actual)
    descriptor = manager.open_cgroup(unit.entry)
    try:
        info = os.fstat(descriptor)
        require(info.st_ino == unit.entry['cgroup_inode'], 'The held kernel cgroup is not the admitted inode')
        require(f'0::{unit.entry["control_group"]}' in Path(f'/proc/{unit.process.pid}/cgroup').read_text().splitlines(),
                'The observed MainPID does not belong to the admitted cgroup')
        if unit.entry.get('bind_policy'):
            await manager.bind_policy.run('verify', descriptor, unit.entry['boot_id'],
                                          unit.entry['listen_port'], unit.entry['bind_policy'])
        return {'inode': info.st_ino, 'device': info.st_dev, 'invocation_id': unit.entry['invocation_id'],
                'unit': unit.entry['unit'], 'main_pid': unit.process.pid,
                'socket_policy_verified': bool(unit.entry.get('bind_policy'))}
    finally:
        os.close(descriptor)


async def run():
    result = {'started_at': datetime.now(timezone.utc).isoformat(), 'ok': False, 'checks': {},
              'scope': 'real v5 UID/cgroup/mount/socket publication; no BlueALSA, controller or audio device'}
    manager = network = initial = None
    lock = source_fd = directory_fd = None
    units, directory = [], None
    try:
        require(sys.platform == 'linux' and os.geteuid() == 0, 'Run this manual candidate check as Linux root')
        result['broker_capabilities'] = capability_admission()
        identities = DaemonIdentities(MAP).load()
        require(identities.installation_id == INSTALLATION and identities.runtime_state_dir == STATE
                and identities.runtime_dir == RUNTIME, 'The dedicated v2 account map belongs to another installation')
        lock = acquire_lock()
        runner = Runner()
        network = NetworkManager(STATE, runner)
        require(network.installation_id == INSTALLATION and not network.manifest['networks']
                and not network.manifest['processes'], 'Candidate ownership must be empty before this check')
        initial = await baseline(runner)
        result['baseline_before'] = initial
        require(initial['legacy_birth'], 'The expected untouched legacy application is absent')
        bridge, output = identities.account('slot7.bridge'), identities.account('slot7.output')
        require(bridge['uid'] != output['uid'] and bridge['gid'] != output['gid'], 'Bridge credentials are not separated')
        trusted_file(HELPER, executable=True)
        python = str(trusted_file(Path('/usr/bin/python3').resolve(strict=True), executable=True))
        root_directory(WORK)
        directory = WORK/uuid4().hex
        directory.mkdir(mode=0o700)
        result_path = directory/'result.json'
        atomic_json(result_path, result)
        bridge_state = private_state(directory/'bridge-state', bridge)
        output_state = private_state(directory/'output-state', output)
        controls = list(Path('/dev/snd').glob('controlC*'))
        require(controls, 'The existing host control-node denial probe is unavailable')
        device = str(controls[0])  # Read/open denial only; no ioctl or PCM write.
        api_secret = str(MAP.parent/'api-token')
        require(Path(api_secret).is_file(), 'The candidate API credential denial target is missing')
        manager = UnitManager(runner, network, bind_policy_helper=HELPER)
        # PrivateDevices supplies a fresh /dev without sound nodes. Masking the
        # absent /dev/snd again fails namespace setup on systemd 249.
        hidden = HIDDEN
        bridge_script = directory/'bridge.py'
        bridge_script.write_text(BRIDGE)
        bridge_script.chmod(0o444)
        result['bridge_script_sha256'] = hashlib.sha256(bridge_script.read_bytes()).hexdigest()
        bridge_spec = UnitSpec(new_unit('b265eb7d', OWNER, 'bluetooth-output'), 'bluetooth-output',
                              bridge['name'], bridge['name'], (python, str(VIEW/'probe.py')),
                              binds=(Bind(str(bridge_script), str(VIEW/'probe.py')),
                                     Bind(str(bridge_state), str(VIEW/'state'), True)),
                              environment=(f'DEVICE_NODE={device}', f'API_SECRET={api_secret}'), inaccessible=hidden)
        bridge_key = OWNER+':bluetooth-output'
        bridge_unit = await manager.start(bridge_key, bridge_spec, directory/'bridge.log')
        units.append((bridge_key, bridge_unit))
        ready = await observed(bridge_state/'ready.json', bridge_unit)
        require(ready['uid'] == bridge['uid'] and ready['gid'] == bridge['gid']
                and ready['pid'] == bridge_unit.process.pid and all(ready['checks'].values()),
                'Actual v5 bridge credentials/capabilities/address-family/bus/device boundary failed')
        require(bridge_unit.entry['policy_version'] == 5, 'The service did not admit the immutable v5 profile')
        result['bridge_observation'] = ready
        result['bridge_cgroup'] = await cgroup_proof(manager, bridge_unit)
        generation = uuid4().hex
        parent = STATE/'rooms'/OWNER/'bridge-published'
        root_directory(parent)
        record, source_fd, directory_fd = prepare(bridge_state/'final-pcm.sock', parent/generation,
                                                 uid=bridge['uid'], initial_gid=bridge['gid'], gid=output['gid'])
        result['publication'] = record
        require((os.fstat(source_fd).st_dev, os.fstat(source_fd).st_ino)
                == (ready['socket_dev'], ready['socket_inode']), 'Root O_PATH did not capture the actual listener inode')
        bridge_unit.entry['socket_publication'] = record
        network.remember_unit(bridge_key, bridge_unit.identity())
        # A real reload must validate the durable receipt before publication.
        reread = NetworkManager(STATE, runner)
        require(reread.manifest['processes'][bridge_key]['socket_publication'] == record,
                'The protected socket authority was not durable before rename')
        published = publish(bridge_state/'final-pcm.sock', record, source_fd, directory_fd)
        held = os.fstat(source_fd)
        require(held.st_uid == bridge['uid'] and held.st_gid == output['gid']
                and stat.S_IMODE(held.st_mode) == 0o660 and published.stat().st_ino == held.st_ino,
                'The actual held socket rename/chown/chmod publication failed')
        atomic_json(bridge_state/'replace.json', {'replace': True}, mode=0o644)
        replacement = await observed(bridge_state/'replacement.json', bridge_unit)
        require(replacement['socket_inode'] != record['socket_inode'], 'The private replacement did not use a new inode')
        output_script = directory/'output.py'
        output_script.write_text(OUTPUT)
        output_script.chmod(0o444)
        result['output_script_sha256'] = hashlib.sha256(output_script.read_bytes()).hexdigest()
        peer = f'/proc/{bridge_unit.process.pid}/root'
        output_spec = UnitSpec(new_unit('b265eb7d', OWNER, 'owntone'), 'owntone', output['name'], output['name'],
                              (python, str(VIEW/'probe.py')), listen_port=3939,
                              binds=(Bind(str(output_script), str(VIEW/'probe.py')),
                                     Bind(str(output_state), str(VIEW/'state'), True),
                                     Bind(str(published), str(VIEW/'bridge/final-pcm.sock'))),
                              environment=(f'DEVICE_NODE={device}', f'API_SECRET={api_secret}',
                                           f'BRIDGE_SECRET={bridge_state}/secret',
                                           f'PEER_SECRET={peer}/run/shiri-worker/state/secret',
                                           f'PEER_BUS={peer}/run/dbus/system_bus_socket'), inaccessible=hidden)
        output_key = OWNER+':owntone'
        output_unit = await manager.start(output_key, output_spec, directory/'output.log')
        units.append((output_key, output_unit))
        observation = await observed(output_state/'observed.json', output_unit)
        accepted = await observed(bridge_state/'accepted.json', bridge_unit)
        require(observation['uid'] == output['uid'] and observation['gid'] == output['gid']
                and observation['pid'] == output_unit.process.pid and all(observation['checks'].values()),
                'Actual output read-only/peer-mount/secret/bus/device boundary failed')
        require(observation['peer'] == [bridge_unit.process.pid, bridge['uid'], bridge['gid']]
                and accepted['peer'] == [output_unit.process.pid, output['uid'], output['gid']]
                and observation['receipt'] == accepted and accepted['channel'] == 'admitted',
                'Kernel peer credentials or replacement-resistant socket-only mount failed')
        require(published.stat().st_ino == record['socket_inode'] and published.stat().st_gid == output['gid']
                and stat.S_IMODE(published.stat().st_mode) == 0o660, 'Consumer changed the protected publication')
        result['output_observation'] = observation
        result['output_cgroup'] = await cgroup_proof(manager, output_unit)
        result['checks'].update({'actual_v5_bridge': True, 'held_opath_and_durable_publication': True,
                                 'readonly_socket_only_mount': True, 'exact_bidirectional_peer_credentials': True,
                                 'old_path_replacement_ignored': True, 'distinct_uid_peer_mount_bus_denied': True})
        result['ok'] = True
    except BaseException:
        result['error'] = traceback.format_exc()
    finally:
        if manager and network:
            # A failed consumer stop must retain its producer/publication.
            # Retry only exact role records, and stop at the first unproven group.
            owned = dict(units)
            for role in ('owntone', 'bluetooth-output'):
                key = OWNER+':'+role
                entry = network.manifest['processes'].get(key)
                if entry is None:
                    continue
                try:
                    if key in owned:
                        await owned[key].stop()
                    else:
                        await manager.stop_saved(entry)
                    require(manager.cgroup_empty(entry), 'The exact owned service cgroup is still populated')
                    network.forget_unit(key)
                    result['checks'][role+'_exact_stop'] = True
                except BaseException:
                    result['ok'] = False
                    result.setdefault('cleanup_errors', []).append(traceback.format_exc())
                    break
            result['checks']['manifest_empty'] = not network.manifest['processes'] and not network.manifest['networks']
            if result.get('publication'):
                result['checks']['published_inode_removed_after_stop'] = not Path(result['publication']['directory']).exists()
            manager.close()
        for descriptor in (source_fd, directory_fd):
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
        if initial is not None:
            try:
                compare_baseline(result, initial, await baseline(Runner()))
            except BaseException:
                result['ok'] = False
                result.setdefault('cleanup_errors', []).append(traceback.format_exc())
        if lock is not None:
            os.close(lock)
        result['ok'] = result['ok'] and bool(result['checks']) and all(result['checks'].values())
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        if directory is not None:
            atomic_json(directory/'result.json', result)
            atomic_json(WORK/'last-result.json', result)
        print(json.dumps({'ok': result['ok'], 'checks': result['checks'],
                          'report': str(directory/'result.json') if directory else None,
                          'error': result.get('error'), 'cleanup_errors': result.get('cleanup_errors')}))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(run()))
