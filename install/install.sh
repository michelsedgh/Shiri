#!/usr/bin/env bash
# Install the package into a root-owned venv. Service activation is explicit.
set -euo pipefail
umask 022
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset PYTHONPATH PYTHONHOME LD_LIBRARY_PATH LD_PRELOAD
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${SHIRI_INSTALL_PREFIX:-/opt/shiri}"
[[ "$PREFIX" =~ ^/[A-Za-z0-9_./-]+$ && "$PREFIX" != / ]] || { echo "Use a canonical absolute prefix without spaces or shell expansion characters" >&2; exit 2; }
[[ $(id -u) == 0 && $(uname -s) == Linux ]] || { echo "Run as root on Ubuntu/Debian" >&2; exit 1; }
/usr/bin/python3 -I "$SOURCE/install/validate_installation.py" --create "$PREFIX"
case "${1:-}" in
  --with-backends) SHIRI_INSTALL_PREFIX="$PREFIX" "$SOURCE/install/build_backends.sh" ;;
  "") ;;
  *) echo "Usage: $0 [--with-backends]" >&2; exit 2 ;;
esac
/usr/bin/python3 -I "$SOURCE/install/apt_dependencies.py" build-essential python3-venv python3-pip \
  iproute2 isc-dhcp-client iputils-ping util-linux coreutils dbus avahi-daemon alsa-utils
# Native receiver and Bluetooth output paths do not use an ALSA loopback card.
# Virtual-device qualification remains an explicit development option.
if [[ "${SHIRI_INSTALL_LOOPBACK:-0}" == 1 ]]; then
  /usr/bin/python3 -I "$SOURCE/install/kernel_modules.py"
  modprobe snd-aloop
  install -d -m 0755 /etc/modules-load.d
  printf '%s\n' snd-aloop > /etc/modules-load.d/shiri.conf
fi
SHIRI_INSTALL_PREFIX="$PREFIX" bash "$SOURCE/install/build_helpers.sh"
mkdir -p "$PREFIX"
/usr/bin/python3 -I -m venv "$PREFIX/venv"
/usr/bin/python3 -I "$SOURCE/install/validate_installation.py" "$PREFIX"
"$PREFIX/venv/bin/python" -m pip install --require-hashes --only-binary=:all: -r "$SOURCE/install/build_requirements.lock"
"$PREFIX/venv/bin/python" -m pip install --require-hashes --only-binary=:all: -r "$SOURCE/install/requirements.lock"
"$PREFIX/venv/bin/python" -m pip install --no-index --no-build-isolation --no-deps "$SOURCE"
/usr/bin/python3 -I "$SOURCE/install/validate_installation.py" "$PREFIX"
"$PREFIX/venv/bin/python" -m pip check
"$PREFIX/venv/bin/python" - <<'PY'
import aiortc
import av
import numpy
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
if [[ "${SHIRI_INSTALL_BLUETOOTH:-0}" == 1 ]]; then
  # Pairing remains an operator action; never take over host sound/pairing state.
  apt-cache show bluez-alsa-utils >/dev/null 2>&1 || {
    echo "This distro has no bluez-alsa-utils package; install a supported BlueALSA adapter first" >&2; exit 1;
  }
  /usr/bin/python3 -I "$SOURCE/install/apt_dependencies.py" bluez bluez-alsa-utils
fi
printf '%s\n' "Package installed in $PREFIX/venv. Run deploy/install_services.sh to configure services."
printf '%s\n' "Existing audio services and host DHCP hooks remain in place."
