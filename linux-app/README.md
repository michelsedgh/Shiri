Shiri Linux (prototype)

Minimal Linux GUI app to start per-room shairport-sync containers and fan-out audio via HTTP to UPnP devices.

Build

```bash
cd linux-app
go build ./cmd/shiri-linux
./shiri-linux
```

Prereqs

- Docker or Podman
- ffmpeg

Usage

- Create a room, select AirPlay NIC (for container network) and Speakers NIC.
- Start: app creates macvlan network, starts shairport container, encodes PCM to MP3, serves on http://<speaker-ip>:809X/stream.
- Discover speakers: finds UPnP renderers; resolve to AVTransport control URLs.
- Start will send SetAVTransportURI+Play to listed targets.


