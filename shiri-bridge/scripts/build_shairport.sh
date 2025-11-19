#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHAIRPORT_DIR="${SCRIPT_DIR}/../third_party/shairport-sync"
OS_NAME="$(uname -s 2>/dev/null || echo Unknown)"
MDNS_BACKEND="${SHIRI_MDNS_BACKEND:-avahi}"

usage() {
    cat <<EOF
Usage: $(basename "$0") [--mdns=<avahi|tinysvc>]

Environment override: export SHIRI_MDNS_BACKEND=avahi|tinysvc
EOF
}

for arg in "$@"; do
    case "$arg" in
        --mdns=*)
            MDNS_BACKEND="${arg#*=}"
            ;;
        --mdns-avahi)
            MDNS_BACKEND="avahi"
            ;;
        --mdns-tinysvc)
            MDNS_BACKEND="tinysvc"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg"
            usage
            exit 1
            ;;
    esac
done

case "$MDNS_BACKEND" in
    avahi|tinysvc) ;;
    *)
        echo "Invalid mdns backend '$MDNS_BACKEND' (expected avahi or tinysvc)"
        exit 1
        ;;
esac

cpu_count() {
    if command -v nproc >/dev/null 2>&1; then
        nproc
    elif command -v sysctl >/dev/null 2>&1 && sysctl -n hw.ncpu >/dev/null 2>&1; then
        sysctl -n hw.ncpu
    else
        echo 1
    fi
}

print_install_hint() {
    case "$OS_NAME" in
        Darwin)
            echo "  brew install autoconf automake libtool pkg-config popt libconfig libdaemon openssl"
            ;;
        Linux)
            if [[ -f /etc/debian_version ]]; then
                echo "  sudo apt update && sudo apt install build-essential autoconf automake libtool pkg-config \\"
                echo "       libpopt-dev libconfig-dev libdaemon-dev libssl-dev"
            else
                echo "  Use your distribution's package manager to install:"
                echo "       autoconf automake libtool pkg-config libpopt-dev libconfig-dev libdaemon-dev libssl-dev"
            fi
            ;;
        *)
            echo "  Install autoconf automake libtool pkg-config libpopt-dev libconfig-dev libdaemon-dev libssl-dev"
            ;;
    esac
}

echo "Checking for required tools (mDNS backend: $MDNS_BACKEND)..."

MISSING_TOOLS=()

for tool in autoconf automake libtool pkg-config; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        MISSING_TOOLS+=("$tool")
    fi
done

if command -v pkg-config >/dev/null 2>&1; then
    for pkg in popt libconfig; do
        if ! pkg-config --exists "$pkg"; then
            MISSING_TOOLS+=("$pkg")
        fi
    done
    if [[ "$MDNS_BACKEND" == "avahi" ]] && ! pkg-config --exists avahi-client; then
        MISSING_TOOLS+=("avahi-client")
    fi
else
    MISSING_TOOLS+=(popt libconfig)
    [[ "$MDNS_BACKEND" == "avahi" ]] && MISSING_TOOLS+=("avahi-client")
fi

if ((${#MISSING_TOOLS[@]} > 0)); then
    printf 'Error: missing build tools or libraries:%s\n' " ${MISSING_TOOLS[*]}"
    echo "Install them with:"
    print_install_hint
    exit 1
fi

echo "Building shairport-sync..."
cd "$SHAIRPORT_DIR"

autoreconf -fi

CONFIGURE_ARGS=(
    --with-ssl=openssl
    --with-pipe
    --with-stdout
    --without-alsa
    --without-pa
    --without-configfiles
    --without-soxr
    --without-metadata
)

if [[ "$MDNS_BACKEND" == "avahi" ]]; then
    CONFIGURE_ARGS+=(--with-avahi)
else
    CONFIGURE_ARGS+=(--with-tinysvcmdns)
fi

./configure "${CONFIGURE_ARGS[@]}"
make -j"$(cpu_count)"

echo "Build complete."
