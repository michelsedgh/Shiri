#!/usr/bin/env python3
"""Compile actual pinned transport-control seams, with no network/device use.

Unchanged upstream callbacks are checked against the supplied real checkout;
default CI compiles reviewed bounded excerpts plus exact patch postimages.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / 'install/patches/owntone-29.3-transport-control.patch'
spec = importlib.util.spec_from_file_location('source_seam_extract', ROOT/'tests/native/check_source_transition.py')
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)


def run_tests(*, source: Path | None = None, compiler=None, sanitize=True):
    compiler = compiler or shutil.which('clang') or shutil.which('cc')
    if not compiler:
        raise RuntimeError('A C compiler is required for actual transport-control tests')
    def native(name):
        return (source/name).read_text() if source else extract.postimage(PATCH, name)
    raop, cast = native('src/outputs/raop.c'), native('src/outputs/cast.c')
    unchanged = ROOT/'tests/native/transport_upstream.h'
    baseline = unchanged.read_text()
    completions = '\n'.join(extract.function(baseline, name) for name in (
        'raop_status', 'session_failure', 'raop_check_cseq', 'raop_cb_set_volume', 'raop_cb_metadata'))
    shutdown = extract.function(baseline, 'cast_session_shutdown')
    types = extract.between(baseline, '#define CAST_STATE_F_STARTUP', 'struct cast_rtcp_packet_feedback')
    if source:
        for name in ('raop_status', 'session_failure', 'raop_check_cseq', 'raop_cb_set_volume', 'raop_cb_metadata'):
            if extract.function(raop, name) != extract.function(baseline, name):
                raise RuntimeError(f'Unreviewed change in actual upstream completion: {name}')
        if extract.function(cast, 'cast_session_shutdown') != shutdown:
            raise RuntimeError('Unreviewed change in actual Cast shutdown seam')
        for name in ('cast_status', 'cast_cb_volume', 'cast_msg_parse_free'):
            if extract.function(cast, name) != extract.function(baseline, name):
                raise RuntimeError(f'Unreviewed change in actual Cast completion: {name}')
    parts = {
        'test_transport_raop': {
            'ACTUAL_RAOP_OWNERSHIP': extract.between(raop, '/* A connection teardown', 'static int\nraop_send_req_teardown'),
            'ACTUAL_RAOP_FREE': extract.function(raop, 'session_free'),
            'UNCHANGED_RAOP_COMPLETIONS': completions,
            'ACTUAL_RAOP_FLUSH_COMPLETION': extract.function(raop,'raop_check_flush_cseq') + extract.function(raop,'raop_cb_flush'),
            'ACTUAL_RAOP_FLUSH_SEND': extract.function(raop, 'raop_send_req_flush'),
            'ACTUAL_RAOP_ADMISSION': extract.function(raop, 'raop_set_volume_one') + extract.function(raop, 'raop_device_flush'),
        },
        'test_transport_cast': {
            'ACTUAL_CAST_TYPES': types,
            'ACTUAL_CAST_ENTRY': extract.between(cast, 'struct cast_reply_entry\n', 'struct cast_session\n'),
            'ACTUAL_CAST_REGISTRY': '\n'.join(extract.function(cast,name) for name in ('cast_reply_now','cast_reply_schedule','cast_reply_take')),
            'ACTUAL_CAST_SEND_PARSE': '\n'.join(extract.function(cast,name) for name in ('cast_msg_send','cast_msg_parse')) + extract.function(baseline,'cast_msg_parse_free'),
            'ACTUAL_CAST_STATUS': extract.function(baseline,'cast_status'),
            'ACTUAL_CAST_SHUTDOWN': shutdown,
            'ACTUAL_CAST_CALLBACKS': extract.function(cast,'cast_cb_stop') + extract.function(baseline,'cast_cb_volume'),
            'ACTUAL_CAST_PROCESS_TIMEOUT': extract.function(cast,'cast_msg_process') + extract.function(cast,'cast_reply_timeout_cb'),
            'ACTUAL_CAST_INTERFACE': extract.function(cast,'cast_device_flush') + extract.function(cast,'cast_device_volume_set'),
        },
    }
    before_raop = extract.patch_image(PATCH, 'src/outputs/raop.c', after=False)
    before_cast = extract.patch_image(PATCH, 'src/outputs/cast.c', after=False)
    parts['test_transport_raop_preimage'] = {'ACTUAL_RAOP_PREIMAGE': '\n'.join(
        extract.function(baseline, name) if name in {'raop_status', 'session_failure', 'raop_check_cseq', 'raop_cb_set_volume', 'raop_cb_flush'}
        else extract.function(before_raop, name)
        for name in ('raop_status', 'session_failure', 'raop_check_cseq', 'raop_cb_set_volume', 'raop_set_volume_one', 'raop_cb_flush', 'raop_device_flush'))}
    parts['test_transport_cast_preimage'] = {'ACTUAL_CAST_PREIMAGE': '\n'.join(
        extract.function(baseline, name) if name in {'cast_status', 'cast_cb_volume'} else extract.function(before_cast, name)
        for name in ('cast_msg_send', 'cast_msg_process', 'cast_status', 'cast_cb_volume', 'cast_reply_timeout_cb',
                     'cast_device_flush', 'cast_device_volume_set', 'cast_cb_stop'))}
    flags=['-std=gnu11','-D_POSIX_C_SOURCE=200809L','-Wall','-Wextra','-Werror','-Wno-sign-compare',
           '-Wno-unused-parameter','-Wno-unused-function','-Wno-unused-variable','-O1','-g']
    if sanitize:
        flags += ['-fsanitize=address,undefined','-fno-sanitize-recover=all']
    pkg_config=shutil.which('pkg-config')
    if not pkg_config:
        raise RuntimeError('Actual Cast JSON tests require pkg-config and json-c development files')
    import shlex
    found=subprocess.run([pkg_config,'--cflags','--libs','json-c'],capture_output=True,text=True,timeout=10)
    if found.returncode:
        raise RuntimeError('Actual Cast JSON tests require json-c development files')
    json_flags=shlex.split(found.stdout)
    env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0:halt_on_error=1',UBSAN_OPTIONS='halt_on_error=1')
    results=[]
    with tempfile.TemporaryDirectory(prefix='shiri-transport-native-') as temp:
        directory=Path(temp)
        (directory/'cast_json.h').write_text(native('src/outputs/cast_json.h'))
        for name,replacements in parts.items():
            fixture=(ROOT/'tests/native'/f'{name}.c').read_text()
            for marker,body in replacements.items():
                fixture=fixture.replace(f'/* @{marker}@ */',body)
            target=directory/name
            unit=target.with_suffix('.c')
            unit.write_text(fixture)
            compiled=subprocess.run([compiler,*flags,str(unit),*(json_flags if name == 'test_transport_cast' else []),'-o',str(target)],
                                    capture_output=True,text=True,timeout=30)
            if compiled.returncode:
                raise RuntimeError(compiled.stderr)
            completed=subprocess.run([str(target)],env=env,capture_output=True,text=True,timeout=30)
            if completed.returncode:
                raise RuntimeError(completed.stdout+completed.stderr)
            results.append(completed.stdout.strip())
    return results


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path)
    parser.add_argument('--compiler')
    parser.add_argument('--no-sanitize',action='store_true')
    args=parser.parse_args()
    print(json.dumps(run_tests(source=args.source,compiler=args.compiler,sanitize=not args.no_sanitize),indent=2))
