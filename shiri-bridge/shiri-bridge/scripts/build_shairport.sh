#!/bin/bash
set -e

echo "Checking for required tools..."

MISSING_TOOLS=""

if ! command -v autoconf &> /dev/null; then
    MISSING_TOOLS="$MISSING_TOOLS autoconf"
fi
if ! command -v automake &> /dev/null; then
    MISSING_TOOLS="$MISSING_TOOLS automake"
fi
if ! command -v libtool &> /dev/null; then
    MISSING_TOOLS="$MISSING_TOOLS libtool"
fi
if ! command -v pkg-config &> /dev/null; then
    MISSING_TOOLS="$MISSING_TOOLS pkg-config"
fi

if command -v pkg-config &> /dev/null; then
    if ! pkg-config --exists popt; then
         MISSING_TOOLS="$MISSING_TOOLS popt"
    fi
    if ! pkg-config --exists libconfig; then
         MISSING_TOOLS="$MISSING_TOOLS libconfig"
    fi
else
    MISSING_TOOLS="$MISSING_TOOLS popt libconfig"
fi

if [ -n "$MISSING_TOOLS" ]; then
    echo "Error: Missing build tools or libraries: $MISSING_TOOLS"
    echo "Please install them using Homebrew:"
    echo "  brew install autoconf automake libtool pkg-config popt libconfig"
    exit 1
fi

echo "Building shairport-sync..."
cd "$(dirname "$0")/../third_party/shairport-sync"
autoreconf -fi
./configure --with-pipe --with-stdout --without-alsa --without-pa --without-avahi --without-configfiles --without-soxr --without-metadata
make -j$(sysctl -n hw.ncpu)

echo "Build complete."
