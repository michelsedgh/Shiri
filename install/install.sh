#!/usr/bin/env bash
# Install the package into a root-owned venv. Service activation is explicit.
set -euo pipefail
umask 022
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset PYTHONPATH PYTHONHOME LD_LIBRARY_PATH LD_PRELOAD
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${SHIRI_INSTALL_PREFIX:-/opt/shiri}"
[[ "$PREFIX" == /* && "$PREFIX" != / ]] || { echo "Use an absolute installation prefix" >&2; exit 2; }
[[ $(id -u) == 0 && $(uname -s) == Linux ]] || { echo "Run as root on Ubuntu/Debian" >&2; exit 1; }
/usr/bin/python3 -I "$SOURCE/install/validate_installation.py" --create "$PREFIX"
case "${1:-}" in
  --with-backends) SHIRI_INSTALL_PREFIX="$PREFIX" "$SOURCE/install/build_backends.sh" ;;
  "") ;;
  *) echo "Usage: $0 [--with-backends]" >&2; exit 2 ;;
esac
apt-get update
apt-get install -y python3-venv python3-pip python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  iproute2 isc-dhcp-client iputils-ping util-linux coreutils dbus avahi-daemon alsa-utils \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-alsa
mkdir -p "$PREFIX"
/usr/bin/python3 -I -m venv --system-site-packages "$PREFIX/venv"
/usr/bin/python3 -I "$SOURCE/install/validate_installation.py" "$PREFIX"
"$PREFIX/venv/bin/python" -m pip install --require-hashes -r "$SOURCE/install/requirements.lock"
"$PREFIX/venv/bin/python" -m pip install --no-deps "$SOURCE"
/usr/bin/python3 -I "$SOURCE/install/validate_installation.py" "$PREFIX"
"$PREFIX/venv/bin/python" - <<'PY'
import gi
import aiortc
import av
import numpy
gi.require_version("Gst", "1.0")
gi.require_version("GstAudio", "1.0")
from gi.repository import Gst, GstAudio
Gst.init(None)
GstAudio.AudioInfo()
for factory in ("alsasrc", "alsasink", "audiomixer", "audioconvert", "audioresample", "appsrc", "appsink"):
    if Gst.ElementFactory.find(factory) is None:
        raise SystemExit("Missing GStreamer factory: " + factory)
PY
install -d -m 0755 /etc/dhcp/shiri
install -m 0755 "$SOURCE/shiri/runtime/dhclient_hook.py" /etc/dhcp/shiri/dhclient-script
if [[ -f /etc/apparmor.d/sbin.dhclient ]]; then
  install -d -m 0755 /etc/apparmor.d/local
  touch /etc/apparmor.d/local/sbin.dhclient
  RULE='  /etc/dhcp/shiri/dhclient-script Uxr,'
  if ! awk -v rule="$RULE" '$0==rule { found=1 } END { exit !found }' /etc/apparmor.d/local/sbin.dhclient; then
    printf '%s\n' "$RULE" >> /etc/apparmor.d/local/sbin.dhclient
  fi
  if command -v apparmor_parser >/dev/null; then apparmor_parser -r /etc/apparmor.d/sbin.dhclient; fi
fi
modprobe snd-aloop
if [[ "${SHIRI_INSTALL_BLUETOOTH:-0}" == 1 ]]; then
  # Pairing remains an operator action; never take over host sound/pairing state.
  apt-cache show bluez-alsa-utils >/dev/null 2>&1 || {
    echo "This distro has no bluez-alsa-utils package; install a supported BlueALSA adapter first" >&2; exit 1;
  }
  apt-get install -y bluez bluez-alsa-utils
fi
install -d -m 0755 /etc/modules-load.d
printf '%s\n' snd-aloop > /etc/modules-load.d/shiri.conf
printf '%s\n' "Package installed in $PREFIX/venv. Run deploy/install_services.sh to configure services."
printf '%s\n' "Existing audio services and host DHCP hooks remain in place."
