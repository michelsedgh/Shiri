#!/usr/bin/env bash
# Build exactly reviewed backend revisions. No host PTP/OwnTone service is added.
set -euo pipefail
umask 022
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset PYTHONPATH PYTHONHOME LD_LIBRARY_PATH LD_PRELOAD
unset PKG_CONFIG_PATH PKG_CONFIG_LIBDIR PKG_CONFIG_SYSROOT_DIR CC CFLAGS CPPFLAGS LDFLAGS LIBS CONFIG_SITE
unset MAKEFLAGS MFLAGS MAKEOVERRIDES
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${SHIRI_INSTALL_PREFIX:-/opt/shiri}"
[[ "$PREFIX" =~ ^/[A-Za-z0-9_./-]+$ && "$PREFIX" != / ]] || { echo "Use a canonical absolute prefix without spaces or shell expansion characters" >&2; exit 2; }
[[ $(id -u) == 0 && $(uname -s) == Linux ]] || { echo "Run as root on Ubuntu/Debian" >&2; exit 1; }
/usr/bin/python3 -I "$SOURCE/install/validate_installation.py" --create "$PREFIX"
export PATH="$PREFIX/sbin:$PREFIX/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:$PREFIX/lib/aarch64-linux-gnu/pkgconfig"
export LD_LIBRARY_PATH="$PREFIX/lib"
NQPTP=c925f27c1fd12e4033ac477e5a405969b0b0260b
SHAIRPORT=7bad231c18368dbd26f298577f6210e36e4b0797
AIRPTP=7e2252e0258525b3480b54b1906038fee230e981
OWNTONE=d6fb3edf5831de38134ebd92fcf09a730ddd37aa
AVAHI=f060abee2807c943821d88839c013ce15db17b58
AVAHI_PATCH="$SOURCE/install/patches/avahi-0.8-private-user.patch"
AVAHI_PATCH_SHA=f61870cffc4578031e7541214107cd21974ed6187d1bc29e3e213391778ebaa3
[[ "$(/usr/bin/sha256sum "$AVAHI_PATCH" | awk '{print $1}')" == "$AVAHI_PATCH_SHA" ]] || {
  echo "Avahi patch digest does not match the reviewed patch" >&2; exit 1;
}
OWNTONE_PATCH="$SOURCE/install/patches/owntone-29.3-software-volume.patch"
OWNTONE_PATCH_SHA=f9250ebec36873ea39fff78ca3bbc5c424b023ead267868331f985ad0da1fe15
[[ "$(/usr/bin/sha256sum "$OWNTONE_PATCH" | awk '{print $1}')" == "$OWNTONE_PATCH_SHA" ]] || {
  echo "OwnTone patch digest does not match the reviewed patch" >&2; exit 1;
}
SHAIRPORT_TIMED_PATCH="$SOURCE/install/patches/shairport-5.5.2-timed-pcm.patch"
SHAIRPORT_TIMED_SHA=6f04b42c31b1e6612586349955b135356d7d844de1b1cf36701c3fd8eae74d36
SHAIRPORT_CLOCK_RECOVERY_PATCH="$SOURCE/install/patches/shairport-5.5.2-clock-recovery.patch"
SHAIRPORT_CLOCK_RECOVERY_SHA=9c242e9fccc77e8fb7251198f2b918c6df2ed3c15d8b2bb4242a5b1710d69dad
SHAIRPORT_STARTUP_PATCH="$SOURCE/install/patches/shairport-5.5.2-native-startup.patch"
SHAIRPORT_STARTUP_SHA=bab272cab5764fc3ebb0f8169b6a94b8318b78f63e09fd7e368058901b9a71d8
SHAIRPORT_VOLUME_PATCH="$SOURCE/install/patches/shairport-5.5.2-receiver-volume.patch"
SHAIRPORT_VOLUME_SHA=26003aa1b8df99c5de6eecc21074bf259158e64baefd41a35c704145d5957bbd
SHAIRPORT_EVENTS_PATCH="$SOURCE/install/patches/shairport-5.5.2-bounded-events.patch"
SHAIRPORT_EVENTS_SHA=a7ffecbe2fe0da846b12ec34b2303a6279d5ad4f7ba4c2312234db0c634d7ecd
SHAIRPORT_BUFFERED_TIMING_PATCH="$SOURCE/install/patches/shairport-5.5.2-buffered-timing.patch"
SHAIRPORT_BUFFERED_TIMING_SHA=0bbd200c49f57df9890c92521e664de42ab21515b623106d460a6520c42d763e
OWNTONE_TIMED_PATCH="$SOURCE/install/patches/owntone-29.3-timed-pcm.patch"
OWNTONE_TIMED_SHA=3b02c678b171e391385ffef207371702ace9c71bfc0f1ae27d60fa11a1acb329
OWNTONE_SOURCE_PATCH="$SOURCE/install/patches/owntone-29.3-source-transition.patch"
OWNTONE_SOURCE_SHA=c9120094fd46b2ae64d2ee1d2aa34c8c0770614248b444e12d82516d26b81f96
OWNTONE_IDENTITY_PATCH="$SOURCE/install/patches/owntone-29.3-pcm-identity.patch"
OWNTONE_IDENTITY_SHA=819b9cc5ea81909975c4dfcb554f9b8cf73a6193379780f7a714ec0e5949bc7f
OWNTONE_TRANSPORT_PATCH="$SOURCE/install/patches/owntone-29.3-transport-control.patch"
OWNTONE_TRANSPORT_SHA=5f5c8e5bc258f79437dda54a182f20e40ffd9c254263925ab01a390439e8a8ec
OWNTONE_OFFSET_PATCH="$SOURCE/install/patches/owntone-29.3-offset-arithmetic.patch"
OWNTONE_OFFSET_SHA=c5ca167859b70449a72d9b0e011af759aa36a5e56f360d7676873ed0f1364cda
OWNTONE_BUFFER_PATCH="$SOURCE/install/patches/owntone-29.3-native-input-buffer.patch"
OWNTONE_BUFFER_SHA=057a81be0b7c4db32f3b8922603b5ef5e31fb5666ad1539c0588e81079f4a51b
OWNTONE_RESAMPLE_PATCH="$SOURCE/install/patches/owntone-29.3-resampler-reset.patch"
OWNTONE_RESAMPLE_SHA=28aa404b9330acbdb6e1f836f20cc7aee2b78701a34b184fa09b1903fb8793e6
OWNTONE_FRAMED_PATCH="$SOURCE/install/patches/owntone-29.3-framed-output.patch"
OWNTONE_FRAMED_SHA=e6581eff5ab0f0eb00e508df3e3f617482d691163101cc9a05a650eed63d0734
OWNTONE_ALSA_PATCH="$SOURCE/install/patches/owntone-29.3-alsa-partial-write.patch"
OWNTONE_ALSA_SHA=07e8c811ac27220fc96993a5a887bddb758d7a21f5f0a2a5b8684a9e006f1e43
OWNTONE_SPEECH_PATCH="$SOURCE/install/patches/owntone-29.3-late-speech.patch"
OWNTONE_SPEECH_SHA=ee8af10e35ee40228837e20be49a4dfe0606e9fdc8fa772227cc050223928d2c
OWNTONE_READY_PATCH="$SOURCE/install/patches/owntone-29.3-cold-speech-ready.patch"
OWNTONE_READY_SHA=3aabf70608d609ea2258c0c6e3c63e3a308c4e049d1edd9498308eddf80cb760
OWNTONE_ANCHOR_PATCH="$SOURCE/install/patches/owntone-29.3-native-anchor.patch"
OWNTONE_ANCHOR_SHA=9c40be469e2715e08ff829e32ed641f3b1bfffef88b242f058ca7ec9a45db4af
OWNTONE_JITTER_PATCH="$SOURCE/install/patches/owntone-29.3-speech-jitter.patch"
OWNTONE_JITTER_SHA=a639d54a245164647161e1650e6e50355c9b84c3660fac90dbe0dc09d1d80d36
OWNTONE_OWNER_PATCH="$SOURCE/install/patches/owntone-29.3-speech-owner.patch"
OWNTONE_OWNER_SHA=83962429b340c77ff55df8bf8716a0bde3ee558d56a72f6a0335248988f2771a
OWNTONE_BALANCE_PATCH="$SOURCE/install/patches/owntone-29.3-speaker-balance.patch"
OWNTONE_BALANCE_SHA=e61e28af5bdaefaa49d355681469ebb8cf23247b8a18e5c3a4261186d2d963a0
OWNTONE_TRANSITION_PATCH="$SOURCE/install/patches/owntone-29.3-native-transition-timer.patch"
OWNTONE_TRANSITION_SHA=912922fb7853d25fb031d0258effeb33f3332a971b84f01e01c79014a194e8a7
OWNTONE_BED_PATCH="$SOURCE/install/patches/owntone-29.3-paused-speech.patch"
OWNTONE_BED_SHA=eb2f9ceb0e58f3c92d82c682cd177b98d3b0a48d4848aa5b430f3761703da928
OWNTONE_EVENT_PATCH="$SOURCE/install/patches/owntone-29.3-event-ack.patch"
OWNTONE_EVENT_SHA=08ead94d976619985445e8177756ee08be82d50a6b03432faadcdd281fbc3f73
OWNTONE_IDLE_PATCH="$SOURCE/install/patches/owntone-29.3-idle-speech.patch"
OWNTONE_IDLE_SHA=19161c472ceb6ab80d88d3e22202ea16c37a7829ed5d44db027cdcd3b73ac29c
OWNTONE_DRAIN_PATCH="$SOURCE/install/patches/owntone-29.3-speech-drain.patch"
OWNTONE_DRAIN_SHA=2e240eecaad804b805d886b961116e142f0dff94d6c437f134b96f80b8408563
OWNTONE_STARTUP_METADATA_PATCH="$SOURCE/install/patches/owntone-29.3-startup-metadata.patch"
OWNTONE_STARTUP_METADATA_SHA=11cd0aec9232b716d2ea3c28a9ac2d321ca24779937063178ad40982d45df525
OWNTONE_COLD_MUSIC_PATCH="$SOURCE/install/patches/owntone-29.3-cold-music.patch"
OWNTONE_COLD_MUSIC_SHA=58f3346db99fadfa448a1c02c91d1ec95656b04fc0100928ef4aa790cada6c80
OWNTONE_OUTPUT_CLOCK_PATCH="$SOURCE/install/patches/owntone-29.3-output-clock.patch"
OWNTONE_OUTPUT_CLOCK_SHA=842b21bb969af4cf839e0254679cd57266fc85d9ef8c2d77e1a9cfe44ec73d94
OWNTONE_DUCK_ENVELOPE_PATCH="$SOURCE/install/patches/owntone-29.3-duck-envelope.patch"
OWNTONE_DUCK_ENVELOPE_SHA=34790726bf9e14085bcde2519f6bb736c36e951f771f78d0282134cf3f669bab
OWNTONE_WARM_LEASE_PATCH="$SOURCE/install/patches/owntone-29.3-warm-lease.patch"
OWNTONE_WARM_LEASE_SHA=37cc3a7a6ab6c6fcb57dbc93c7e4e9208193aa6c219559a12930f2b07574959e
for patch in "$SHAIRPORT_TIMED_PATCH:$SHAIRPORT_TIMED_SHA" "$SHAIRPORT_CLOCK_RECOVERY_PATCH:$SHAIRPORT_CLOCK_RECOVERY_SHA" "$SHAIRPORT_STARTUP_PATCH:$SHAIRPORT_STARTUP_SHA" "$SHAIRPORT_VOLUME_PATCH:$SHAIRPORT_VOLUME_SHA" "$SHAIRPORT_EVENTS_PATCH:$SHAIRPORT_EVENTS_SHA" "$SHAIRPORT_BUFFERED_TIMING_PATCH:$SHAIRPORT_BUFFERED_TIMING_SHA" "$OWNTONE_TIMED_PATCH:$OWNTONE_TIMED_SHA" "$OWNTONE_SOURCE_PATCH:$OWNTONE_SOURCE_SHA" "$OWNTONE_IDENTITY_PATCH:$OWNTONE_IDENTITY_SHA" "$OWNTONE_TRANSPORT_PATCH:$OWNTONE_TRANSPORT_SHA" "$OWNTONE_OFFSET_PATCH:$OWNTONE_OFFSET_SHA" "$OWNTONE_BUFFER_PATCH:$OWNTONE_BUFFER_SHA" "$OWNTONE_RESAMPLE_PATCH:$OWNTONE_RESAMPLE_SHA" "$OWNTONE_FRAMED_PATCH:$OWNTONE_FRAMED_SHA" "$OWNTONE_ALSA_PATCH:$OWNTONE_ALSA_SHA" "$OWNTONE_SPEECH_PATCH:$OWNTONE_SPEECH_SHA" "$OWNTONE_READY_PATCH:$OWNTONE_READY_SHA" "$OWNTONE_ANCHOR_PATCH:$OWNTONE_ANCHOR_SHA" "$OWNTONE_JITTER_PATCH:$OWNTONE_JITTER_SHA" "$OWNTONE_OWNER_PATCH:$OWNTONE_OWNER_SHA" "$OWNTONE_BALANCE_PATCH:$OWNTONE_BALANCE_SHA" "$OWNTONE_TRANSITION_PATCH:$OWNTONE_TRANSITION_SHA" "$OWNTONE_BED_PATCH:$OWNTONE_BED_SHA" "$OWNTONE_EVENT_PATCH:$OWNTONE_EVENT_SHA" "$OWNTONE_IDLE_PATCH:$OWNTONE_IDLE_SHA" "$OWNTONE_DRAIN_PATCH:$OWNTONE_DRAIN_SHA" "$OWNTONE_STARTUP_METADATA_PATCH:$OWNTONE_STARTUP_METADATA_SHA" "$OWNTONE_COLD_MUSIC_PATCH:$OWNTONE_COLD_MUSIC_SHA" "$OWNTONE_OUTPUT_CLOCK_PATCH:$OWNTONE_OUTPUT_CLOCK_SHA" "$OWNTONE_DUCK_ENVELOPE_PATCH:$OWNTONE_DUCK_ENVELOPE_SHA" "$OWNTONE_WARM_LEASE_PATCH:$OWNTONE_WARM_LEASE_SHA"; do
  [[ "$(/usr/bin/sha256sum "${patch%:*}" | awk '{print $1}')" == "${patch##*:}" ]] || {
    echo "A timing/source patch digest does not match the reviewed patch" >&2; exit 1;
  }
