# Shiri Install Notes

Shiri requires Linux, root privileges, bridged networking, and AirPlay 2 builds of:

- Shairport Sync 5.0.4 with AirPlay 2, Avahi, ALSA, metadata, and soxr support
- NQPTP 1.2.8 for Shairport Sync receiver timing
- OwnTone 29.2 with AirPlay 2 support
- libairptp / `airptpd` 0.5 for OwnTone sender timing
- GStreamer 1.0 with Python GI bindings

The runtime architecture is documented in the root `README.md`.

## Start

```bash
sudo /home/ubuntu/Shiri/scripts/shiri_service.sh start
sudo /home/ubuntu/Shiri/scripts/shiri_service.sh status
sudo /home/ubuntu/Shiri/scripts/shiri_service.sh restart
sudo /home/ubuntu/Shiri/scripts/shiri_service.sh stop
```

The web UI listens on `http://<host-ip>:8080`.

## Expected Runtime Shape

- No system `shairport-sync`, `nqptp`, `owntone`, or `airptpd` services should own the stack.
- Shiri starts per-zone Shairport receiver namespaces with their own `nqptp`.
- Shiri starts one shared OwnTone sender namespace with `airptpd`.
- Shiri uses macvlan, not ipvlan. macvlan gives each namespace a stable Shiri MAC; ipvlan shares the host MAC and can confuse DHCP/router state on this VM.
- Shiri installs `/etc/dhcp/dhclient-script` from `scripts/dhclient_namespace.sh` so namespace DHCP does not run host DNS/time hooks.
- The pause bridge and play-start reset hook are intentionally gone.
- Shiri receives TTS through WebRTC signaling/control and terminates audio inside
  the zone mixer.

## Troubleshooting

- Missing ALSA loopback: `sudo apt install linux-modules-extra-$(uname -r)` then restart.
- Can't see `liv`/room AirPlay targets: confirm bridged networking and mDNS/Avahi visibility.
- Speaker disappears after router/VM weirdness: run `sudo /home/ubuntu/Shiri/scripts/shiri_service.sh restart`; startup will fail with a LAN preflight error if macvlan DHCP works but router unicast/ARP is broken.
- Chopped audio: check `mixer.log` for downstream warnings and `owntone.log` for pipe stop/flush messages.
- OwnTone not AirPlay 2: check `/api/outputs`; selected real speakers should show `type: "AirPlay 2"` and `format: "alac"`.
