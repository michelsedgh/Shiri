#!/usr/bin/python3
"""Sanitize actual timed backend/marker functions without a VM or hardware."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

REPOSITORY = Path(__file__).resolve().parents[2]
OWN_PATCH = REPOSITORY / 'install/patches/owntone-29.3-timed-pcm.patch'
SHA_PATCH = REPOSITORY / 'install/patches/shairport-5.5.2-timed-pcm.patch'


def postimage(patch, name):
    block = patch.read_text().split(f'diff --git a/{name} b/{name}\n',1)[1].split('\ndiff --git ',1)[0]
    output,active=[],False
    for line in block.splitlines():
        if line.startswith('@@'):
            active=True
            output.append('')
        elif active and line.startswith(('+',' ')):
            output.append(line[1:])
    return '\n'.join(output)+'\n'


def function(text,name):
    match=re.search(rf'(?m)^{name}\([^\n]*\)\n\{{',text)
    if not match:
        raise ValueError(f'Missing actual patched function {name}')
    start=text.rfind('\n',0,match.start()-1)+1
    opening=text.index('{',match.start())
    depth,end=1,opening+1
    while depth:
        depth += (text[end]=='{')-(text[end]=='}')
        end+=1
    result = text[start:end]+'\n'
    if result.lstrip().startswith(name+'('):
        # Git --function-context places an unchanged return type in the @@
        # header; retain the exact pinned public/static declaration here.
        declaration = {'playback_cb':'static void', 'source_read':'static inline int',
                       'pb_timer_stop':'static int', 'input_read':'int',
                       'input_peek_sync':'int'}.get(name)
        if declaration is None:
            raise ValueError(f'Missing actual function declaration {name}')
        result = declaration+'\n'+result
    return result


def run_tests(*,own_source=None,shairport_source=None,compiler=None,sanitize=True):
    compiler=compiler or shutil.which('clang') or shutil.which('cc')
    if not compiler:
        raise RuntimeError('A C compiler is required for actual timing seam tests')
    def own(name):
        return (own_source/name).read_text() if own_source else postimage(OWN_PATCH,name)
    def sha(name):
        return (shairport_source/name).read_text() if shairport_source else postimage(SHA_PATCH,name)
    version = sha('common.c')
    recovery = None
    callback = sha('audio_shiri.c')
    if '"-shiri-timed3"' in version:
        if shairport_source is None or '"-shiri-timed2"' in version:
            raise ValueError('Current clock recovery requires an exact supplied timed3 source tree')
        path = REPOSITORY/'tests/native/check_shairport_clock_recovery.py'
        spec = importlib.util.spec_from_file_location('actual_timed3_clock_recovery', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        recovery = module.run_tests(shairport_source=shairport_source,compiler=compiler,sanitize=sanitize)
        # Retain the original callback/preimage proof and its four-result shape.
        # The current callback is exercised separately against the supplied tree.
        callback = postimage(SHA_PATCH,'audio_shiri.c')
    elif '"-shiri-timed2"' not in version:
        raise ValueError('Clock backend must advertise the exact timed2 or timed3 feature marker')
    flags=['-std=c99','-D_POSIX_C_SOURCE=200809L','-O1','-Wall','-Wextra','-Werror']
    if sys.platform == 'darwin':
        flags += ['-D_DARWIN_C_SOURCE=1']
    if sanitize:
        flags+=['-fsanitize=address,undefined','-fno-sanitize-recover=all']
    environment={**os.environ,'ASAN_OPTIONS':'detect_leaks=0:halt_on_error=1','UBSAN_OPTIONS':'halt_on_error=1'}
    results=[]
    with tempfile.TemporaryDirectory(prefix='shiri-timed-c-') as temporary:
        directory=Path(temporary)
        wire=own('src/inputs/shiri_pcm.h')
        if wire.strip()!=sha('shiri_pcm.h').strip():
            raise ValueError('Native ingress/output wire definitions diverged')
        (directory/'shiri_pcm.h').write_text(wire)
        pipe=own('src/inputs/pipe.c')
        names=['input_pipe_shiri_seal','input_pipe_shiri_arm','input_pipe_shiri_state','shiri_packet_owned','play_framed','shiri_ts_get']
        reader = function(own('src/input.c'),'input_read')
        # Git's function-context hunk leaves the return type in its @@ suffix.
        if reader.lstrip().startswith('input_read('):
            reader = 'int\n' + reader
        (directory/'timed_input.inc').write_text('\n'.join(function(pipe,name) for name in names)+'\n'+function(own('src/input.c'),'input_peek_sync')+'\n'+reader)
        (directory/'audio_shiri.inc').write_text(callback)
        (directory/'audio.h').write_text(sha('audio.h'))
        (directory/'libconfig.h').write_text('/* config services supplied by the test seam */\n')
        (directory/'common.h').write_text((REPOSITORY/'tests/native/shairport_timing_common.h').read_text())
        (directory/'shiri_timing.h').write_text(sha('shiri_timing.h'))
        preimage = REPOSITORY/'tests/native/shairport_clock_preimage.c'
        if hashlib.sha256(preimage.read_bytes()).hexdigest() != 'c5aeab5e8300079941bac171449f7a2db9092d6a32f8fd9264dbfd7a9828dad7':
            raise ValueError('Clock regression preimage no longer matches reviewed 48cbe backend source')
        # Compile the exact previous callback with the same scripted clock and
        # socket boundary. It must reproduce transmission of the observed
        # invalid bracket before checking the repaired callback below.
        (directory/'audio_shiri.inc').write_text(preimage.read_text())
        target = directory/'clock_preimage'
        subprocess.run([compiler,*flags,'-I',str(directory),
                        str(REPOSITORY/'tests/native/test_shairport_timing.c'),'-lm','-pthread',
                        '-o',str(target)],check=True,capture_output=True,text=True,timeout=30)
        preimage_result = subprocess.run([str(target),'clock-preimage'],env=environment,check=True,
                                        capture_output=True,text=True,timeout=30).stdout.strip()
        (directory/'audio_shiri.inc').write_text(callback)
        for name in ('test_timed_input','test_shairport_timing','test_native_timer'):
            target=directory/name
            unit = REPOSITORY/'tests/native'/f'{name}.c'
            extras = []
            if name == 'test_native_timer':
                player = own('src/player.c')
                bodies = '\n'.join(function(player,n) for n in ('pb_timer_native_prepare','pb_timer_start','pb_timer_stop','source_read','playback_cb'))
                unit = directory/f'{name}.c'
                unit.write_text((REPOSITORY/'tests/native'/f'{name}.c').read_text().replace('/* @ACTUAL_NATIVE_TIMER@ */',bodies))
                extras = ['-Wno-unused-parameter','-Wno-sign-compare']
                late_calls = [len(re.findall(r'\b'+name+r'\s*\(', bodies))
                              for name in ('shiri_speech_poll', 'shiri_speech_mix')]
                if late_calls != [0, 0]:
                    if late_calls != [1, 1]:
                        raise ValueError('Actual player must retain exactly one late-speech poll/mix seam')
                    extras.append('-DSHIRI_TIMER_SPEECH=1')
                ready_calls = len(re.findall(r'\bshiri_speech_mix_ready\s*\(', bodies))
                if ready_calls:
                    if ready_calls != 1 or late_calls != [1, 1]:
                        raise ValueError('Actual ready hook must occur once after the late-speech output seam')
                    extras.append('-DSHIRI_TIMER_READY=1')
            subprocess.run([compiler,*flags,*extras,'-I',str(directory),str(unit),'-lm','-pthread','-o',str(target)],check=True,capture_output=True,text=True,timeout=30)
            result=subprocess.run([str(target)],env=environment,check=True,capture_output=True,text=True,timeout=30)
            if name == 'test_shairport_timing':
                for argc, allowed in [(-1, True), (0, True), (-2, False), (1, False)]:
                    initialized = subprocess.run([str(target), str(argc)], env=environment,
                                                 capture_output=True, text=True, timeout=10)
                    if initialized.returncode != (0 if allowed else 71):
                        raise RuntimeError('Shairport backend startup argument contract failed: ' + initialized.stderr)
            results.append(result.stdout.strip())
            if name == 'test_native_timer':
                subprocess.run([compiler,*flags,*extras,'-DHAVE_TIMERFD=1','-I',str(directory),str(unit),'-lm','-pthread','-o',str(target)],check=True,capture_output=True,text=True,timeout=30)
                result=subprocess.run([str(target)],env=environment,check=True,capture_output=True,text=True,timeout=30)
                results.append('timerfd: '+result.stdout.strip())
    proof = {'ok':True,'sanitized':sanitize,'results':results,'clock_preimage':preimage_result}
    if recovery is not None:
        proof['clock_callback_evidence'] = 'Original timed2 callback boundary proof; current supplied timed3 callback checked separately'
        proof['clock_recovery'] = recovery
    return proof


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--own-source',type=Path)
    parser.add_argument('--shairport-source',type=Path)
    parser.add_argument('--compiler')
    parser.add_argument('--no-sanitizers',action='store_true')
    args=parser.parse_args()
    try:
        print(json.dumps(run_tests(own_source=args.own_source,shairport_source=args.shairport_source,compiler=args.compiler,sanitize=not args.no_sanitizers),indent=2))
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f'Native timed seam check failed: {exc.stderr or exc.stdout}') from None


if __name__=='__main__':
    main()