done
BUILD="$(mktemp -d /tmp/shiri-backends.XXXXXX)"
trap 'rm -rf "$BUILD"' EXIT
DEPENDENCIES=(build-essential git autoconf automake autotools-dev libtool pkg-config
  gettext autopoint intltool xmltoman gawk gperf bison flex libpopt-dev libconfig-dev libssl-dev libavahi-client-dev
  libsoxr-dev libasound2-dev libavcodec-dev libavformat-dev libavfilter-dev libswscale-dev
  libswresample-dev libavutil-dev libgcrypt20-dev libsodium-dev libplist-dev libplist-utils uuid-dev xxd libconfuse-dev
  libunistring-dev libsqlite3-dev libxml2-dev zlib1g-dev libevent-dev libjson-c-dev
  libwebsockets-dev libcurl4-openssl-dev libprotobuf-c-dev libgnutls28-dev
  libdbus-1-dev libexpat1-dev libdaemon-dev)
MISSING=()
for package in "${DEPENDENCIES[@]}"; do
  if [[ "$(/usr/bin/dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)" != 'install ok installed' ]]; then
    MISSING+=("$package")
  fi
done
if ((${#MISSING[@]})); then
  /usr/bin/python3 -I "$SOURCE/install/apt_dependencies.py" "${MISSING[@]}"
fi
SHIRI_INSTALL_PREFIX="$PREFIX" bash "$SOURCE/install/build_helpers.sh"
checkout() {
  local name="$1" repository="$2" commit="$3"
  mkdir "$BUILD/$name"
  git -C "$BUILD/$name" init -q
  git -C "$BUILD/$name" remote add origin "$repository"
  git -C "$BUILD/$name" fetch --depth 1 origin "$commit"
  git -C "$BUILD/$name" checkout --detach FETCH_HEAD
  [[ "$(git -C "$BUILD/$name" rev-parse HEAD)" == "$commit" ]]
}
checkout avahi https://github.com/avahi/avahi.git "$AVAHI"
git -C "$BUILD/avahi" apply --check "$AVAHI_PATCH"
git -C "$BUILD/avahi" apply "$AVAHI_PATCH"
/usr/bin/python3 -I "$SOURCE/tests/native/check_avahi_patch.py" --source "$BUILD/avahi" --compiler /usr/bin/cc
(cd "$BUILD/avahi" && autoreconf -fi && \
  ./configure --prefix="$PREFIX" --sysconfdir="$PREFIX/etc" --localstatedir="$PREFIX/var" \
    --with-distro=none --with-systemdsystemunitdir=no --with-dbus-sys="$PREFIX/etc/dbus-1/system.d" \
    --disable-glib --disable-gobject --disable-gtk --disable-gtk3 --disable-qt4 --disable-qt5 \
    --disable-python --disable-mono --disable-autoipd --enable-manpages --enable-xmltoman \
    --disable-libevent --disable-gdbm && make -j"$(nproc)" && make install)
checkout nqptp https://github.com/mikebrady/nqptp.git "$NQPTP"
(cd "$BUILD/nqptp" && autoreconf -fi && ./configure --prefix="$PREFIX" && make -j"$(nproc)" && make install)
checkout shairport https://github.com/mikebrady/shairport-sync.git "$SHAIRPORT"
git -C "$BUILD/shairport" apply --check "$SHAIRPORT_TIMED_PATCH"
git -C "$BUILD/shairport" apply "$SHAIRPORT_TIMED_PATCH"
git -C "$BUILD/shairport" apply --check "$SHAIRPORT_CLOCK_RECOVERY_PATCH"
git -C "$BUILD/shairport" apply "$SHAIRPORT_CLOCK_RECOVERY_PATCH"
git -C "$BUILD/shairport" apply --check "$SHAIRPORT_STARTUP_PATCH"
git -C "$BUILD/shairport" apply "$SHAIRPORT_STARTUP_PATCH"
git -C "$BUILD/shairport" apply --check "$SHAIRPORT_VOLUME_PATCH"
git -C "$BUILD/shairport" apply "$SHAIRPORT_VOLUME_PATCH"
# Strictly inverse only the reviewed expired-prefix publisher, retaining the
# immutable layer28 source guard on its exact original source bytes.
/usr/bin/python3 -I "$SOURCE/tests/native/check_shairport_pcm_deadline.py" --source "$BUILD/shairport" --stage volume
git -C "$BUILD/shairport" apply --check "$SHAIRPORT_EVENTS_PATCH"
git -C "$BUILD/shairport" apply "$SHAIRPORT_EVENTS_PATCH"
git -C "$BUILD/shairport" apply --check "$SHAIRPORT_BUFFERED_TIMING_PATCH"
git -C "$BUILD/shairport" apply "$SHAIRPORT_BUFFERED_TIMING_PATCH"
/usr/bin/python3 -I "$SOURCE/tests/native/check_shairport_buffered_timing.py" --source "$BUILD/shairport" --compiler /usr/bin/cc
/usr/bin/python3 -I "$SOURCE/tests/native/check_shairport_pcm_deadline.py" --source "$BUILD/shairport" --stage events --compiler /usr/bin/cc --sanitize
/usr/bin/python3 -I "$SOURCE/tests/native/check_shairport_startup.py" --source "$BUILD/shairport" --compiler /usr/bin/cc
(cd "$BUILD/shairport" && autoreconf -fi && \
  ./configure --prefix="$PREFIX" --sysconfdir="$PREFIX/etc" --with-avahi --with-ssl=openssl \
    --with-airplay-2 --with-soxr --with-alsa --with-shiri --with-metadata --with-metadata-pipe && \
  make -j"$(nproc)" && make install)
checkout airptp https://github.com/owntone/libairptp.git "$AIRPTP"
(cd "$BUILD/airptp" && autoreconf -fi && ./configure --prefix="$PREFIX" --enable-daemon && \
  make -j"$(nproc)" && make install)
checkout owntone https://github.com/owntone/owntone-server.git "$OWNTONE"
git -C "$BUILD/owntone" apply --check "$OWNTONE_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_PATCH"
git -C "$BUILD/owntone" apply --check "$OWNTONE_TIMED_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_TIMED_PATCH"
git -C "$BUILD/owntone" apply --check "$OWNTONE_SOURCE_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_SOURCE_PATCH"
git -C "$BUILD/owntone" apply --check "$OWNTONE_IDENTITY_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_IDENTITY_PATCH"
git -C "$BUILD/owntone" apply --check "$OWNTONE_TRANSPORT_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_TRANSPORT_PATCH"
git -C "$BUILD/owntone" apply --check "$OWNTONE_OFFSET_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_OFFSET_PATCH"
git -C "$BUILD/owntone" apply --check "$OWNTONE_BUFFER_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_BUFFER_PATCH"
git -C "$BUILD/owntone" apply --check "$OWNTONE_RESAMPLE_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_RESAMPLE_PATCH"
git -C "$BUILD/owntone" apply --check "$OWNTONE_FRAMED_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_FRAMED_PATCH"
git -C "$BUILD/owntone" apply --check "$OWNTONE_ALSA_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_ALSA_PATCH"
git -C "$BUILD/owntone" apply --check "$OWNTONE_SPEECH_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_SPEECH_PATCH"
git -C "$BUILD/owntone" apply --check "$OWNTONE_READY_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_READY_PATCH"
git -C "$BUILD/owntone" apply --check "$OWNTONE_ANCHOR_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_ANCHOR_PATCH"
git -C "$BUILD/owntone" apply --check "$OWNTONE_JITTER_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_JITTER_PATCH"
/usr/bin/python3 -I "$SOURCE/tests/native/check_speech_jitter.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
/usr/bin/python3 -I "$SOURCE/tests/native/check_music_minimum.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
/usr/bin/python3 -I "$SOURCE/tests/native/check_native_anchor.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
/usr/bin/python3 -I "$SOURCE/tests/native/check_cold_speech_ready.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
/usr/bin/python3 -I "$SOURCE/tests/native/check_late_speech.py" --source "$BUILD/owntone" --compiler /usr/bin/cc --require-credentials
/usr/bin/python3 -I -B "$SOURCE/tests/native/check_timing_patch.py" --own-source "$BUILD/owntone" --shairport-source "$BUILD/shairport"
/usr/bin/python3 -I "$SOURCE/tests/native/check_source_transition.py" --source "$BUILD/owntone" --require-json
/usr/bin/python3 -I "$SOURCE/tests/native/check_owntone_patch.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
/usr/bin/python3 -I "$SOURCE/tests/native/check_alsa_partial_write.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
/usr/bin/python3 -I "$SOURCE/tests/native/check_pcm_identity.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
/usr/bin/python3 -I "$SOURCE/tests/native/check_transport_control.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
/usr/bin/python3 -I "$SOURCE/tests/native/check_offset_arithmetic.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
/usr/bin/python3 -I "$SOURCE/tests/native/check_native_input_buffer.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
/usr/bin/python3 -I "$SOURCE/tests/native/check_resampler_reset.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
/usr/bin/python3 -I "$SOURCE/tests/native/check_packet_timestamps.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
/usr/bin/python3 -I "$SOURCE/tests/native/check_framed_output.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
git -C "$BUILD/owntone" apply --check "$OWNTONE_OWNER_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_OWNER_PATCH"
git -C "$BUILD/owntone" apply --check "$OWNTONE_BALANCE_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_BALANCE_PATCH"
/usr/bin/python3 -I "$SOURCE/tests/native/check_volume_settings.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
/usr/bin/python3 -I "$SOURCE/tests/native/check_speech_owner.py" --source "$BUILD/owntone" --compiler /usr/bin/cc --require-credentials
git -C "$BUILD/owntone" apply --check "$OWNTONE_TRANSITION_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_TRANSITION_PATCH"
/usr/bin/python3 -I "$SOURCE/tests/native/check_native_transition_timer.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
git -C "$BUILD/owntone" apply --check "$OWNTONE_BED_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_BED_PATCH"
/usr/bin/python3 -I "$SOURCE/tests/native/check_paused_speech_guards.py" --source "$BUILD/owntone"
/usr/bin/python3 -I "$SOURCE/tests/native/check_paused_speech.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
git -C "$BUILD/owntone" apply --check "$OWNTONE_EVENT_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_EVENT_PATCH"
/usr/bin/python3 -I "$SOURCE/tests/native/check_airplay_events.py" --source "$BUILD/owntone" --compiler /usr/bin/cc --sanitize
cp -a "$BUILD/owntone" "$BUILD/owntone-before-idle"
git -C "$BUILD/owntone" apply --check "$OWNTONE_IDLE_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_IDLE_PATCH"
/usr/bin/python3 -I "$SOURCE/tests/native/check_idle_speech.py" --source "$BUILD/owntone" --preimage "$BUILD/owntone-before-idle" --compiler /usr/bin/cc
cp -a "$BUILD/owntone" "$BUILD/owntone-before-drain"
git -C "$BUILD/owntone" apply --check "$OWNTONE_DRAIN_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_DRAIN_PATCH"
/usr/bin/python3 -I "$SOURCE/tests/native/check_speech_drain.py" --source "$BUILD/owntone" --preimage "$BUILD/owntone-before-drain" --compiler /usr/bin/cc --capture "$BUILD/speech-bed-capture.bin"
cp -a "$BUILD/owntone" "$BUILD/owntone-before-startup-metadata"
git -C "$BUILD/owntone" apply --check "$OWNTONE_STARTUP_METADATA_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_STARTUP_METADATA_PATCH"
/usr/bin/python3 -I "$SOURCE/tests/native/check_startup_metadata.py" --source "$BUILD/owntone" --preimage "$BUILD/owntone-before-startup-metadata" --compiler /usr/bin/cc
cp -a "$BUILD/owntone" "$BUILD/owntone-before-cold-music"
git -C "$BUILD/owntone" apply --check "$OWNTONE_COLD_MUSIC_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_COLD_MUSIC_PATCH"
/usr/bin/python3 -I "$SOURCE/tests/native/check_cold_music.py" --source "$BUILD/owntone" --preimage "$BUILD/owntone-before-cold-music" --compiler /usr/bin/cc
git -C "$BUILD/owntone" apply --check "$OWNTONE_OUTPUT_CLOCK_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_OUTPUT_CLOCK_PATCH"
/usr/bin/python3 -I "$SOURCE/tests/native/check_output_clock.py" --source "$BUILD/owntone" --compiler /usr/bin/cc --real-confuse
git -C "$BUILD/owntone" apply --check "$OWNTONE_DUCK_ENVELOPE_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_DUCK_ENVELOPE_PATCH"
/usr/bin/python3 -I "$SOURCE/tests/native/check_duck_envelope.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
git -C "$BUILD/owntone" apply --check "$OWNTONE_WARM_LEASE_PATCH"
git -C "$BUILD/owntone" apply "$OWNTONE_WARM_LEASE_PATCH"
/usr/bin/python3 -I "$SOURCE/tests/native/check_warm_lease.py" --source "$BUILD/owntone" --compiler /usr/bin/cc
(cd "$BUILD/owntone" && autoreconf -fi && \
  ./configure --prefix="$PREFIX" --sysconfdir="$PREFIX/etc" --localstatedir="$PREFIX/var" \
    --disable-install-systemd --disable-webinterface --enable-chromecast && \
  make -j"$(nproc)")
/usr/bin/python3 -I "$SOURCE/tests/native/check_framed_resampler.py" --source "$BUILD/owntone" --compiler /usr/bin/cc --native-transition --paused-speech
/usr/bin/python3 -I "$SOURCE/tests/native/check_speech_packetizer.py" --source "$BUILD/owntone" --capture "$BUILD/speech-bed-capture.bin" --compiler /usr/bin/cc
(cd "$BUILD/owntone" && make install)
mkdir -p "$PREFIX/share/shiri"
/usr/bin/python3 -I - "$PREFIX/share/shiri/backends.json" "$NQPTP" "$SHAIRPORT" "$AIRPTP" "$OWNTONE" "$OWNTONE_PATCH_SHA" "$AVAHI" "$AVAHI_PATCH_SHA" "$SHAIRPORT_TIMED_SHA" "$OWNTONE_TIMED_SHA" "$OWNTONE_SOURCE_SHA" "$OWNTONE_IDENTITY_SHA" "$OWNTONE_TRANSPORT_SHA" "$OWNTONE_OFFSET_SHA" "$OWNTONE_BUFFER_SHA" "$OWNTONE_RESAMPLE_SHA" "$OWNTONE_FRAMED_SHA" "$OWNTONE_ALSA_SHA" "$OWNTONE_SPEECH_SHA" "$OWNTONE_READY_SHA" "$OWNTONE_ANCHOR_SHA" "$OWNTONE_JITTER_SHA" "$OWNTONE_OWNER_SHA" "$SHAIRPORT_CLOCK_RECOVERY_SHA" "$OWNTONE_BALANCE_SHA" "$SHAIRPORT_STARTUP_SHA" "$OWNTONE_TRANSITION_SHA" "$SHAIRPORT_VOLUME_SHA" "$OWNTONE_BED_SHA" "$SHAIRPORT_EVENTS_SHA" "$OWNTONE_EVENT_SHA" "$OWNTONE_IDLE_SHA" "$OWNTONE_DRAIN_SHA" "$OWNTONE_STARTUP_METADATA_SHA" "$OWNTONE_COLD_MUSIC_SHA" "$OWNTONE_OUTPUT_CLOCK_SHA" "$OWNTONE_DUCK_ENVELOPE_SHA" "$OWNTONE_WARM_LEASE_SHA" "$SHAIRPORT_BUFFERED_TIMING_SHA" <<'PY'
import json
import sys
with open(sys.argv[1], "w") as stream:
    manifest = dict(zip(("nqptp", "shairport", "libairptp", "owntone"), sys.argv[2:6]))
    manifest["owntone_software_volume_patch"] = sys.argv[6]
    manifest["owntone_software_volume_patch_file"] = "owntone-29.3-software-volume.patch"
    manifest["avahi"] = sys.argv[7]
    manifest["avahi_private_user_patch"] = sys.argv[8]
    manifest["avahi_private_user_patch_file"] = "avahi-0.8-private-user.patch"
    manifest["shairport_timed_pcm_patch"] = sys.argv[9]
    manifest["owntone_timed_pcm_patch"] = sys.argv[10]
    manifest["owntone_source_transition_patch"] = sys.argv[11]
    manifest["owntone_pcm_identity_patch"] = sys.argv[12]
    manifest["owntone_transport_control_patch"] = sys.argv[13]
    manifest["owntone_offset_arithmetic_patch"] = sys.argv[14]
    manifest["owntone_native_input_buffer_patch"] = sys.argv[15]
    manifest["owntone_resampler_reset_patch"] = sys.argv[16]
    manifest["owntone_framed_output_patch"] = sys.argv[17]
    manifest["owntone_alsa_partial_write_patch"] = sys.argv[18]
    manifest["owntone_late_speech_patch"] = sys.argv[19]
    manifest["owntone_late_speech_patch_file"] = "owntone-29.3-late-speech.patch"
    manifest["owntone_cold_speech_ready_patch"] = sys.argv[20]
    manifest["owntone_cold_speech_ready_patch_file"] = "owntone-29.3-cold-speech-ready.patch"
    manifest["owntone_native_anchor_patch"] = sys.argv[21]
    manifest["owntone_native_anchor_patch_file"] = "owntone-29.3-native-anchor.patch"
    manifest["owntone_speech_jitter_patch"] = sys.argv[22]
    manifest["owntone_speech_jitter_patch_file"] = "owntone-29.3-speech-jitter.patch"
    manifest["owntone_speech_owner_patch"] = sys.argv[23]
    manifest["owntone_speech_owner_patch_file"] = "owntone-29.3-speech-owner.patch"
    manifest["owntone_speaker_balance_patch"] = sys.argv[25]
    manifest["owntone_speaker_balance_patch_file"] = "owntone-29.3-speaker-balance.patch"
    manifest["shairport_clock_recovery_patch"] = sys.argv[24]
    manifest["shairport_clock_recovery_patch_file"] = "shairport-5.5.2-clock-recovery.patch"
    manifest["shairport_native_startup_patch"] = sys.argv[26]
    manifest["shairport_native_startup_patch_file"] = "shairport-5.5.2-native-startup.patch"
    manifest["owntone_native_transition_patch"] = sys.argv[27]
    manifest["owntone_native_transition_patch_file"] = "owntone-29.3-native-transition-timer.patch"
    manifest["shairport_receiver_volume_patch"] = sys.argv[28]
    manifest["shairport_receiver_volume_patch_file"] = "shairport-5.5.2-receiver-volume.patch"
    manifest["owntone_paused_speech_patch"] = sys.argv[29]
    manifest["owntone_paused_speech_patch_file"] = "owntone-29.3-paused-speech.patch"
    manifest["shairport_bounded_events_patch"] = sys.argv[30]
    manifest["shairport_bounded_events_patch_file"] = "shairport-5.5.2-bounded-events.patch"
    manifest["owntone_event_ack_patch"] = sys.argv[31]
    manifest["owntone_event_ack_patch_file"] = "owntone-29.3-event-ack.patch"
    manifest["owntone_idle_speech_patch"] = sys.argv[32]
    manifest["owntone_idle_speech_patch_file"] = "owntone-29.3-idle-speech.patch"
    manifest["owntone_speech_drain_patch"] = sys.argv[33]
    manifest["owntone_speech_drain_patch_file"] = "owntone-29.3-speech-drain.patch"
    manifest["owntone_startup_metadata_patch"] = sys.argv[34]
    manifest["owntone_startup_metadata_patch_file"] = "owntone-29.3-startup-metadata.patch"
    manifest["owntone_cold_music_patch"] = sys.argv[35]
    manifest["owntone_cold_music_patch_file"] = "owntone-29.3-cold-music.patch"
    manifest["owntone_output_clock_patch"] = sys.argv[36]
    manifest["owntone_output_clock_patch_file"] = "owntone-29.3-output-clock.patch"
    manifest["owntone_duck_envelope_patch"] = sys.argv[37]
    manifest["owntone_duck_envelope_patch_file"] = "owntone-29.3-duck-envelope.patch"
    manifest["owntone_warm_lease_patch"] = sys.argv[38]
    manifest["owntone_warm_lease_patch_file"] = "owntone-29.3-warm-lease.patch"
    manifest["shairport_buffered_timing_patch"] = sys.argv[39]
    manifest["shairport_buffered_timing_patch_file"] = "shairport-5.5.2-buffered-timing.patch"
    json.dump(manifest, stream, indent=2)
PY
/usr/bin/python3 -I "$SOURCE/install/validate_installation.py" "$PREFIX"
printf '%s\n' "Backend binaries installed in $PREFIX; host audio services were not restarted."
