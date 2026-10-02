#!/usr/bin/env python3
"""Manual isolated systemd launch/SIGKILL/recovery proof; no audio or networks.

Requires the empty b265 candidate and a trusted root-owned source installation.
Run under an external 180-second systemd watchdog. The disposable broker owner
is killed at three exact launch stages; the surviving units are recovered only
through the candidate's durable identity and kernel-held cgroup authority.
"""
from __future__ import annotations

# Manual bounded filesystem observations are part of the kernel evidence.
# ruff: noqa: ASYNC240

import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys
import traceback
from uuid import uuid4

from shiri.runtime.bind_policy import BindPolicy
from shiri.runtime.identities import DaemonIdentities
from shiri.runtime.network import NetworkManager
from shiri.runtime.system import Runner, atomic_json, process_birth, root_directory
from shiri.runtime.units import Bind, UnitManager, UnitSpec, VIEW, new_unit

STATE = Path('/var/lib/shiri-v2-test-runtime')
WORK = Path('/var/lib/shiri-v2-unit-recovery-review')
PREFIX = Path('/opt/shiri-v2-next4-deps')
MAP = Path('/etc/shiri-v2-test/daemon-identities.json')
RESULT = Path('/tmp/shiri-v2-unit-recovery-result.json')
OWNER = 'b6786543-7eb2-443d-83b1-65b984123a76'
KEY = OWNER + ':owntone'
INSTALLATION = 'b265eb7d-18fa-4756-8bf6-5277bdb0ef60'
PAYLOAD = r'''import json,os,socket,time
from pathlib import Path
checks={}
for port in range(3869,3940,10):
    probe=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    try: probe.bind(('127.0.0.1',port)); success=True
    except OSError: success=False
    finally: probe.close()
    checks['port_'+str(port)]=success==(port==3939)
Path('/run/shiri-worker/state/payload.json').write_text(json.dumps({
    'uid':os.getuid(),'pid':os.getpid(),'checks':checks}))
while True: time.sleep(.1)
'''


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def baseline():
    return {'legacy_app_birth': process_birth(2444), 'loopback': {
        f'{direction}/sub{sub}': '\n'.join(line for line in
            Path(f'/proc/asound/Loopback/{direction}/sub{sub}/status').read_text().splitlines()
            if line.strip() == 'closed' or line.startswith(('state:', 'owner_pid')))
        for direction in ['pcm0p', 'pcm0c', 'pcm1p', 'pcm1c'] for sub in [0, 2]}}


def manager(network, runner):
    owned = UnitManager(runner, network, bind_policy_helper=PREFIX / 'libexec/shiri-bind-policy')
    runner.unit_manager = owned
    return owned


async def child(phase, ready, directory):
    async def publish(stage, **details):
        atomic_json(ready, {'stage': stage, 'pid': os.getpid(), 'birth': process_birth(os.getpid()), **details})
        await asyncio.Event().wait()

    class StageRunner(Runner):
        async def run(self, args, **kwargs):
            result = await super().run(args, **kwargs)
            if phase == 'gated' and args[0] == '/usr/bin/systemd-run':
                await publish('gated')
            return result

    class StagePolicy(BindPolicy):
        async def run(self, action, *args, **kwargs):
            result = await super().run(action, *args, **kwargs)
            if phase == 'attached' and action == 'attach':
                await publish('attached', attached_proof=result)
            return result

    runner = StageRunner()
    network = NetworkManager(STATE, runner)
    require(network.installation_id == INSTALLATION and not network.manifest['processes']
            and not network.manifest['networks'], 'Candidate ownership must be empty')
    account = DaemonIdentities(MAP).load().account('slot7.output')
    owned = manager(network, runner)
    if phase == 'attached':
        owned.bind_policy = StagePolicy(PREFIX / 'libexec/shiri-bind-policy')
    spec = UnitSpec(new_unit('b265eb7d', OWNER, 'owntone'), 'owntone', account['name'], account['name'],
                    (str(Path('/usr/bin/python3').resolve()), str(VIEW / 'probe.py')),
                    binds=(Bind(str(directory / 'probe.py'), str(VIEW / 'probe.py')),
                           Bind(str(directory / 'state'), str(VIEW / 'state'), True)), listen_port=3939)
    unit = await owned.start(KEY, spec, directory / 'unit.log')
    for _ in range(100):
        if (directory / 'state/payload.json').exists():
            break
        require(unit.alive, 'Released unit exited before payload evidence')
        await asyncio.sleep(.02)
    payload = json.loads((directory / 'state/payload.json').read_text())
    require(payload['uid'] == account['uid'] and payload['pid'] == unit.process.pid
            and all(payload['checks'].values()), 'Released payload did not enforce its exact port boundary')
    # Drain the startup journal before killing its idle owner. A reader still
    # writing its initial backlog can exit on EPIPE and hide an orphan bug.
    await asyncio.sleep(1)
    require(unit.logger.alive, 'The live journal reader exited before owner-death observation')
    await publish('released', payload=payload, logger=unit.logger.identity())


