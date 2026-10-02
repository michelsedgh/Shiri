"""Controlled arrival/tick replay of the exact maintained OwnTone C overlay."""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT/'install/patches/owntone-29.3-late-speech.patch'


@contextlib.contextmanager
def compiled_driver(*, patch=PATCH, sanitize=True):
    spec = importlib.util.spec_from_file_location('finite_existing_late_checker', ROOT/'tests/native/check_late_speech.py')
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    checker.validate_integration(patch.read_text(), None)
    compiler = shutil.which('clang') or shutil.which('cc')
    if compiler is None:
        raise RuntimeError('Actual OwnTone replay requires a C compiler')
    with tempfile.TemporaryDirectory(prefix='shiri-finite-c-') as temporary:
        root = Path(temporary)
        source = root/'shiri_speech.c'
        source.write_text(checker.added_file(patch.read_text(), 'src/shiri_speech.c'))
        (root/'shiri_speech.h').write_text(checker.added_file(patch.read_text(), 'src/shiri_speech.h'))
        binary = root/'replay'
        command = [compiler, '-std=gnu11', '-D_GNU_SOURCE', '-Wall', '-Wextra', '-Werror',
            f'-DSHIRI_SPEECH_SOURCE="{source}"', str(ROOT/'tests/native/finite_speech_replay.c'), '-lm', '-o', str(binary)]
        if sanitize:
            command[1:1] = ['-fsanitize=address,undefined', '-fno-sanitize-recover=all']
        subprocess.run(command, check=True, capture_output=True, timeout=60)
        yield binary, {'patch_sha256': hashlib.sha256(patch.read_bytes()).hexdigest(),
                       'module_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                       'compiler': compiler, 'sanitizers': sanitize, 'kernel_credentials_proven': False}


def replay(binary, events, *, room, launch):
    if len(room) != 16 or len(launch) != 16 or not 0 < len(events) <= 3000:
        raise ValueError('Bounded replay requires exact16byte identities and1..3000events')
    wire = bytearray(b'FTTSv1!!'+room+launch+struct.pack('!I', len(events)))
    previous, mixed_bytes = 0, 0
    for event in events:
        kind, now, payload = event
        if type(now) is not int or not 0 < now < 2**64 or now < previous:
            raise ValueError('Replay event clock went backwards or is invalid')
        previous = now
        wire.extend(struct.pack('!BQ', kind, now))
        if kind == 1:
            wire.extend(struct.pack('!I', len(payload))+payload)
        elif kind == 2:
            if not payload or len(payload) % 4:
                raise ValueError('Replay mix requires complete stereo frames')
            wire.extend(struct.pack('!II', len(payload)//4, len(payload))+payload)
            mixed_bytes += len(payload)
        else:
            raise ValueError('Undeclared replay event kind')
    result = subprocess.run([str(binary)], input=wire, check=True, capture_output=True, timeout=10)
    if len(result.stdout) != mixed_bytes:
        raise ValueError('Actual C replay omitted a requested final mix block')
    return result.stdout, json.loads(result.stderr.decode().splitlines()[-1])
