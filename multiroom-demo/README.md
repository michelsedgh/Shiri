# Multiroom Demo

Proof-of-concept script demonstrating two independent AirPlay 2 zones, each with its own shairport-sync instance (in a separate network namespace) and OwnTone instance (unique ports) for multiroom speaker routing.

## Prerequisites (Ubuntu 22.04+)

### 1. Install system packages

```bash
sudo apt update
sudo apt install -y \
  iproute2 \
  isc-dhcp-client \
  util-linux \
  dbus \
  avahi-daemon \
  jq \
  curl \
  coreutils
```

### 2. Install shairport-sync (AirPlay 2 build)

You need the **AirPlay 2** build with pipe backend and Avahi support:

```bash
# Dependencies
sudo apt install -y build-essential git autoconf automake libtool \
  libpopt-dev libconfig-dev libssl-dev libavahi-client-dev \
  libsoxr-dev libpulse-dev libasound2-dev libavcodec-dev libavformat-dev \
  libavutil-dev libgcrypt20-dev libsodium-dev libplist-dev xxd

# Clone and build nqptp (required for AirPlay 2)
git clone https://github.com/mikebrady/nqptp.git
cd nqptp
autoreconf -fi
./configure
make
sudo make install
cd ..

# Clone and build shairport-sync with AirPlay 2 support
git clone https://github.com/mikebrady/shairport-sync.git
cd shairport-sync
autoreconf -fi
./configure --sysconfdir=/etc --with-avahi --with-ssl=openssl \
  --with-airplay-2 --with-soxr --with-pipe --with-metadata
make
sudo make install
cd ..
```

### 3. Install OwnTone

```bash
# From official repo (Ubuntu 22.04+)
sudo apt install -y owntone-server

# Or build from source: https://owntone.github.io/owntone-server/installation/
```

Verify OwnTone is installed:
```bash
owntone --version
```

### 4. Kernel requirements

Your network interface must support **macvlan**. Most physical NICs do; some VM/container setups may not.

Test with:
```bash
sudo ip link add testmv link eth0 type macvlan mode bridge
sudo ip link delete testmv
```
(Replace `eth0` with your interface name.)

---

## Usage

```bash
cd /path/to/Shiri/multiroom-demo
sudo ./dual_zone_demo.sh
```

The script will:

1. **Prompt for network interface** – select the parent NIC for macvlan creation.
2. **Create directories and FIFOs** under `/var/lib/shiri/groups/{zone1,zone2}/`.
3. **Spin up two shairport-sync instances**, each in its own network namespace with a separate IP/MAC, advertising as "Shiri zone1" and "Shiri zone2".
4. **Start two OwnTone instances** on unique ports (library: 3689/3690, websocket: 49100/49101, mpd: 6600/6601).
5. **Query OwnTone for available speakers** and let you pick one speaker per zone.
6. **Keep running** until you press Ctrl+C, at which point it tears down namespaces and stops all services.

---

## What you'll see

On your iPhone/Mac:
- Two AirPlay destinations: **Shiri zone1** and **Shiri zone2**.

Each zone routes audio independently:
- zone1 → the speaker you selected for zone1's OwnTone.
- zone2 → the speaker you selected for zone2's OwnTone.

---

## Logs

All logs are written to:
```
/var/lib/shiri/groups/zone1/logs/
/var/lib/shiri/groups/zone2/logs/
```

---

## Troubleshooting

| Symptom | Possible cause |
|---------|---------------|
| "Missing required commands" | Install the packages listed in Prerequisites. |
| macvlan creation fails | Your NIC or VM doesn't support macvlan. Try a physical NIC. |
| shairport-sync not visible on iPhone | Check `shairport_wrapper.log` and `shairport.log` for errors. Ensure Avahi is running in the namespace. |
| No speakers listed in OwnTone | OwnTone may need more time to discover speakers. Check `owntone.log`. |
| Speaker not playing | Ensure the speaker is on and reachable. Try toggling it manually via OwnTone web UI at `http://localhost:3689` (zone1) or `:3690` (zone2). |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Host (Ubuntu)                                                      │
│                                                                     │
│  ┌──────────────────┐        ┌──────────────────┐                   │
│  │  netns: zone1    │        │  netns: zone2    │                   │
│  │  ┌────────────┐  │        │  ┌────────────┐  │                   │
│  │  │ shairport  │  │        │  │ shairport  │  │                   │
│  │  │  -sync     │  │        │  │  -sync     │  │                   │
│  │  │ "zone1"    │  │        │  │ "zone2"    │  │                   │
│  │  └─────┬──────┘  │        │  └─────┬──────┘  │                   │
│  │        │ PCM     │        │        │ PCM     │                   │
│  │        ▼         │        │        ▼         │                   │
│  │   audio.pipe     │        │   audio.pipe     │                   │
│  └────────┬─────────┘        └────────┬─────────┘                   │
│           │                           │                             │
│           ▼                           ▼                             │
│  ┌──────────────────┐        ┌──────────────────┐                   │
│  │  OwnTone         │        │  OwnTone         │                   │
│  │  port 3689       │        │  port 3690       │                   │
│  │  → Speaker A     │        │  → Speaker B     │                   │
│  └──────────────────┘        └──────────────────┘                   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Extending

To add more zones, edit `GROUPS=("zone1" "zone2" "zone3" ...)` at the top of the script. Each zone gets its own:
- Network namespace + macvlan
- shairport-sync instance
- OwnTone instance (ports auto-increment)
- Speaker selection prompt
