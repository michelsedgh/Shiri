#!/usr/bin/env python3
"""Manual Linux root validation of DHCP-hook address transitions.

Creates only a fresh isolated namespace/dummy interface. It never obtains a
LAN lease or changes host interfaces. --before-hook optionally reproduces the
previous same-IP/new-prefix bug before checking the candidate hook.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback
from uuid import uuid4
from datetime import datetime, timezone

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--hook', type=Path)
parser.add_argument('--before-hook', type=Path)
args = parser.parse_args()
if args.hook is None:
    args.hook = Path(__file__).resolve().parent.parent.parent / 'shiri/runtime/dhclient_hook.py'
if not args.hook.is_file() or args.before_hook is not None and not args.before_hook.is_file():
    parser.error('Run from the candidate checkout or provide existing explicit --hook/--before-hook files')
if sys.platform != 'linux' or os.geteuid() != 0:
    raise SystemExit('Run this manual namespace check explicitly as root on Linux')
RESULT = Path('/tmp/shiri-v2-dhcp-renewal-result.json')
namespace = 'shiri-test-dhcp-' + uuid4().hex[:12]
interface = 'shtestdhcp'
inode = None
report = {'started_at': datetime.now(timezone.utc).isoformat(), 'passed': False,
          'scope': 'Kernel DHCP-hook address transitions in an isolated dummy interface; no DHCP server/router lease claim',
          'checks': {}, 'cleanup': {}}

def command(argv, check=True):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    if check and result.returncode:
        raise RuntimeError(f'{argv[:4]}: {result.stderr[-1500:]}')
    return result

def save_report():
    # Never follow a predictable /tmp report symlink while running as root.
    descriptor, temporary = tempfile.mkstemp(prefix='.shiri-dhcp-result-', dir=RESULT.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, RESULT)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def snapshot():
    links = json.loads(command(['ip', '-j', '-d', 'link', 'show']).stdout)
    addresses = json.loads(command(['ip', '-j', 'addr', 'show']).stdout)
    return {
        'links': sorted((v['ifindex'], v['ifname'], v.get('address'), v.get('ifalias'), v.get('linkinfo', {}).get('info_kind')) for v in links),
        'addresses': sorted((v['ifname'], a['family'], a['local'], a['prefixlen']) for v in addresses for a in v.get('addr_info', [])),
        'namespaces': sorted(line.split()[0] for line in command(['ip', 'netns', 'list']).stdout.splitlines()),
    }

def ns(*argv):
    return command(['ip', 'netns', 'exec', namespace, *argv])

def addresses():
    observed = json.loads(ns('ip', '-j', '-4', 'addr', 'show', 'dev', interface).stdout)
    return sorted(f"{a['local']}/{a['prefixlen']}" for v in observed for a in v.get('addr_info', []))

def prepare():
    ns('ip', '-4', 'addr', 'flush', 'dev', interface)
    ns('ip', 'addr', 'add', '192.0.2.10/24', 'dev', interface)
    ns('ip', 'addr', 'add', '198.51.100.1/32', 'dev', interface)

def hook(path, **changes):
    environment = {'SHIRI_HOST_NETNS': os.readlink('/proc/self/ns/net'), 'interface': interface,
                   'reason': 'RENEW', 'old_ip_address': '192.0.2.10',
                   'old_subnet_mask': '255.255.255.0', 'new_ip_address': '192.0.2.10',
                   'new_subnet_mask': '255.255.255.128'}
    environment.update(changes)
    ns('/usr/bin/env', *(f'{k}={v}' for k, v in environment.items()), '/usr/bin/python3', path)

baseline = snapshot()
try:
    assert os.geteuid() == 0
    path = Path('/run/netns') / namespace
    assert not path.exists() and not path.is_symlink()
    command(['ip', 'netns', 'add', namespace])
    inode = path.stat().st_ino
    ns('ip', 'link', 'add', interface, 'type', 'dummy')
    ns('ip', 'link', 'set', interface, 'up')
    if args.before_hook:
        prepare()
        hook(str(args.before_hook))
        before = addresses()
        assert before == ['192.0.2.10/24', '192.0.2.10/25', '198.51.100.1/32'], before
        report['checks']['original_bug_reproduced'] = before
    prepare()
    hook(str(args.hook))
    after = addresses()
    assert after == ['192.0.2.10/25', '198.51.100.1/32'], after
    report['checks']['same_ip_prefix_replaced'] = after
    prepare()
    hook(str(args.hook), reason='REBIND', new_ip_address='192.0.2.11', new_subnet_mask='255.255.255.192')
    after = addresses()
    assert after == ['192.0.2.11/26', '198.51.100.1/32'], after
    report['checks']['address_and_prefix_replaced'] = after
    prepare()
    hook(str(args.hook), new_subnet_mask='255.255.255.0')
    after = addresses()
    assert after == ['192.0.2.10/24', '198.51.100.1/32'], after
    report['checks']['unchanged_lease_retained'] = after
    report['passed'] = True
except BaseException:
    report['failure'] = traceback.format_exc()
finally:
    try:
        if inode is not None:
            path = Path('/run/netns') / namespace
            assert path.stat().st_ino == inode, 'Test namespace inode changed; retaining it for inspection'
            command(['ip', 'netns', 'delete', namespace])
        report['cleanup']['test_namespace_removed'] = not (Path('/run/netns') / namespace).exists()
        report['cleanup']['host_baseline_preserved'] = snapshot() == baseline
        report['passed'] = report['passed'] and all(report['cleanup'].values())
    except BaseException:
        report['cleanup_failure'] = traceback.format_exc()
        report['passed'] = False
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    save_report()
raise SystemExit(0 if report['passed'] else 1)
