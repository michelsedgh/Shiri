#!/usr/bin/env python3
"""Prove the actual native input capacity boundary without audio or networking."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT/'install/patches/owntone-29.3-native-input-buffer.patch'
TIMED = ROOT/'install/patches/owntone-29.3-timed-pcm.patch'
spec = importlib.util.spec_from_file_location('timed_extract', ROOT/'tests/native/check_timing_patch.py')
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)


def image(path, *, before=False):
    block = PATCH.read_text().split(f'diff --git a/{path} b/{path}\n', 1)[1].split('\ndiff --git ', 1)[0]
    result, active = [], False
    for line in block.splitlines():
        if line.startswith('@@'):
            active = True
            result.append('')
        elif active and line.startswith((' ', '-' if before else '+')):
            result.append(line[1:])
    return '\n'.join(result)+'\n'


def function(source, name):
    body = extract.function(source, name) if name not in {'input_write', 'wait_buffer_ready'} else None
    if body is not None:
        return body
    match = re.search(rf'(?m)^{name}\([^\n]*\)\n\{{', source)
    if match is None:
        raise ValueError(f'Missing actual input function {name}')
    opening, depth = source.index('{', match.start()), 1
    end = opening+1
    while depth:
        depth += (source[end] == '{')-(source[end] == '}')
        end += 1
    return ('int' if name == 'input_write' else 'static int')+'\n'+source[match.start():end]+'\n'


def run_tests(*, source=None, compiler=None, sanitize=True):
    compiler = compiler or shutil.which('clang') or shutil.which('cc')
    if not compiler:
        raise RuntimeError('A C compiler is required for native input capacity tests')
    fixture = (ROOT/'tests/native/test_native_input_buffer.c').read_text()
    pipe = (source/'src/inputs/pipe.c').read_text() if source else extract.postimage(TIMED, 'src/inputs/pipe.c')
    wire = (source/'src/inputs/shiri_pcm.h').read_text() if source else extract.postimage(TIMED, 'src/inputs/shiri_pcm.h')
    pipe_functions = '\n'.join(extract.function(pipe, name) for name in (
        'input_pipe_shiri_seal', 'input_pipe_shiri_arm', 'shiri_packet_owned', 'play_framed'))
    flags = ['-std=c99', '-D_POSIX_C_SOURCE=200809L', '-Wall', '-Wextra', '-Werror',
             '-Wno-unused-function', '-O1', '-g']
    if sanitize:
        flags += ['-fsanitize=address,undefined', '-fno-sanitize-recover=all']
    environment = dict(os.environ, ASAN_OPTIONS='detect_leaks=0:halt_on_error=1', UBSAN_OPTIONS='halt_on_error=1')
    results = []
    with tempfile.TemporaryDirectory(prefix='shiri-native-capacity-') as temporary:
        directory = Path(temporary)
        (directory/'shiri_pcm.h').write_text(wire)
        for before in [True, False]:
            text = image('src/input.c', before=True) if before else (
                (source/'src/input.c').read_text() if source else image('src/input.c'))
            definitions = '\n'.join(re.findall(r'(?m)^#define INPUT_BUFFER_[A-Z_]+ .*$', text))
            names = ['input_write', 'wait_buffer_ready', 'input_read']
            if not before:
                names.insert(0, 'input_buffer_threshold')
            functions = '\n'.join(function(text, name) for name in names)
            unit = directory/('before.c' if before else 'after.c')
            unit.write_text(fixture.replace('/* @ACTUAL_THRESHOLD_DEFINITIONS@ */', definitions)
                .replace('/* @ACTUAL_INPUT_FUNCTIONS@ */', functions)
                .replace('/* @ACTUAL_PIPE_FUNCTIONS@ */', pipe_functions))
            program = directory/('before' if before else 'after')
            compiled = subprocess.run([compiler, *flags, *(['-DTEST_PREIMAGE=1'] if before else []),
                '-I', str(directory), str(unit), '-pthread', '-o', str(program)],
                capture_output=True, text=True, timeout=30)
            if compiled.returncode:
                raise RuntimeError(compiled.stdout+compiled.stderr)
            result = subprocess.run([str(program)], env=environment, capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError(result.stdout+result.stderr)
            results.append(result.stdout.strip())
    return {'ok': True, 'sanitized': sanitize, 'actual_source': source is not None, 'results': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--compiler')
    parser.add_argument('--no-sanitize', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run_tests(source=args.source, compiler=args.compiler, sanitize=not args.no_sanitize), indent=2))
