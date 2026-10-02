#!/usr/bin/env python3
"""Actual encoder timestamp arithmetic using real FFmpeg, without media devices."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / 'install/patches/owntone-29.3-resampler-reset.patch'
spec = importlib.util.spec_from_file_location('timestamp_extract', ROOT / 'tests/native/check_source_transition.py')
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)


def run_tests(*, source=None, compiler=None, sanitize=True):
    compiler = compiler or shutil.which('clang') or shutil.which('cc')
    if not compiler:
        raise RuntimeError('A C compiler is required')
    libraries = subprocess.run(['pkg-config', '--cflags', '--libs', 'libavcodec', 'libavformat', 'libavfilter', 'libavutil'],
                               capture_output=True, text=True, timeout=10)
    if libraries.returncode:
        raise RuntimeError('Real FFmpeg development packages are required: ' + libraries.stderr)
    text = (source / 'src/transcode.c').read_text() if source else extract.postimage(PATCH, 'src/transcode.c')
    # The real stream type is small but outside the changed function context.
    # It is copied from the reviewed pinned header boundary in the fixture when
    # patch-only testing; actual-source runs extract the same complete type.
    stream = ('struct stream_ctx { AVStream *stream; AVCodecContext *codec; AVFilterContext *buffersink_ctx;'
              ' AVFilterContext *buffersrc_ctx; AVFilterGraph *filter_graph; int64_t prev_pts; int64_t offset_pts; };')
    if source:
        stream = extract.between(text, 'struct stream_ctx\n{', '\nstruct decode_ctx')
    fixture = (ROOT / 'tests/native/test_packet_timestamps.c').read_text()
    flags = ['-std=c11', '-Wall', '-Wextra', '-Werror', '-O1', '-g']
    if sanitize:
        flags += ['-fsanitize=address,undefined', '-fno-sanitize-recover=all']
    environment = dict(os.environ, ASAN_OPTIONS='detect_leaks=0:halt_on_error=1', UBSAN_OPTIONS='halt_on_error=1')
    result_text = []
    with tempfile.TemporaryDirectory(prefix='shiri-packet-timestamps-') as temporary:
        directory = Path(temporary)
        for before in [True, False]:
            image = extract.patch_image(PATCH, 'src/transcode.c', after=False) if before else text
            parts = [extract.function(image, 'packet_prepare')] if before else [extract.function(image, name) for name in
                      ['packet_timestamp_add', 'packet_timestamp_subtract', 'packet_prepare']]
            # Diff function context may omit the preceding return-type line.
            prepared = parts[-1]
            parts[-1] = ('static void\n' if before else 'static int\n') + prepared[prepared.index('packet_prepare('):]
            encoded = '' if before else extract.function(image, 'encode_write')
            if not before:
                encoded = 'static int\n' + encoded[encoded.index('encode_write('):]
            unit = directory / 'actual.c'
            unit.write_text(fixture.replace('/* @ACTUAL_STREAM@ */', stream)
                            .replace('/* @ACTUAL_PREPARE@ */', '\n'.join(parts))
                            .replace('/* @ACTUAL_ENCODE@ */', encoded))
            target = directory / 'actual'
            built = subprocess.run([compiler, *flags, *(['-DTEST_PREIMAGE=1'] if before else []), str(unit),
                                    '-o', str(target), *shlex.split(libraries.stdout)], capture_output=True, text=True, timeout=30)
            if built.returncode:
                raise RuntimeError(built.stdout + built.stderr)
            completed = subprocess.run([str(target)], env=environment, capture_output=True, text=True, timeout=30)
            if before:
                if not sanitize or not completed.returncode or 'signed integer overflow' not in completed.stderr:
                    raise RuntimeError('Actual missing-PTS preimage did not reproduce under UBSan: ' + completed.stderr)
                result_text.append('Actual preimage reproduced: 0−AV_NOPTS_VALUE signed overflow')
            elif completed.returncode:
                raise RuntimeError(completed.stdout + completed.stderr)
            else:
                result_text.append(completed.stdout.strip())
    return {'ok': True, 'sanitized': sanitize, 'actual_source': source is not None, 'real_ffmpeg': True, 'results': result_text}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--compiler')
    args = parser.parse_args()
    print(json.dumps(run_tests(source=args.source, compiler=args.compiler), indent=2))
