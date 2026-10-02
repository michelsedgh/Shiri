#!/usr/bin/env python3
"""Exercise actual native ingress and ALSA offset scheduling with configured B.

No device, socket, namespace or backend process is opened. The ingress fixture
uses a private pipe; output session and timestamp statements are extracted from
the maintained patches, or from --source for an independently composed checkout.
"""
from __future__ import annotations

import argparse
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
spec = importlib.util.spec_from_file_location('common_buffer_timed_seam', Path(__file__).with_name('check_timing_patch.py'))
timing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(timing)


def configured_buffer(text):
    matches = re.findall(r'(?m)^\s*start_buffer_ms\s*=\s*([0-9]+)\s*$', text)
    if len(matches) != 1 or not 0 < int(matches[0]) <= 60000:
        raise ValueError('One bounded generated start_buffer_ms is required')
    return int(matches[0])


def extract(text, start, end):
    at = text.index(start)
    return text[at:text.index(end, at)]


def run_tests(buffer_ms, *, own_source=None, compiler=None, sanitize=True):
    if type(buffer_ms) is not int or not 0 < buffer_ms <= 60000:
        raise ValueError('Invalid test buffer')
    compiler = compiler or shutil.which('clang') or shutil.which('cc')
    if not compiler:
        raise RuntimeError('A C compiler is required for the actual buffer scheduling seam')
    def source(name, patch):
        return (own_source/name).read_text() if own_source else timing.postimage(REPOSITORY/'install/patches'/patch, name)
    timed = 'owntone-29.3-timed-pcm.patch'
    arithmetic = 'owntone-29.3-offset-arithmetic.patch'
    pipe = source('src/inputs/pipe.c', timed)
    reader = source('src/input.c', timed)
    alsa = source('src/outputs/alsa.c', 'owntone-29.3-software-volume.patch')
    alsa_stamp = source('src/outputs/alsa.c', arithmetic)
    output = source('src/outputs.c', arithmetic)
    bodies = '\n'.join(timing.function(pipe, name) for name in
                       ('input_pipe_shiri_seal', 'input_pipe_shiri_arm', 'input_pipe_shiri_state',
                        'shiri_packet_owned', 'play_framed', 'shiri_ts_get'))
    bodies += '\n'+timing.function(reader, 'input_peek_sync')+'\n'+timing.function(reader, 'input_read')
    # Compile unchanged production control/timestamp statements. Surrounding
    # ALSA opening and timespec plumbing are deliberately supplied by the seam.
    delay = extract(alsa, '  as->delay_ms = outputs_buffer_duration_ms_get();', '\n\n')
    stamp = extract(alsa_stamp, '  delay_ts.tv_sec = as->delay_ms / 1000;', '\n\n')
    template = (REPOSITORY/'tests/native/test_timed_input.c').read_text()
    getter = timing.function(output, 'outputs_buffer_duration_ms_get')
    template = template.replace('static uint64_t outputs_buffer_duration_ms_get(void) { return 2000; }',
                                'static uint64_t outputs_buffer_duration_ms;\n'+getter)
    template = template.replace('int main(void){', 'int baseline_input_regressions(void){')
    flags = ['-std=c99', '-D_POSIX_C_SOURCE=200809L', '-O1', '-Wall', '-Wextra', '-Werror']
    if sys.platform == 'darwin':
        flags += ['-D_DARWIN_C_SOURCE=1']
    if sanitize:
        flags += ['-fsanitize=address,undefined', '-fno-sanitize-recover=all']
    environment = {**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0:halt_on_error=1', 'UBSAN_OPTIONS': 'halt_on_error=1'}
    with tempfile.TemporaryDirectory(prefix='shiri-common-buffer-') as temporary:
        directory = Path(temporary)
        (directory/'shiri_pcm.h').write_text(source('src/inputs/shiri_pcm.h', timed))
        (directory/'timed_input.inc').write_text(bodies)
        (directory/'input_fixture.inc').write_text(template)
        (directory/'alsa_delay.inc').write_text(delay)
        (directory/'alsa_stamp.inc').write_text(stamp)
        target = directory/'common-buffer'
        try:
            subprocess.run([compiler, *flags, '-I', str(directory), str(Path(__file__).with_name('test_common_buffer.c')),
                            '-pthread', '-o', str(target)], check=True, capture_output=True, text=True, timeout=30)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(exc.stderr or exc.stdout or 'Common buffer C compilation failed') from exc
        result = subprocess.run([str(target), str(buffer_ms)], env=environment,
                                capture_output=True, text=True, timeout=30)
        if result.returncode not in (0, 1):
            raise RuntimeError(result.stderr or 'Common buffer C seam failed unexpectedly')
        report = json.loads(result.stdout)
        if result.returncode != int(not report['ok']):
            raise RuntimeError('Scheduling result does not match the native check exit status')
        return {**report, 'sanitized': sanitize, 'source': str(own_source) if own_source else 'maintained patch postimages'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--compiler')
    parser.add_argument('--no-sanitizers', action='store_true')
    args = parser.parse_args()
    try:
        report = run_tests(configured_buffer(args.config.read_text()), own_source=args.source,
                           compiler=args.compiler, sanitize=not args.no_sanitizers)
        print(json.dumps(report, indent=2))
        return int(not report['ok'])
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr or exc.stdout) from None


if __name__ == '__main__':
    raise SystemExit(main())
