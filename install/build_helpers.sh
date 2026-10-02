#!/usr/bin/env bash
# Install the root broker's map-free Linux bind policy helper, without services.
set -euo pipefail
umask 022
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset PYTHONPATH PYTHONHOME LD_LIBRARY_PATH LD_PRELOAD
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${SHIRI_INSTALL_PREFIX:-/opt/shiri}"
[[ $(id -u) == 0 && $(uname -s) == Linux ]] || { echo "Run as root on Linux" >&2; exit 1; }
/usr/bin/python3 -I "$SOURCE/install/validate_installation.py" --create "$PREFIX"
BIND_SHA=1b85d268c8b709fa929e1bde36fa1aeadedbd9e8775d71ca0749f4cf9a920a58
PCM_SHA=dbdd99457473b53787932b1cc263c34bcf10c593889b34d19e713746e8b971c6
[[ "$(/usr/bin/sha256sum "$SOURCE/native/shiri_socket_policy.c" | awk '{print $1}')" == "$BIND_SHA" && \
   "$(/usr/bin/sha256sum "$SOURCE/native/shiri_pcm_exec.c" | awk '{print $1}')" == "$PCM_SHA" ]] || {
  echo "Native helper sources differ from the reviewed, frozen revisions" >&2; exit 1;
}
BUILD="$(mktemp -d /tmp/shiri-helper.XXXXXX)"
trap 'rm -rf "$BUILD"' EXIT
/usr/bin/cc -std=c11 -O2 -Wall -Wextra -Werror "$SOURCE/native/shiri_socket_policy.c" -o "$BUILD/shiri-bind-policy"
/usr/bin/cc -std=c11 -O2 -Wall -Wextra -Werror "$SOURCE/native/shiri_pcm_exec.c" -o "$BUILD/shiri-pcm-exec"
install -d -m 0755 "$PREFIX/libexec"
install -m 0755 "$BUILD/shiri-bind-policy" "$PREFIX/libexec/shiri-bind-policy"
install -m 0755 "$BUILD/shiri-pcm-exec" "$PREFIX/libexec/shiri-pcm-exec"
install -d -m 0755 "$PREFIX/share/shiri"
/usr/bin/python3 -I - "$PREFIX" "$BIND_SHA" "$PCM_SHA" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

prefix = Path(sys.argv[1])
target = prefix / 'share/shiri/runtime-helpers.json'
try:
    info = target.lstat()
except FileNotFoundError:
    pass
else:
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022:
        raise SystemExit('Existing helper manifest is linked, writable or not a root-owned regular file')
manifest = {'version': 1, 'helpers': {}}
for name, source, digest in [('shiri-bind-policy', 'shiri_socket_policy.c', sys.argv[2]),
                             ('shiri-pcm-exec', 'shiri_pcm_exec.c', sys.argv[3])]:
    path = prefix / 'libexec' / name
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o6022:
            raise SystemExit('Installed helper is linked, set-ID, writable or not owned by root')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            binary_hash = hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else None
            if binary_hash is None:
                digestor = hashlib.sha256()
                while chunk := stream.read(131072):
                    digestor.update(chunk)
                binary_hash = digestor.hexdigest()
    finally:
        os.close(descriptor)
    manifest['helpers'][name] = {'source': source, 'source_sha256': digest, 'binary_sha256': binary_hash}
descriptor, temporary = tempfile.mkstemp(prefix='.runtime-helpers.', dir=target.parent)
try:
    with os.fdopen(descriptor, 'w') as stream:
        os.fchmod(stream.fileno(), 0o644)
        json.dump(manifest, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
/usr/bin/python3 -I "$SOURCE/install/validate_installation.py" "$PREFIX"
