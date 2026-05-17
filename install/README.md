# Shiri — Multiroom AirPlay Manager

Shiri creates AirPlay zones using ALSA loopback devices, shairport-sync, OwnTone, and a shared host nqptp clock. Each zone gets its own Shairport and OwnTone instance on dedicated host ports.

## Requirements

- **Linux** (Ubuntu 22.04+ recommended) — uses kernel features not available on macOS
- **Root access** — required for ALSA loopback, realtime audio scheduling, and AirPlay timing
- **Python 3.8+**
- System dependencies: `nqptp`, `shairport-sync` (AirPlay 2 build), `owntone`, GStreamer 1.0 with Python GI bindings

### Running on macOS

Since Shiri requires Linux kernel features, you must use a Linux VM:

1. **Multipass** (quickest): `brew install --cask multipass`
2. **UTM / VirtualBox** with **Bridged Networking** (required for AirPlay device discovery)

Bridged networking is essential — your phone needs to see the AirPlay devices on the same network.

## Installation

### 1. Install system dependencies

```bash
sudo ./install.sh
```

This installs `nqptp`, `shairport-sync` (with AirPlay 2 + pipe support), `owntone`, and the GStreamer packages used by the zone mixer.

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Run Shiri

```bash
sudo python3 app.py
```

The web UI will be available at **http://\<your-ip\>:8080**

## Project Structure

```
app.py               Entry point — Flask app, startup, shutdown
zone.py              Zone model + ZoneManager API facade
zone_lifecycle.py    Start/stop, host audio services, and stale runtime cleanup
config.py            Persistent JSON config plus template/config generation
owntone_api.py       OwnTone REST API client for per-zone host ports

templates/           Config/script templates (native format, %%PLACEHOLDER%% syntax)
  shairport_sync.conf    Shairport-sync config template
  owntone.conf           OwnTone config template
  reset_audio_pipe.sh    Audio pipeline flush script template
  mixer_supervisor.sh    GStreamer zone mixer supervisor template

scripts/             Runtime shell scripts
  pause_bridge.sh        Shairport→OwnTone play/pause bridge
  volume_bridge.sh       Shairport→OwnTone volume bridge

static/              Web UI frontend
  index.html, app.js, style.css
```

## Troubleshooting

- **"Missing required commands"**: Run `sudo ./install.sh` to install dependencies
- **"modprobe: FATAL: Module snd-aloop not found"**: Install kernel modules:
  `sudo apt install linux-modules-extra-$(uname -r)`
- **Can't see AirPlay devices**: Verify your VM uses **Bridged Networking** and is on the same network as your phone
- **Firewall blocking mDNS**: Check that Avahi/mDNS traffic (port 5353) is allowed
