#!/usr/bin/env bash
# Build exactly reviewed backend revisions. No host PTP/OwnTone service is added.
set -euo pipefail
umask 022
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset PYTHONPATH PYTHONHOME LD_LIBRARY_PATH LD_PRELOAD
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${SHIRI_INSTALL_PREFIX:-/opt/shiri}"
[[ "$PREFIX" == /* && "$PREFIX" != / ]] || { echo "Use an absolute installation prefix" >&2; exit 2; }
[[ $(id -u) == 0 && $(uname -s) == Linux ]] || { echo "Run as root on Ubuntu/Debian" >&2; exit 1; }
/usr/bin/python3 -I "$SOURCE/install/validate_installation.py" --create "$PREFIX"
export PATH="$PREFIX/sbin:$PREFIX/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:$PREFIX/lib/aarch64-linux-gnu/pkgconfig"
export LD_LIBRARY_PATH="$PREFIX/lib"
NQPTP=c925f27c1fd12e4033ac477e5a405969b0b0260b
SHAIRPORT=7bad231c18368dbd26f298577f6210e36e4b0797
AIRPTP=7e2252e0258525b3480b54b1906038fee230e981
OWNTONE=d6fb3edf5831de38134ebd92fcf09a730ddd37aa
OWNTONE_PATCH="$SOURCE/install/patches/owntone-29.3-software-volume.patch"
OWNTONE_PATCH_SHA=f9250ebec36873ea39fff78ca3bbc5c424b023ead267868331f985ad0da1fe15
[[ "$(/usr/bin/sha256sum "$OWNTONE_PATCH" | awk '{print $1}')" == "$OWNTONE_PATCH_SHA" ]] || {
  echo "OwnTone patch digest does not match the reviewed patch" >&2; exit 1;
}
BUILD="$(mktemp -d /tmp/shiri-backends.XXXXXX)"
trap 'rm -rf "$BUILD"' EXIT
apt-get update
apt-get install -y build-essential git autoconf automake autotools-dev libtool pkg-config \
  gettext gawk gperf bison flex libpopt-dev libconfig-dev libssl-dev libavahi-client-dev \
  libsoxr-dev libasound2-dev libavcodec-dev libavformat-dev libavfilter-dev libswscale-dev \
  libswresample-dev libavutil-dev libgcrypt20-dev libsodium-dev libplist-dev libplist-utils xxd libconfuse-dev \
  libunistring-dev libsqlite3-dev libxml2-dev zlib1g-dev libevent-dev libjson-c-dev \
  libwebsockets-dev libcurl4-openssl-dev libprotobuf-c-dev libgnutls28-dev
checkout() {
  local name="$1" repository="$2" commit="$3"
  mkdir "$BUILD/$name"
  git -C "$BUILD/$name" init -q
  git -C "$BUILD/$name" remote add origin "$repository"
  git -C "$BUILD/$name" fetch --depth 1 origin "$commit"
  git -C "$BUILD/$name" checkout --detach FETCH_HEAD
  [[ "$(git -C "$BUILD/$name" rev-parse HEAD)" == "$commit" ]]
}
checkout nqptp https://github.com/mikebrady/nqptp.git "$NQPTP"
(cd "$BUILD/nqptp" && autoreconf -fi && ./configure --prefix="$PREFIX" && make -j"$(nproc)" && make install)
checkout shairport https://github.com/mikebrady/shairport-sync.git "$SHAIRPORT"
(cd "$BUILD/shairport" && autoreconf -fi && \
  ./configure --prefix="$PREFIX" --sysconfdir="$PREFIX/etc" --with-avahi --with-ssl=openssl \
    --with-airplay-2 --with-soxr --with-alsa --with-metadata --with-metadata-pipe && \
  make -j"$(nproc)" && make install)
checkout airptp https://github.com/owntone/libairptp.git "$AIRPTP"
(cd "$BUILD/airptp" && autoreconf -fi && ./configure --prefix="$PREFIX" --enable-daemon && \
  make -j"$(nproc)" && make install)
checkout owntone https://github.com/owntone/owntone-server.git "$OWNTONE"
git -C "$BUILD/owntone" apply --check "$OWNTONE_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_PATCH"
/usr/bin/python3 -I "$SOURCE/tests/native/check_owntone_patch.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
(cd "$BUILD/owntone" && autoreconf -fi && \
  ./configure --prefix="$PREFIX" --sysconfdir="$PREFIX/etc" --localstatedir="$PREFIX/var" \
    --disable-install-systemd --disable-webinterface --enable-chromecast && \
  make -j"$(nproc)" && make install)
mkdir -p "$PREFIX/share/shiri"
/usr/bin/python3 -I - "$PREFIX/share/shiri/backends.json" "$NQPTP" "$SHAIRPORT" "$AIRPTP" "$OWNTONE" "$OWNTONE_PATCH_SHA" <<'PY'
import json
import sys
with open(sys.argv[1], "w") as stream:
    manifest = dict(zip(("nqptp", "shairport", "libairptp", "owntone"), sys.argv[2:6]))
    manifest["owntone_software_volume_patch"] = sys.argv[6]
    manifest["owntone_software_volume_patch_file"] = "owntone-29.3-software-volume.patch"
    json.dump(manifest, stream, indent=2)
PY
/usr/bin/python3 -I "$SOURCE/install/validate_installation.py" "$PREFIX"
printf '%s\n' "Backend binaries installed in $PREFIX; host audio services were not restarted."