async def run():
    result = {'started_at': datetime.now(timezone.utc).isoformat(), 'ok': False, 'stages': [], 'checks': {}}
    atomic_json(RESULT, result, mode=0o644)
    initial = None
    proc = recovery = logger_fd = None
    started = False
    try:
        require(sys.platform == 'linux' and os.getuid() == 0, 'Linux root is required')
        existing = NetworkManager(STATE, Runner())
        require(existing.installation_id == INSTALLATION and not existing.manifest['processes']
                and not existing.manifest['networks'], 'Candidate ownership must be empty before this check')
        initial = baseline()
        require(initial['legacy_app_birth'], 'The expected untouched legacy application is missing')
        root_directory(WORK)
        for phase in ['gated', 'attached', 'released']:
            directory = WORK / uuid4().hex
            directory.mkdir(mode=0o700)
            state = directory / 'state'
            state.mkdir(mode=0o700)
            account = DaemonIdentities(MAP).load().account('slot7.output')
            os.chown(state, account['uid'], account['gid'])
            script = directory / 'probe.py'
            script.write_text(PAYLOAD)
            script.chmod(0o444)
            ready = directory / 'ready.json'
            stderr = (directory / 'owner.log').open('wb')
            try:
                proc = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()),
                            '--child', phase, '--ready', str(ready), '--directory', str(directory),
                            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2])},
                            stdout=stderr, stderr=asyncio.subprocess.STDOUT)
                started = True
            finally:
                stderr.close()
            for _ in range(400):
                if ready.exists():
                    break
                require(proc.returncode is None, 'Disposable owner exited: ' + (directory / 'owner.log').read_text())
                await asyncio.sleep(.025)
            require(ready.exists(), 'Disposable owner did not reach its bounded launch stage')
            stage = json.loads(ready.read_text())
            require(stage['stage'] == phase and stage['pid'] == proc.pid
                    and stage['birth'] == process_birth(proc.pid), 'Launch-stage evidence belongs to a different owner')
            if phase == 'released':
                require(stage['logger']['birth'] == process_birth(stage['logger']['pid']), 'Journal reader identity changed')
                logger_fd = os.pidfd_open(stage['logger']['pid'])
            owner_fd = os.pidfd_open(proc.pid)
            try:
                signal.pidfd_send_signal(owner_fd, signal.SIGKILL)
                await asyncio.wait_for(proc.wait(), timeout=3)
            finally:
                os.close(owner_fd)
            require(proc.returncode == -signal.SIGKILL, 'The disposable owner was not killed at the selected stage')
            proc = None
            runner = Runner()
            network = NetworkManager(STATE, runner)
            recovery = manager(network, runner)
            entry = network.manifest['processes'].get(KEY)
            require(entry and entry['policy_version'] == 3, 'Crash lost the exact gated launch intent')
            actual = await recovery.inspect(entry['unit'])
            require(actual and actual['ActiveState'] == 'active', 'The independently managed unit did not survive owner death')
            recovery.verify(entry, actual)
            stage['intent_before_recovery'] = entry.copy()
            stage['checks'] = {'owner_sigkill': True, 'exact_live_unit_survived': True,
                              'payload_released_only_after_admission': (state / 'payload.json').exists() == (phase == 'released')}
            if phase == 'released':
                for _ in range(40):
                    if process_birth(stage['logger']['pid']) != stage['logger']['birth']:
                        break
                    await asyncio.sleep(.025)
                stage['checks']['journal_reader_terminated_on_owner_death'] = (
                    process_birth(stage['logger']['pid']) != stage['logger']['birth'])
            if phase in {'attached', 'released'}:
                descriptor = recovery.open_cgroup(entry)
                try:
                    proof = stage.get('attached_proof', entry.get('bind_policy'))
                    await recovery.bind_policy.run('verify', descriptor, entry['boot_id'], entry['listen_port'], proof)
                    stage['checks']['kernel_policy_survived_owner_death'] = True
                finally:
                    os.close(descriptor)
            await network.recover()
            stage['checks']['manifest_empty_after_exact_recovery'] = not network.manifest['processes'] and not network.manifest['networks']
            stage['checks']['exact_cgroup_empty'] = recovery.cgroup_empty(entry)
            stage['checks']['gate_removed'] = not Path(entry['gate']['directory']).exists()
            if phase == 'released':
                for _ in range(40):
                    if process_birth(stage['logger']['pid']) != stage['logger']['birth']:
                        break
                    await asyncio.sleep(.025)
                stage['checks']['journal_reader_terminated'] = process_birth(stage['logger']['pid']) != stage['logger']['birth']
                if not stage['checks']['journal_reader_terminated']:
                    # The handle was captured before SIGKILL; cleanup cannot hit a reused PID.
                    with suppress(ProcessLookupError):
                        signal.pidfd_send_signal(logger_fd, signal.SIGTERM)
                os.close(logger_fd)
                logger_fd = None
            stage['checks']['legacy_unchanged'] = baseline() == initial
            stage['legacy_before'] = initial
            stage['legacy_after'] = baseline()
            result['stages'].append(stage)
            atomic_json(RESULT, result, mode=0o644)
            require(all(stage['checks'].values()), 'A real crash/recovery assertion failed: ' + json.dumps(stage['checks']))
            recovery.close()
            recovery = None
        result['ok'] = True
    except BaseException:
        result['error'] = traceback.format_exc()
    finally:
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        if logger_fd is not None:
            with suppress(ProcessLookupError):
                signal.pidfd_send_signal(logger_fd, signal.SIGTERM)
            os.close(logger_fd)
        if recovery:
            recovery.close()
        try:
            runner = Runner()
            network = NetworkManager(STATE, runner)
            require(not network.manifest['networks'] and set(network.manifest['processes']) <= {KEY},
                    'Unexpected candidate ownership appeared; preserve it for inspection')
            final = manager(network, runner)
            try:
                if started:
                    await network.recover()
                result['checks']['final_ownership_empty'] = not network.manifest['processes'] and not network.manifest['networks']
            finally:
                final.close()
            result['checks']['legacy_unchanged'] = initial is not None and baseline() == initial
        except BaseException:
            result['cleanup_error'] = traceback.format_exc()
            result['ok'] = False
        result['ok'] = result['ok'] and all(result['checks'].values())
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(RESULT, result, mode=0o644)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child', choices=['gated', 'attached', 'released'])
    parser.add_argument('--ready', type=Path)
    parser.add_argument('--directory', type=Path)
    options = parser.parse_args()
    if options.child:
        asyncio.run(child(options.child, options.ready, options.directory))
    else:
        report = asyncio.run(run())
        print(json.dumps(report, indent=2))
        raise SystemExit(0 if report['ok'] else 1)
