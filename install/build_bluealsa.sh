#!/usr/bin/env bash
# Stage the reviewed synchronous SBC capability. No host daemon/service is changed.
set -euo pipefail
umask 022
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset PYTHONPATH PYTHONHOME LD_LIBRARY_PATH LD_PRELOAD
unset PKG_CONFIG_PATH PKG_CONFIG_LIBDIR PKG_CONFIG_SYSROOT_DIR CC CFLAGS CPPFLAGS LDFLAGS LIBS CONFIG_SITE
unset MAKEFLAGS MFLAGS MAKEOVERRIDES
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${SHIRI_INSTALL_PREFIX:-/opt/shiri-bluealsa}"
[[ "$PREFIX" =~ ^/[A-Za-z0-9_./-]+$ && "$PREFIX" != / ]] || { echo "Use a canonical absolute prefix without spaces or shell expansion characters" >&2; exit 2; }
[[ $(id -u) == 0 && $(uname -s) == Linux ]] || { echo "Run as root on Ubuntu/Debian" >&2; exit 1; }
/usr/bin/python3 -I "$SOURCE/install/validate_installation.py" --create "$PREFIX"
REVISION=1a84465dd860d1be9dcf62339c6273e9e0632dd2
PATCH="$SOURCE/install/patches/bluealsa-5.0.0-drop-sync.patch"
PATCH_SHA=964c756acfc82d8ab6877e9be3741fdf754b835fff1b7024b7347c1be46705a2
[[ "$(/usr/bin/sha256sum "$PATCH" | awk '{print $1}')" == "$PATCH_SHA" ]] || {
  echo "BlueALSA patch differs from the reviewed revision" >&2; exit 1;
}
# An isolated review build must not upgrade already-installed host audio libraries.
# Configure still checks the installed dependency versions before compilation.
DEPENDENCIES=(build-essential git autoconf automake libtool pkg-config
  libasound2-dev libbluetooth-dev libdbus-1-dev libglib2.0-dev libsbc-dev)
MISSING=()
for package in "${DEPENDENCIES[@]}"; do
  if [[ "$(/usr/bin/dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)" != 'install ok installed' ]]; then
    MISSING+=("$package")
  fi
done
if ((${#MISSING[@]})); then
  /usr/bin/python3 -I "$SOURCE/install/apt_dependencies.py" "${MISSING[@]}"
fi
BUILD="$(mktemp -d /tmp/shiri-bluealsa.XXXXXX)"
trap 'rm -rf "$BUILD"' EXIT
git -C "$BUILD" init -q
git -C "$BUILD" remote add origin https://github.com/arkq/bluez-alsa.git
git -C "$BUILD" fetch --depth 1 origin "$REVISION"
git -C "$BUILD" checkout --detach FETCH_HEAD
[[ "$(git -C "$BUILD" rev-parse HEAD)" == "$REVISION" ]]
git -C "$BUILD" apply --check "$PATCH"
git -C "$BUILD" apply "$PATCH"
/usr/bin/python3 -I "$SOURCE/tests/native/check_bluealsa_drop_sync.py" --source "$BUILD" --compiler /usr/bin/cc --require-sbc
(cd "$BUILD" && autoreconf -fi && \
  ./configure --prefix="$PREFIX" --sysconfdir="$PREFIX/etc" --localstatedir="$PREFIX/var" \
    --with-dbusconfdir="$PREFIX/etc/dbus-1/system.d" --with-dbus-iface-xml="$PREFIX/share/dbus-1/interfaces" \
    --with-alsaplugindir="$PREFIX/lib/alsa-lib" --with-alsaconfdir="$PREFIX/etc/alsa/conf.d" --without-bash-completion \
    --enable-systemd --with-systemdsystemunitdir="$PREFIX/lib/systemd/system" \
    --disable-aplay --disable-ctl --disable-manpages && make -j"$(nproc)" && make install)
/usr/bin/python3 -I "$SOURCE/install/validate_installation.py" "$PREFIX"
[[ "$("$PREFIX/bin/bluealsad" --version)" == 5.0.0-shiri-dropsync1 ]] || { echo "BlueALSA capability marker mismatch" >&2; exit 1; }
grep -aq 'STATE_DIRECTORY' "$PREFIX/bin/bluealsad" || { echo "BlueALSA isolated state-directory support is missing" >&2; exit 1; }
install -d -m 0755 "$PREFIX/share/shiri"
/usr/bin/python3 -I - "$PREFIX" "$REVISION" "$PATCH_SHA" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

prefix = Path(sys.argv[1])
target = prefix / 'share/shiri/bluealsa.json'
try:
    existing = target.lstat()
except FileNotFoundError:
    pass
else:
    if not stat.S_ISREG(existing.st_mode) or existing.st_uid != 0 or existing.st_nlink != 1 or existing.st_mode & 0o022:
        raise SystemExit('Existing BlueALSA provenance is not a trusted regular file')
path = prefix / 'bin/bluealsad'
handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
try:
    info = os.fstat(handle)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o6022:
        raise SystemExit('Built BlueALSA binary is not a trusted root-owned executable')
    digest = hashlib.sha256()
    while data := os.read(handle, 1024 * 1024):
        digest.update(data)
finally:
    os.close(handle)
manifest = {'version': 1, 'upstream_commit': sys.argv[2],
            'patch': 'bluealsa-5.0.0-drop-sync.patch', 'patch_sha256': sys.argv[3],
            'binary_sha256': digest.hexdigest(), 'binary_size': info.st_size,
            'binary_version': '5.0.0-shiri-dropsync1',
            'synchronous_drop': {'a2dp-source/sbc': True, 'other_codecs': False},
            'restricted_controller': True, 'open_method': 'OpenRestricted',
            'systemd_state_directory': True, 'systemd_units': 'private-prefix-only'}
fd, temporary = tempfile.mkstemp(prefix='.bluealsa-', dir=target.parent)
try:
    os.fchmod(fd, 0o644)
    with os.fdopen(fd, 'w') as stream:
        json.dump(manifest, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
/usr/bin/python3 -I "$SOURCE/install/validate_installation.py" "$PREFIX"
printf '%s\n' "BlueALSA candidate staged in $PREFIX; service activation and physical acceptance remain separate."
