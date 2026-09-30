#!/usr/bin/env bash
# Configure services without starting them or replacing an existing API token.
set -euo pipefail
umask 022
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset PYTHONPATH PYTHONHOME LD_LIBRARY_PATH LD_PRELOAD
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${SHIRI_INSTALL_PREFIX:-/opt/shiri}"
[[ $(id -u) == 0 && $(uname -s) == Linux ]] || { echo "Run as root on Linux" >&2; exit 1; }
[[ "$PREFIX" =~ ^/[A-Za-z0-9_./-]+$ && "$PREFIX" != / ]] || { echo "Use a safe absolute prefix" >&2; exit 2; }
/usr/bin/python3 -I "$SOURCE/install/validate_installation.py" "$PREFIX"
[[ -x "$PREFIX/venv/bin/python" ]] || { echo "Install the package first" >&2; exit 1; }
if [[ "${1:-}" != --locked ]]; then
  exec /usr/bin/python3 -I "$SOURCE/deploy/adopt_state.py" --run "$SOURCE/deploy/install_services.sh"
fi
getent group shiri >/dev/null || groupadd --system shiri
id shiri >/dev/null 2>&1 || useradd --system --gid shiri --home-dir /var/lib/shiri --shell /usr/sbin/nologin shiri
/usr/bin/python3 -I "$SOURCE/deploy/adopt_state.py" --check-only
# Validate/adopt existing files before changing their parent-directory owner.
/usr/bin/python3 -I "$SOURCE/deploy/adopt_state.py"
runuser -u shiri -- "$PREFIX/venv/bin/python" -I -c 'import shiri.cli'
install -d -o shiri -g shiri -m 0750 /var/lib/shiri
install -d -o root -g shiri -m 0750 /run/shiri
install -d -o root -g root -m 0700 /var/lib/shiri-runtime
install -d -o root -g shiri -m 0750 /etc/shiri
[[ ! -L /etc/shiri/api-token && ! -L /etc/shiri/shiri.env ]] || { echo "Refusing symlink service credentials/config" >&2; exit 1; }
if [[ ! -f /etc/shiri/api-token ]]; then
  TOKEN_FILE=$(mktemp /etc/shiri/.token.XXXXXX)
  trap 'rm -f "$TOKEN_FILE"' EXIT
  /usr/bin/python3 -I -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN_FILE"
  chown root:shiri "$TOKEN_FILE"
  chmod 0640 "$TOKEN_FILE"
  mv "$TOKEN_FILE" /etc/shiri/api-token
fi
if [[ ! -f /etc/shiri/shiri.env ]]; then
  cat > /etc/shiri/shiri.env <<ENV
SHIRI_STATE_DIR=/var/lib/shiri
SHIRI_RUNTIME_STATE_DIR=/var/lib/shiri-runtime
SHIRI_RUNTIME_DIR=/run/shiri
SHIRI_RUNTIME_SOCKET=/run/shiri/runtime.sock
SHIRI_BINARY_DIR=$PREFIX
SHIRI_API_TOKEN_FILE=/etc/shiri/api-token
SHIRI_HOST=127.0.0.1
SHIRI_PORT=8080
LD_LIBRARY_PATH=$PREFIX/lib
ENV
  chown root:shiri /etc/shiri/shiri.env
  chmod 0640 /etc/shiri/shiri.env
fi
/usr/bin/python3 -I "$SOURCE/deploy/adopt_state.py"
for service in shiri-runtime shiri-api; do
  UNIT_FILE=$(mktemp /etc/systemd/system/.shiri-unit.XXXXXX)
  trap 'rm -f "${TOKEN_FILE:-}" "${UNIT_FILE:-}"' EXIT
  sed "s|@PREFIX@|$PREFIX|g" "$SOURCE/deploy/$service.service" > "$UNIT_FILE"
  chmod 0644 "$UNIT_FILE"
  mv "$UNIT_FILE" "/etc/systemd/system/$service.service"
done
systemctl daemon-reload
printf '%s\n' "Services configured. Review /etc/shiri/shiri.env, then:"
printf '%s\n' "  sudo systemctl enable --now shiri-runtime shiri-api"
printf '%s\n' "The API binds localhost by default. Pair Bluetooth/BlueALSA separately when needed."
