#!/usr/bin/env python3
"""Check the actual sealed source-transition converter reset, without devices."""
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
PATCH = ROOT / 'install/patches/owntone-29.3-resampler-reset.patch'
SOURCE = ROOT / 'install/patches/owntone-29.3-source-transition.patch'
TIMED = ROOT / 'install/patches/owntone-29.3-timed-pcm.patch'
spec = importlib.util.spec_from_file_location('reset_extract', ROOT / 'tests/native/check_source_transition.py')
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)


def run_tests(*, source=None, compiler=None, sanitize=True):
    compiler = compiler or shutil.which('clang') or shutil.which('cc')
    if not compiler:
        raise RuntimeError('A C compiler is required for converter reset checks')
    player = (source / 'src/player.c').read_text() if source else extract.postimage(PATCH, 'src/player.c')
    outputs = (source / 'src/outputs.c').read_text() if source else extract.postimage(PATCH, 'src/outputs.c')
    parent = extract.postimage(SOURCE, 'src/player.c')
    param = extract.between(parent, 'struct shiri_source_param {', '\nstruct event_base *evbase_player;')
    fixture = (ROOT / 'tests/native/test_resampler_reset.c').read_text()
    flags = ['-std=c11', '-D_POSIX_C_SOURCE=200809L', '-Wall', '-Wextra', '-Werror', '-O1', '-g']
    if sanitize:
        flags += ['-fsanitize=address,undefined', '-fno-sanitize-recover=all']
    environment = dict(os.environ, ASAN_OPTIONS='detect_leaks=0:halt_on_error=1', UBSAN_OPTIONS='halt_on_error=1')
    results = []
    with tempfile.TemporaryDirectory(prefix='shiri-resample-reset-') as temporary:
        directory = Path(temporary)
        (directory / 'inputs').mkdir()
        (directory / 'inputs/shiri_pcm.h').write_text(extract.postimage(TIMED, 'src/inputs/shiri_pcm.h'))
        (directory / 'shiri_source.h').write_text(extract.postimage(SOURCE, 'src/shiri_source.h'))
        for before in [True, False]:
            text = extract.patch_image(PATCH, 'src/player.c', after=False) if before else player
            seal = extract.function(text, 'shiri_source_seal_flush')
            seal = 'static enum command_state\n' + seal[seal.index('shiri_source_seal_flush('):]
            unit = directory / 'actual.c'
            unit.write_text(fixture.replace('/* @ACTUAL_PARAM@ */', param)
                            .replace('/* @ACTUAL_RESET@ */', extract.function(outputs, 'outputs_resampling_reset'))
                            .replace('/* @ACTUAL_SEAL@ */', seal))
            target = directory / 'actual'
            built = subprocess.run([compiler, *flags, *(['-DTEST_PREIMAGE=1', '-Wno-unused-function'] if before else []),
                                    '-I', str(directory), str(unit), '-o', str(target)],
                                   capture_output=True, text=True, timeout=30)
            if built.returncode:
                raise RuntimeError(built.stdout + built.stderr)
            result = subprocess.run([str(target)], env=environment, capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            results.append(result.stdout.strip())
        conversion = (ROOT / 'tests/native/test_output_conversion.c').read_text()
        bodies = []
        for name, returns in [('encoding_reset', 'static int'), ('buffer_fill', 'static void'), ('buffer_drain', 'static void')]:
            body = extract.function(outputs, name)
            bodies.append(returns + '\n' + body[body.index(name + '('):])
        unit = directory / 'conversion.c'
        unit.write_text(conversion.replace('/* @ACTUAL_CONVERSION@ */', '\n'.join(bodies)))
        target = directory / 'conversion'
        built = subprocess.run([compiler, *flags, str(unit), '-o', str(target)], capture_output=True, text=True, timeout=30)
        if built.returncode:
            raise RuntimeError(built.stdout + built.stderr)
        result = subprocess.run([str(target)], env=environment, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        results.append(result.stdout.strip())
    return {'ok': True, 'sanitized': sanitize, 'actual_source': source is not None, 'results': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--compiler')
    parser.add_argument('--no-sanitize', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run_tests(source=args.source, compiler=args.compiler, sanitize=not args.no_sanitize), indent=2))
