#!/usr/bin/env bash
###############################################################################
# install_deps_ubuntu.sh
#
# Installs all dependencies needed for the dual_zone_demo.sh script on Ubuntu.
# Run with: sudo ./install_deps_ubuntu.sh
###############################################################################
set -euo pipefail

log() { echo "[$(date '+%H:%M:%S')] $*"; }

if [[ $(id -u) -ne 0 ]]; then
  echo "Run this script as root (sudo)." >&2
  exit 1
fi

log "Updating package lists..."
apt update

log "Installing base packages..."
apt install -y \
  iproute2 \
  isc-dhcp-client \
  util-linux \
  dbus \
  avahi-daemon \
  jq \
  curl \
  coreutils \
  build-essential \
  git \
  autoconf \
  automake \
  libtool \
  pkg-config

log "Installing shairport-sync build dependencies..."
apt install -y \
  libpopt-dev \
  libconfig-dev \
  libssl-dev \
  libavahi-client-dev \
  libsoxr-dev \
  libpulse-dev \
  libasound2-dev \
  libavcodec-dev \
  libavformat-dev \
  libavutil-dev \
  libgcrypt20-dev \
  libsodium-dev \
  libplist-dev \
  xxd

# Check if nqptp is installed
if ! command -v nqptp &>/dev/null; then
  log "Building and installing nqptp (required for AirPlay 2)..."
  TMPDIR=$(mktemp -d)
  cd "$TMPDIR"
  git clone https://github.com/mikebrady/nqptp.git
  cd nqptp
  autoreconf -fi
  ./configure
  make -j"$(nproc)"
  make install
  cd /
  rm -rf "$TMPDIR"
  log "nqptp installed."
else
  log "nqptp already installed."
fi

# Check if shairport-sync is installed with AirPlay 2 support
if ! command -v shairport-sync &>/dev/null; then
  log "Building and installing shairport-sync (AirPlay 2 build)..."
  TMPDIR=$(mktemp -d)
  cd "$TMPDIR"
  git clone https://github.com/mikebrady/shairport-sync.git
  cd shairport-sync
  autoreconf -fi
  ./configure --sysconfdir=/etc \
    --with-avahi \
    --with-ssl=openssl \
    --with-airplay-2 \
    --with-soxr \
    --with-pipe \
    --with-metadata
  make -j"$(nproc)"
  make install
  cd /
  rm -rf "$TMPDIR"
  log "shairport-sync installed."
else
  log "shairport-sync already installed. Verify it has AirPlay 2 + pipe support."
fi

# Install OwnTone
if ! command -v owntone &>/dev/null; then
  log "Installing OwnTone from package repository..."
  # Try the official PPA or package
  if apt-cache show owntone-server &>/dev/null; then
    apt install -y owntone-server
  else
    log "owntone-server package not found in repos."
    log "You may need to build from source: https://owntone.github.io/owntone-server/installation/"
    log "Or add the OwnTone PPA."
  fi
else
  log "OwnTone already installed."
fi

log ""
log "=========================================="
log "  Dependency installation complete!"
log "=========================================="
log ""
log "Verify installations:"
echo "  nqptp:          $(command -v nqptp || echo 'NOT FOUND')"
echo "  shairport-sync: $(command -v shairport-sync || echo 'NOT FOUND')"
echo "  owntone:        $(command -v owntone || echo 'NOT FOUND')"
echo ""
log "You can now run: sudo ./dual_zone_demo.sh"
