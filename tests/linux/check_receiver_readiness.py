#!/usr/bin/env python3
"""Manual real-kernel receiver admission under the broker's bounded capabilities.

Requires the stopped, empty b265 candidate and its provisioned v2 identity map.
An internal dummy interface in one isolated netns carries only TEST-NET IPv4;
there is no host link, DHCP, LAN, PTP, audio, Bluetooth or backend execution.
Run under an outer systemd watchdog with the production broker capability set
and host mount propagation. This fixture refuses CAP_SYS_PTRACE explicitly.
"""
from __future__ import annotations

# These bounded filesystem observations are the actual kernel evidence.
# ruff: noqa: ASYNC240

import asyncio
from copy import deepcopy
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
from uuid import UUID, uuid4

from shiri.runtime.bind_policy import trusted_file
from shiri.runtime.identities import DaemonIdentities
from shiri.runtime.network import NetworkManager
from shiri.runtime.receiver_readiness import wait_receiver_ready
from shiri.runtime.system import Runner, RuntimeFailure, atomic_json, boot_id, process_birth, root_directory
from shiri.runtime.units import Bind, UnitManager, UnitSpec, VIEW, is_cgroup2, new_unit

BASELINE_SPEC = importlib.util.spec_from_file_location('manual_host_preservation', Path(__file__).with_name('host_preservation.py'))
BASELINE_MODULE = importlib.util.module_from_spec(BASELINE_SPEC)
BASELINE_SPEC.loader.exec_module(BASELINE_MODULE)
compare_baseline = BASELINE_MODULE.compare_baseline

STATE = Path('/var/lib/shiri-v2-test-runtime')
RUNTIME = Path('/run/shiri-v2-test')
MAP = Path('/etc/shiri-v2-test/daemon-identities.json')
WORK = Path('/var/lib/shiri-v2-receiver-readiness-review')
INSTALLATION = 'b265eb7d-18fa-4756-8bf6-5277bdb0ef60'
OWNER = '79c446ee-9b4a-459d-a042-a6b062da1e37'
KEY = OWNER+':shairport'
NAMESPACE = 'shiri_rx_b265eb7d_'+UUID(OWNER).hex[:12]
INTERFACE = 'sr'+UUID(OWNER).hex[:12]
ADDRESS = '192.0.2.91'
LEGACY_PID = 2444

