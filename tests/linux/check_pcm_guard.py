#!/usr/bin/env python3
"""Check a guarded, isolated VM Loopback PCM and its gated socket boundary.

This manual root check uses only the disposable candidate's free substream 7.
It checks actual libasound hw/plug opens under the output service's restrictions,
the opened-device identity guard, and inherited control-ioctl enforcement.
Only the virtual Loopback receives 2,400 silent frames per mode; no Bluetooth
device, physical speaker or legacy substream is opened.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
from uuid import uuid4

from shiri.runtime.alsa_configuration import render_pcm_config
from shiri.runtime.alsa_identity import inventory, resolve
from shiri.runtime.identities import DaemonIdentities
from shiri.runtime.network import NetworkManager
from shiri.runtime.system import Runner, root_directory
from shiri.runtime.units import Bind, PCMExec, UnitManager, UnitSpec, VIEW, new_unit

REPOSITORY = Path(__file__).resolve().parents[2]
STATE = Path("/var/lib/shiri-v2-test-runtime")
WORK = Path("/var/lib/shiri-v2-pcm-guard-review")
MAP = Path("/etc/shiri-v2-test/daemon-identities.json")
RESULT = Path("/tmp/shiri-v2-pcm-guard-result.json")
PREFIX = Path("/opt/shiri-v2-next4-deps")
INSTALLATION = "b265eb7d-18fa-4756-8bf6-5277bdb0ef60"
OWNER = "b6786543-7eb2-443d-83b1-65b984123a76"

C_SOURCE = r'''#include <alsa/asoundlib.h>
#include "pcm_identity.h"
int main(int argc, char **argv) {
  snd_pcm_t *pcm = NULL;
  int status;
  if (argc != 6 || getuid() == 0) return 2;
  status = snd_pcm_open(&pcm, "shiri", SND_PCM_STREAM_PLAYBACK, SND_PCM_NONBLOCK);
  if (status < 0) {
    fprintf(stderr, "Actual private PCM open failed: %s\n", snd_strerror(status));
    return 3;
  }
  bool right = pcm_identity_validate(pcm, argv[1]) == 0;
  bool subdevice = pcm_identity_validate(pcm, argv[2]) < 0;
  bool boot = pcm_identity_validate(pcm, argv[3]) < 0;
  bool node = pcm_identity_validate(pcm, argv[4]) < 0;
  bool malformed = pcm_identity_validate(pcm, argv[5]) < 0;
  snd_pcm_close(pcm);
  printf("{\"correct_opened_pcm\":%s,\"wrong_subdevice_denied\":%s,"
         "\"wrong_boot_denied\":%s,\"wrong_node_inode_denied\":%s,"
         "\"duplicate_manifest_denied\":%s}\n",
         right ? "true" : "false", subdevice ? "true" : "false",
         boot ? "true" : "false", node ? "true" : "false",
         malformed ? "true" : "false");
  return right && subdevice && boot && node && malformed ? 0 : 1;
}
'''

CHILD = r'''import json,os,socket,subprocess,time
from pathlib import Path
view = Path('/run/shiri-worker')
checks = {'uid_nonroot':os.getuid()!=0}
status = {line.split(':',1)[0]:line.split(':',1)[1].strip()
          for line in Path('/proc/self/status').read_text().splitlines() if ':' in line}
checks['no_capabilities'] = all(int(status[k],16)==0 for k in ['CapEff','CapPrm','CapBnd','CapAmb'])
checks['no_new_privileges'] = status.get('NoNewPrivs')=='1'
for port in range(3869,3940,10):
    probe = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    try: probe.bind(('127.0.0.1',port)); success=True
    except OSError: success=False
    finally: probe.close()
    checks['tcp_'+str(port)+'_boundary']=success==(port==3939)
for family,kind,label,allowed in [
    (socket.AF_INET,socket.SOCK_DGRAM,'ipv4_udp',True),
    (socket.AF_INET6,socket.SOCK_STREAM,'ipv6_tcp',False),
    (socket.AF_INET6,socket.SOCK_DGRAM,'ipv6_udp',False),
]:
    probe=socket.socket(family,kind)
    try: probe.bind(('127.0.0.1' if family==socket.AF_INET else '::1',0)); success=True
    except OSError: success=False
    finally: probe.close()
    checks[label+'_boundary']=success==allowed
release=json.loads((view/'gate/ready.json').read_text())
checks['exact_invocation_released']=release.get('invocation_id')==os.environ.get('INVOCATION_ID')
for name in ['CONTROL_NODE','PEER_NODE']:
    try: fd=os.open(os.environ[name],os.O_RDWR|os.O_NONBLOCK)
    except OSError: success=False
    else: os.close(fd); success=True
    checks[name+'_boundary']=success==(name=='CONTROL_NODE')
results = {}
for mode in ['plain','plug']:
    env = {**os.environ,'ALSA_CONFIG_PATH':str(view/(mode+'.conf'))}
    reply = subprocess.run([str(view/'guard'),*(str(view/(name+'.json')) for name in
              ['correct','subdevice','boot','node','duplicate'])],env=env,
              capture_output=True,text=True,timeout=5)
    results[mode] = {'exit_code':reply.returncode,'stdout':reply.stdout,'stderr':reply.stderr}
    if reply.returncode==0:
        results[mode]['checks']=json.loads(reply.stdout)
        checks[mode+'_guarded_open']=all(results[mode]['checks'].values())
    else: checks[mode+'_guarded_open']=False
    control = os.environ['CONTROL_NODE']
    endpoint = json.loads((view/'correct.json').read_text())
    playback = subprocess.run([str(view/'alsa-probe'),control,'shiri',
               *(str(endpoint[key]) for key in ['card_index','device','subdevice'])],
               env=env,capture_output=True,text=True,timeout=10)
    results[mode]['filter_probe']={'exit_code':playback.returncode,'stdout':playback.stdout,'stderr':playback.stderr}
    checks[mode+'_filtered_playback']=playback.returncode==0 and json.loads(playback.stdout)['ok']
Path('/run/shiri-worker/state/observed.json').write_text(json.dumps({
    'checks':checks,'modes':results,'uid':os.getuid(),'pid':os.getpid(),'status':status},indent=2))
while True: time.sleep(1)
'''


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def closed_slot():
    for direction in ("pcm0p", "pcm0c", "pcm1p", "pcm1c"):
        path = Path(f"/proc/asound/Loopback/{direction}/sub7/status")
        require(path.read_text().strip() == "closed", f"Candidate substream 7 is busy: {path}")


def header_image():
    patch = (REPOSITORY / "install/patches/owntone-29.3-pcm-identity.patch").read_text()
    name = "src/outputs/pcm_identity.h"
    block = patch.split(f"diff --git a/{name} b/{name}\n", 1)[1].split("\ndiff --git ", 1)[0]
    active, lines = False, []
    for line in block.splitlines():
        if line.startswith("@@"):
            active = True
        elif active and line.startswith(("+", " ")):
            lines.append(line[1:])
    require(lines, "The pinned backend guard source is missing")
    return "\n".join(lines) + "\n"


async def run():
    result = {"started_at": datetime.now(timezone.utc).isoformat(), "ok": False, "checks": {}}
    manager = network = unit = pin = None
    key = OWNER + ":owntone"
    await asyncio.to_thread(RESULT.write_text, json.dumps(result, indent=2))
    try:
        require(sys.platform == "linux" and os.getuid() == 0, "This guarded check requires Linux root")
        closed_slot()
        root_directory(WORK)
        network = NetworkManager(STATE, Runner())
        require(network.installation_id == INSTALLATION, "The candidate installation identity changed")
        require(not network.manifest.get("processes") and not network.manifest.get("networks"),
                "Candidate ownership must be empty before this isolated check")
        account = DaemonIdentities(MAP).load().account("slot7.output")
        candidates = [item for item in inventory() if item.fingerprint
                      and item.fingerprint.get("kind") == "virtual" and item.device == 1 and item.subdevice == 7]
        require(len(candidates) == 1, "The authentic Loopback DEV1/SUBDEV7 endpoint is missing or ambiguous")
        pin = resolve(candidates[0].fingerprint, conversion=False)
        directory = WORK / uuid4().hex
        directory.mkdir(mode=0o700)
        state = directory / "state"
        state.mkdir(mode=0o700)
        os.chown(state, account["uid"], account["gid"])
        header = directory / "pcm_identity.h"
        header.write_text(header_image())
        source = directory / "guard.c"
        source.write_text(C_SOURCE)
        helper = directory / "guard"
        libraries = await asyncio.to_thread(subprocess.run, ["pkg-config", "--cflags", "--libs", "alsa", "json-c"],
                                            capture_output=True, text=True, timeout=5, check=True)
        await asyncio.to_thread(subprocess.run, ["/usr/bin/cc", "-std=c11", "-D_GNU_SOURCE", "-Wall", "-Wextra", "-Werror", "-O2",
                                str(source), *libraries.stdout.split(), "-o", str(helper)],
                                capture_output=True, text=True, timeout=20, check=True)
        helper.chmod(0o555)
        pcm_exec = PREFIX / 'libexec/shiri-pcm-exec'
        probe = directory / 'alsa-probe'
        for native_source, target, link in [
            (REPOSITORY / 'tests/native/probe_pcm_exec.c', probe,
             [str(REPOSITORY / 'tests/native/pcm_exec_requests.c'), '-lasound']),
        ]:
            await asyncio.to_thread(subprocess.run, ['/usr/bin/cc','-std=c11','-Wall','-Wextra','-Werror','-O2',
                                    str(native_source),*link,'-o',str(target)],
                                    capture_output=True,text=True,timeout=20,check=True)
            target.chmod(0o555)
        result["guard_header_sha256"] = hashlib.sha256(header.read_bytes()).hexdigest()
        result["pcm_exec_source_sha256"] = hashlib.sha256((REPOSITORY / 'native/shiri_pcm_exec.c').read_bytes()).hexdigest()
        result["pcm_exec_binary_sha256"] = hashlib.sha256(pcm_exec.read_bytes()).hexdigest()
        result["actual_endpoint"] = pin.manifest
        manifests = {"correct": pin.manifest,
                     "subdevice": {**pin.manifest, "subdevice": 6},
                     "boot": {**pin.manifest, "boot_id": "00000000-0000-4000-8000-000000000000"},
                     "node": {**pin.manifest, "node_st_ino": pin.manifest["node_st_ino"] + 1}}
        binds = [Bind(str(helper), str(VIEW / "guard")),
                 Bind(str(probe), str(VIEW / 'alsa-probe')), Bind(str(state), str(VIEW / "state"), True)]
        for name, manifest in manifests.items():
            path = directory / (name + ".json")
            path.write_text(json.dumps(manifest))
            path.chmod(0o444)
            binds.append(Bind(str(path), str(VIEW / path.name)))
        duplicate = directory / "duplicate.json"
        duplicate.write_text('{"version":1,' + json.dumps(pin.manifest)[1:])
        duplicate.chmod(0o444)
        binds.append(Bind(str(duplicate), str(VIEW / duplicate.name)))
        for name, conversion in [("plain", False), ("plug", True)]:
            path = directory / (name + ".conf")
            path.write_text(render_pcm_config(pin, conversion))
            path.chmod(0o444)
            binds.append(Bind(str(path), str(VIEW / path.name)))
        script = directory / "probe.py"
        script.write_text(CHILD)
        script.chmod(0o444)
        binds.extend([Bind(str(script), str(VIEW / "probe.py")), Bind(pin.playback_node, pin.playback_node, True)])
        card = pin.manifest["card_index"]
        control_node = f'/dev/snd/controlC{card}'
        binds.append(Bind(control_node, control_node, True))
        python = await asyncio.to_thread(Path('/usr/bin/python3').resolve)
        filtered = PCMExec(str(pcm_exec), (str(python), str(VIEW / "probe.py")))
        spec = UnitSpec(new_unit("b265eb7d", OWNER, "owntone"), "owntone", account["name"], account["name"],
                        filtered.command(), binds=tuple(binds),
                        devices=(pin.playback_node,control_node), supplementary_groups=("audio",), listen_port=3939,
                        inaccessible=("/run/dbus/system_bus_socket", str(MAP.parent)),
                        environment=(f"CONTROL_NODE=/dev/snd/controlC{card}", f"PEER_NODE=/dev/snd/pcmC{card}D0p"),
                        pcm_exec=filtered)
        manager = UnitManager(network.runner, network,
                              bind_policy_helper=PREFIX / 'libexec/shiri-bind-policy',
                              pcm_exec_helper=pcm_exec)
        unit = await manager.start(key, spec, directory / "unit.log")
        result['unit_policy'] = unit.identity()
        require(unit.entry.get('policy_version') == 4 and unit.entry.get('bind_policy'),
                'The actual combined gated PCM policy was not durably admitted')
        descriptor = manager.open_cgroup(unit.entry)
        try:
            await manager.bind_policy.run('verify', descriptor, unit.entry['boot_id'],
                                          unit.entry['listen_port'], unit.entry['bind_policy'])
        finally:
            os.close(descriptor)
        result['checks']['kernel_policy_reverified'] = True
        for _ in range(100):
            if (state / "observed.json").exists():
                break
            require(unit.alive, "The restricted PCM probe exited before recording evidence")
            await asyncio.sleep(0.05)
        observation = json.loads((state / "observed.json").read_text())
        require(observation["uid"] == account["uid"] and observation["pid"] == unit.process.pid,
                "Probe evidence does not belong to the exact launched output process")
        result["observation"] = observation
        require(all(observation["checks"].values()), "Actual restricted PCM validation failed")
        result["checks"]["actual_hw_and_plug_guard"] = True
        result["ok"] = True
    except BaseException as exc:
        result["error"] = traceback.format_exc()
        if isinstance(exc, subprocess.CalledProcessError):
            result["command_stderr"] = exc.stderr
    finally:
        if unit:
            try:
                await unit.stop()
                network.forget_unit(key)
                result["checks"]["exact_cgroup_terminated"] = manager.cgroup_empty(unit.entry)
            except BaseException:
                result["cleanup_error"] = traceback.format_exc()
                result["ok"] = False
        if pin:
            pin.close()
        if manager:
            manager.close()
        if network:
            result["checks"]["ownership_empty"] = not network.manifest.get("processes") and not network.manifest.get("networks")
        try:
            closed_slot()
            result["checks"]["loopback_closed"] = True
        except BaseException:
            result["cleanup_slot_error"] = traceback.format_exc()
            result["ok"] = False
        result["ok"] = result["ok"] and all(result["checks"].values())
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(RESULT.write_text, json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    report = asyncio.run(run())
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["ok"] else 1)
