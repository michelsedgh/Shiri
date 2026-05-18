# Shiri Install Notes

Shiri requires Linux, root privileges, bridged networking, and current AirPlay 2 builds of:

- Shairport Sync 5 with AirPlay 2, Avahi, ALSA, metadata, and soxr support
- NQPTP for Shairport Sync receiver timing
- OwnTone 29 with AirPlay 2 support
- libairptp / `airptpd` for OwnTone sender timing
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
- The old pause bridge and play-start reset hook are intentionally gone.

## Troubleshooting

- Missing ALSA loopback: `sudo apt install linux-modules-extra-$(uname -r)` then restart.
- Can't see `liv`/room AirPlay targets: confirm bridged networking and mDNS/Avahi visibility.
- Chopped audio: check `mixer.log` for downstream warnings and `owntone.log` for pipe stop/flush messages.
- OwnTone not AirPlay 2: check `/api/outputs`; selected real speakers should show `type: "AirPlay 2"` and `format: "alac"`.