PAYLOAD = r'''import json,os,signal,socket,sys,time
from pathlib import Path
mode=sys.argv[1];state=Path('/run/shiri-worker/state');main=os.getpid()
def exit_now(*_): raise SystemExit(0)
signal.signal(signal.SIGTERM,exit_now)
def idle():
    while True: time.sleep(.02)
def ready(listener=None):
    status={line.split(':',1)[0]:line.split(':',1)[1].strip()
            for line in Path('/proc/self/status').read_text().splitlines() if ':' in line}
    data={'pid':os.getpid(),'main_pid':main,'uid':os.getuid(),'gid':os.getgid(),
          'capabilities':{k:status[k] for k in ['CapEff','CapPrm','CapInh','CapBnd','CapAmb']},
          'no_new_privileges':status['NoNewPrivs'],'socket_inode':os.fstat(listener.fileno()).st_ino if listener else None}
    temp=state/'ready.tmp';temp.write_text(json.dumps(data));temp.replace(state/'ready.json')
def listen(address):
    listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    listener.bind((address,7000));listener.listen(2);ready(listener);idle()
if mode=='foreign':
    child=os.fork()
    if child==0: listen('0.0.0.0')
    idle()
elif mode=='wildcard': listen('0.0.0.0')
elif mode=='lan': listen('192.0.2.91')
elif mode=='loopback': listen('127.0.0.1')
elif mode=='exit':
    ready()
    while not (state/'exit.json').exists():time.sleep(.005)
    raise SystemExit(23)
elif mode=='idle': ready();idle()
else: raise RuntimeError('Unknown fixture mode')
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
            'Run this fixture with the reviewed production broker capabilities')
    return {key: hex(value) for key, value in fields.items()}


def acquire_lock():
    root_directory(RUNTIME)
    path = RUNTIME/'broker.lock'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                and not info.st_mode & 0o077, 'The candidate broker lock is not root protected')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        named = path.lstat()
        require((named.st_dev, named.st_ino) == (info.st_dev, info.st_ino), 'The candidate broker lock was replaced')
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


async def child_ready(path, unit):
    deadline = asyncio.get_running_loop().time()+3
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        require(unit.alive, 'The exact fixture service exited before its observation')
        await asyncio.sleep(.02)
    raise RuntimeError('The fixture process did not report its bounded startup')


async def exact_unit(manager, unit):
    expected = deepcopy(unit.identity())
    actual = await manager.inspect(expected['unit'])
    require(actual is not None and actual.get('MainPID') == unit.process.pid
            and actual.get('ActiveState') in {'active', 'activating'}, 'The exact fixture MainPID is not active')
    manager.verify(expected, actual)
    descriptor = manager.open_cgroup(expected)
    try:
        held = os.fstat(descriptor)
        require(is_cgroup2(descriptor) and held.st_ino == expected['cgroup_inode'],
                'The observed fixture cgroup is not the admitted kernel inode')
        require(f'0::{expected["control_group"]}' in Path(f'/proc/{unit.process.pid}/cgroup').read_text().splitlines(),
                'The fixture MainPID does not belong to the admitted cgroup')
        return {'unit': expected['unit'], 'invocation_id': expected['invocation_id'],
                'pid': unit.process.pid, 'birth': process_birth(unit.process.pid),
                'cgroup_dev': held.st_dev, 'cgroup_inode': held.st_ino}
    finally:
        os.close(descriptor)


async def stopped(manager, network, unit=None):
    entry = network.manifest['processes'].get(KEY)
    if entry is None:
        return
    if unit is None:
        await manager.stop_saved(deepcopy(entry))
    else:
        await unit.stop()
    require(manager.cgroup_empty(entry), 'The exact receiver fixture cgroup is still populated')
    network.forget_unit(KEY)


async def reject(unit, receiver, account, timeout, expected_error, *, before=None):
    started = asyncio.get_running_loop().time()
    task = asyncio.create_task(wait_receiver_ready(unit, receiver, account, timeout=timeout))
    try:
        if before:
            await before()
        try:
            await task
        except RuntimeFailure as exc:
            error = str(exc)
        else:
            raise RuntimeError('A receiver without exact admitted listener ownership was accepted')
        elapsed = asyncio.get_running_loop().time()-started
        require(elapsed <= timeout+2 and any(part in error.lower() for part in expected_error),
                'The receiver rejection was unbounded or did not exercise the intended ownership gate')
        return {'error': error, 'elapsed_seconds': elapsed}
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def run():
    result = {'ok': False, 'started_at': datetime.now(timezone.utc).isoformat(), 'checks': {}, 'cases': []}
    runner = Runner()
    manager = network = lock = namespace_fd = directory = initial = unit = None
    namespace_created = False
    try:
        require(sys.platform == 'linux' and os.getuid() == 0, 'This manual fixture requires Linux root')
        result['broker_capabilities'] = capability_admission()
        lock = acquire_lock()
        identities = DaemonIdentities(MAP).load()
        require(identities.installation_id == INSTALLATION and identities.runtime_state_dir == STATE
                and identities.runtime_dir == RUNTIME, 'The fixture requires the dedicated b265 v2 identity map')
        network = NetworkManager(STATE, runner)
        require(network.installation_id == INSTALLATION and not network.manifest['processes']
                and not network.manifest['networks'], 'The candidate ownership manifest must initially be empty')
        initial = await baseline(runner)
        result['baseline_before'] = initial
        require(initial['legacy_birth'], 'The expected untouched legacy application is absent')
        root_directory(WORK)
        directory = WORK/uuid4().hex
        directory.mkdir(mode=0o700)
        atomic_json(directory/'result.json', result)
        namespace = Path('/run/netns')/NAMESPACE
        require(not namespace.exists() and not namespace.is_symlink(),
                'The dedicated fixture namespace already exists; preserve it for exact recovery')
        receiver = {'namespace': NAMESPACE, 'interface': INTERFACE, 'ip': ADDRESS, 'boot_id': boot_id(), 'inode': None}
        require(receiver['boot_id'], 'The kernel boot identity is unavailable')
        atomic_json(directory/'namespace.json', receiver)
        await runner.run(['/usr/sbin/ip', 'netns', 'add', NAMESPACE])
        namespace_created = True
        namespace_fd = os.open(namespace, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        held = os.fstat(namespace_fd)
        require(os.path.ismount(namespace), 'The new namespace is not a real kernel bind mount')
        receiver.update(inode=held.st_ino, device=held.st_dev)
        atomic_json(directory/'namespace.json', receiver)
        prefix = ['/usr/sbin/ip', 'netns', 'exec', NAMESPACE, '/usr/sbin/ip']
        await runner.run(prefix+['link', 'set', 'lo', 'up'])
        await runner.run(prefix+['link', 'add', INTERFACE, 'type', 'dummy'])
        await runner.run(prefix+['address', 'add', ADDRESS+'/24', 'dev', INTERFACE])
        await runner.run(prefix+['link', 'set', INTERFACE, 'up'])
        account = identities.account('slot7.receiver')
        python = str(trusted_file(Path('/usr/bin/python3').resolve(strict=True), executable=True))
        script = directory/'receiver.py'
        script.write_text(PAYLOAD)
        script.chmod(0o444)
        trusted_file(script)
        result['payload_sha256'] = hashlib.sha256(script.read_bytes()).hexdigest()
        manager = UnitManager(runner, network)
        runner.unit_manager = manager
        for mode in ['wildcard', 'lan', 'foreign', 'loopback', 'idle', 'exit', 'namespace']:
            state = directory/mode
            state.mkdir(mode=0o700)
            os.chown(state, account['uid'], account['gid'])
            command_mode = 'wildcard' if mode == 'namespace' else mode
            spec = UnitSpec(new_unit('b265eb7d', OWNER, 'shairport'), 'shairport', account['name'], account['name'],
                            (python, '-I', '-S', str(VIEW/'receiver.py'), command_mode), namespace=str(namespace),
                            binds=(Bind(str(script), str(VIEW/'receiver.py')), Bind(str(state), str(VIEW/'state'), True)),
                            inaccessible=('/run/dbus/system_bus_socket', str(MAP.parent), str(STATE)))
            unit = await manager.start(KEY, spec, directory/(mode+'.log'))
            observation = await child_ready(state/'ready.json', unit)
            require(observation['main_pid'] == unit.process.pid and observation['uid'] == account['uid']
                    and observation['gid'] == account['gid'] and observation['no_new_privileges'] == '1'
                    and all(int(value, 16) == 0 for value in observation['capabilities'].values()),
                    'The actual receiver fixture is not capability-free under its fixed UID/GID')
            case = {'mode': mode, 'unit': await exact_unit(manager, unit), 'process': observation}
            if mode in {'wildcard', 'lan'}:
                case['readiness'] = await wait_receiver_ready(unit, receiver, account, timeout=2)
                require(case['readiness']['listeners'][0]['inode'] == observation['socket_inode'],
                        'Readiness did not identify the genuine exact MainPID socket inode')
                case['after'] = await exact_unit(manager, unit)
                require(case['unit'] == case['after'], 'The admitted unit changed during actual readiness')
            elif mode == 'foreign':
                require(observation['pid'] != unit.process.pid and process_birth(observation['pid']),
                        'The foreign-listener case did not launch a separate same-UID process')
                listening = await runner.run(['/usr/sbin/ip', 'netns', 'exec', NAMESPACE,
                                             '/usr/bin/ss', '-H', '-ltn', 'sport', '=', ':7000'])
                require(':7000' in listening.stdout, 'The foreign process has no real kernel listener')
                case['rejection'] = await reject(unit, receiver, account, .3, ['deadline'])
            elif mode == 'loopback':
                case['rejection'] = await reject(unit, receiver, account, 2, ['unexpected credentials or bind address'])
            elif mode == 'idle':
                case['rejection'] = await reject(unit, receiver, account, .3, ['deadline'])
            elif mode == 'exit':
                async def exiting(state=state):
                    await asyncio.sleep(.05)
                    atomic_json(state/'exit.json', {'exit': True}, mode=0o644)
                case['rejection'] = await reject(unit, receiver, account, 2, ['exited', 'disappeared', 'changed', 'authority'],
                                                before=exiting)
                require(case['rejection']['elapsed_seconds'] < 1.5, 'MainPID exit was not detected promptly')
            else:
                mismatch = dict(receiver, inode=receiver['inode']+1)
                case['rejection'] = await reject(unit, mismatch, account, 2, ['namespace changed'])
            result['cases'].append(case)
            await stopped(manager, network, unit)
            unit = None
            result['checks'][mode] = True
        result['ok'] = True
    except BaseException:
        result['error'] = traceback.format_exc()
    finally:
        if manager and network:
            try:
                await stopped(manager, network, unit)
                result['checks']['exact_units_stopped'] = KEY not in network.manifest['processes']
            except BaseException:
                result['ok'] = False
                result.setdefault('cleanup_errors', []).append(traceback.format_exc())
            manager.close()
        if namespace_created:
            try:
                require(manager is not None and KEY not in network.manifest['processes'],
                        'Receiver cgroup termination was not proved; preserve its namespace')
                require(namespace_fd is not None, 'Namespace creation lacked a held inode; preserve its intent')
                held = os.fstat(namespace_fd)
                current = namespace.stat()
                require(boot_id() == receiver['boot_id'] and os.path.ismount(namespace)
                        and (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino)
                        and held.st_ino == receiver['inode'], 'The owned fixture namespace was replaced')
                pids = await runner.run(['/usr/sbin/ip', 'netns', 'pids', NAMESPACE])
                require(not pids.stdout.strip(), 'The fixture namespace still contains processes; preserve it')
                await runner.run(['/usr/sbin/ip', 'netns', 'delete', NAMESPACE])
                result['checks']['held_namespace_removed'] = not namespace.exists()
                atomic_json(directory/'namespace.json', dict(receiver, removed=True))
            except BaseException:
                result['ok'] = False
                result.setdefault('cleanup_errors', []).append(traceback.format_exc())
        if namespace_fd is not None:
            os.close(namespace_fd)
        if network is not None:
            result['checks']['manifest_empty'] = not network.manifest['processes'] and not network.manifest['networks']
        if initial is not None:
            try:
                compare_baseline(result, initial, await baseline(runner))
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
        print(json.dumps({'ok': result['ok'], 'checks': result['checks'], 'error': result.get('error'),
                          'cleanup_errors': result.get('cleanup_errors'),
                          'report': str(directory/'result.json') if directory else None}))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(run()))
