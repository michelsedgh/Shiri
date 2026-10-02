#!/usr/bin/python3
"""Actual composed OwnTone first-anchor seam; no devices/network or rebasing."""
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

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('music_actual_timing_extractor', ROOT/'tests/native/check_timing_patch.py')
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)


def run_tests(source, *, compiler=None):
    source = Path(source)
    compiler = compiler or shutil.which('clang') or shutil.which('cc')
    if compiler is None:
        raise RuntimeError('A trusted C compiler is required')
    player = (source/'src/player.c').read_text()
    bodies = '\n'.join(extract.function(player,name) for name in
                      ('pb_timer_native_prepare','pb_timer_start','pb_timer_stop','source_read','playback_cb'))
    calls = [len(re.findall(r'\b'+name+r'\s*\(',bodies)) for name in ('shiri_speech_poll','shiri_speech_mix')]
    if calls != [1,1]:
        raise RuntimeError('Actual composed callback must retain its disabled-overlay poll/mix seams')
    flags = ['-std=c99','-D_POSIX_C_SOURCE=200809L','-O1','-Wall','-Wextra','-Werror',
             '-Wno-unused-parameter','-Wno-sign-compare','-DSHIRI_TIMER_SPEECH=1',
             '-fsanitize=address,undefined','-fno-sanitize-recover=all']
    if sys.platform == 'darwin':
        flags.append('-D_DARWIN_C_SOURCE=1')
    results = []
    environment = {**os.environ,'ASAN_OPTIONS':'detect_leaks=0:halt_on_error=1','UBSAN_OPTIONS':'halt_on_error=1'}
    with tempfile.TemporaryDirectory(prefix='shiri-music-first-anchor-') as directory:
        temporary = Path(directory)
        scaffold = (ROOT/'tests/native/test_native_timer.c').read_text()
        if scaffold.count('/* @ACTUAL_NATIVE_TIMER@ */') != 1:
            raise RuntimeError('Actual timer extraction scaffold changed')
        (temporary/'test_native_timer.c').write_text(scaffold.replace('/* @ACTUAL_NATIVE_TIMER@ */',bodies))
        shutil.copy2(ROOT/'tests/native/test_music_startup.c',temporary/'test_music_startup.c')
        for timerfd in (False,True):
            executable = temporary/('timerfd' if timerfd else 'timer')
            compiled = subprocess.run([compiler,*flags,*(['-DHAVE_TIMERFD=1'] if timerfd else []),
                '-I',str(temporary),str(temporary/'test_music_startup.c'),'-lm','-pthread','-o',str(executable)],
                capture_output=True,text=True,timeout=30)
            if compiled.returncode:
                raise RuntimeError('Actual MUSIC timing fixture failed to compile: '+compiled.stderr)
            ran = subprocess.run([str(executable)],env=environment,capture_output=True,text=True,timeout=30)
            if ran.returncode:
                raise RuntimeError('Actual MUSIC timing fixture failed: '+ran.stdout+ran.stderr)
            results.append({'timerfd':timerfd,'stdout':ran.stdout.strip()})
    return {'passed':True,'sanitized':True,'controlled_boundaries':28,'source':str(source),
            'source_sha256':hashlib.sha256(player.encode()).hexdigest(),'results':results,
            'scope':'Controlled native timer/output-start-completion semantics; not measured startup or physical latency'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--compiler')
    args = parser.parse_args()
    print(json.dumps(run_tests(args.source,compiler=args.compiler),indent=2))
