#!/usr/bin/env python3
"""Sanitize real ALSA queue/admission/drain/flush functions without devices."""
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

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT/'install/patches/owntone-29.3-alsa-partial-write.patch'
SOFTWARE = ROOT/'install/patches/owntone-29.3-software-volume.patch'
spec = importlib.util.spec_from_file_location('alsa_partial_patch_extract', Path(__file__).with_name('check_timing_patch.py'))
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)


def function(text, name):
    match = re.search(rf'(?m)^{name}\([^\n]*\)\n\{{', text)
    if match is None:
        raise ValueError(f'Missing actual ALSA seam {name}')
    start = text.rfind('\n', 0, match.start()-1)+1
    opening = text.index('{', match.start())
    depth, end = 1, opening+1
    while depth:
        depth += (text[end] == '{')-(text[end] == '}')
        end += 1
    body = text[start:end]+'\n'
    if body.lstrip().startswith(name+'('):
        declaration = 'static snd_pcm_sframes_t' if name == 'pcm_write' else 'static int'
        body = declaration+'\n'+body
    return body


def run_tests(*, source=None, preimage_source=None, compiler=None, sanitize=True):
    compiler = compiler or shutil.which('clang') or shutil.which('cc')
    if compiler is None:
        raise RuntimeError('A C compiler is required for actual ALSA tail tests')
    alsa = (source/'src/outputs/alsa.c').read_text() if source else extract.postimage(PATCH, 'src/outputs/alsa.c')
    if not re.search(r'\bbool startup_done;', alsa):
        raise ValueError('Actual per-playback startup-state declaration is missing')
    software = (source/'src/outputs/alsa.c').read_text() if source else extract.postimage(SOFTWARE, 'src/outputs/alsa.c')
    upstream = Path(__file__).with_name('alsa_partial_upstream.h').read_text()
    if source:
        # These functions are unchanged by this layer; nevertheless compile
        # actual composed bodies and reject a stale vendored lifecycle fixture.
        misc = (source/'src/misc.c').read_text()
        for name in ('ringbuffer_read', 'ringbuffer_write', 'ringbuffer_free'):
            if function(upstream, name).strip() != function(misc, name).strip():
                raise ValueError(f'Pinned queue fixture differs from actual {name}')
        for name in ('playback_session_free', 'playback_session_remove_all', 'alsa_status', 'alsa_device_flush'):
            if function(upstream, name).strip() != function(alsa, name).strip():
                raise ValueError(f'Pinned lifecycle fixture differs from actual {name}')
    header = (source/'src/outputs/pcm_volume.h').read_text() if source else extract.postimage(SOFTWARE, 'src/outputs/pcm_volume.h')
    template = Path(__file__).with_name('test_alsa_partial_write.c').read_text()
    flags = ['-std=gnu99', '-D_POSIX_C_SOURCE=200809L', '-O1', '-Wall', '-Wextra', '-Werror']
    if sys.platform == 'darwin':
        flags += ['-D_DARWIN_C_SOURCE=1']
    if sanitize:
        flags += ['-fsanitize=address,undefined', '-fno-sanitize-recover=all']
    environment = {**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0:halt_on_error=1', 'UBSAN_OPTIONS': 'halt_on_error=1'}
    results = []
    with tempfile.TemporaryDirectory(prefix='shiri-alsa-tail-') as temporary:
        directory = Path(temporary)
        (directory/'pcm_volume.h').write_text(header)
        def compile_and_run(body, names, *, mode=None):
            unit = template.replace('/* @ACTUAL_PCM_WRITE@ */', function(software, 'pcm_write'))
            # Keep strict diagnostics on the new queue layer. The unchanged
            # pinned misc helpers and prebuffer comparison have legacy signed
            # comparisons; isolate precisely those existing statements.
            legacy = '#pragma GCC diagnostic push\n#pragma GCC diagnostic ignored "-Wsign-compare"\n'
            unit = unit.replace('/* @ACTUAL_LIFECYCLE@ */', legacy+upstream+'\n#pragma GCC diagnostic pop\n')
            functions = []
            for name in names:
                actual = function(body, name)
                if name == 'playback_write':
                    line = '  prebuffering = (pb->pos + obuf->data[i].samples <= pb->buffer_nsamp);'
                    if mode is not None:
                        if actual.count(line) != 1:
                            raise ValueError('Original prebuffer comparison seam changed')
                        actual = actual.replace(line, legacy+line+'\n#pragma GCC diagnostic pop')
                    elif actual.count('prebuffering = !pb->startup_done') != 1:
                        raise ValueError('Persistent startup-state seam is missing')
                functions.append(actual)
            unit = unit.replace('/* @ACTUAL_QUEUE@ */', '\n'.join(functions))
            path = directory/'test.c'
            path.write_text(unit)
            target = directory/'test'
            subprocess.run([compiler, *flags, '-I', str(directory), str(path), '-o', str(target)],
                           check=True, capture_output=True, text=True, timeout=30)
            result = subprocess.run([str(target), *([mode] if mode else [])], env=environment,
                                    check=True, capture_output=True, text=True, timeout=30)
            return result.stdout.strip()
        names = ['buffer_transfer', 'buffer_append', 'buffer_write', 'playback_drain', 'playback_write']
        results.append(compile_and_run(alsa, names))
        preimage = None
        wrap_preimage = None
        if preimage_source:
            original = (preimage_source/'src/outputs/alsa.c').read_text()
            old_names = ['buffer_write', 'playback_drain', 'playback_write']
            preimage = json.loads(compile_and_run(original, old_names, mode='--preimage'))
            wrap_preimage = json.loads(compile_and_run(original, old_names, mode='--wrap-preimage'))
    return {'ok': True, 'sanitized': sanitize, 'results': results, 'preimage': preimage, 'wrap_preimage': wrap_preimage}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--preimage-source', type=Path)
    parser.add_argument('--compiler')
    parser.add_argument('--no-sanitizers', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(run_tests(source=args.source, preimage_source=args.preimage_source,
                                   compiler=args.compiler, sanitize=not args.no_sanitizers), indent=2))
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr or exc.stdout) from None


if __name__ == '__main__':
    main()
