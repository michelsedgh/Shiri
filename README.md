# Shiri AirPlay Stack

Shiri exposes one virtual AirPlay receiver per room, mixes room audio with Shiri TTS, then sends the mixed stream to the selected speaker through OwnTone.

## Live Audio Path

```
iPhone / Apple Music
  -> Shairport Sync 5 AirPlay 2 receiver (per-zone network namespace)
  -> ALSA loopback subdevice
  -> Shiri GStreamer mixer (music + TTS ducking/overlay)
  -> OwnTone raw PCM pipe input
  -> OwnTone 29 AirPlay 2 sender (shared sender namespace)
  -> real AirPlay speaker
```

The mixer stays in the path because it is where Shiri can duck music and overlay TTS. Shairport Sync still owns the AirPlay receiver side; OwnTone owns the AirPlay sender side.

## Network and PTP Layout

AirPlay 2 timing uses PTP ports that cannot be cleanly shared by unrelated receiver/sender daemons in one namespace. Shiri isolates them:

- Each Shairport receiver zone runs in its own namespace: `shiri_rx_<zone>`.
- Each receiver namespace has one macvlan LAN address, one Avahi, one D-Bus, one `nqptp`, and one `shairport-sync`.
- All OwnTone sender instances run in shared namespace `shiri_ot`.
- `shiri_ot` has one LAN macvlan address, one API veth (`10.211.0.2` inside the namespace), one Avahi, one D-Bus, and one `airptpd`.
- `airptpd` is for OwnTone's AirPlay 2 sender timing. `nqptp` is for Shairport Sync's AirPlay 2 receiver timing.

## Latency Policy

OwnTone uses `start_buffer_ms = 500` in `templates/owntone.conf`. This is the main startup-buffer tuning point for OwnTone's speaker output.

Shairport Sync's `audio_backend_latency_offset_in_seconds` is not a pipeline delay workaround. It is clamped to +/- 0.25 seconds and defaults to `0.0`, matching the Shairport Sync docs guidance for small hardware compensation only.

Shairport Sync's backend buffer is set to `0.1` seconds and the Shiri mixer uses 10 ms buffers with 30 ms mixer latency. These are small bridge costs; the remaining user-perceived delay mostly comes from the AirPlay 2 source's buffered stream and OwnTone's AirPlay 2 output buffer. The Living Room AirPlay 2 output rejected `150ms` because its minimum latency is `250ms`; `500ms` is the current stable low-latency setting.

OwnTone pipe input is explicitly configured as `pipe_sample_rate = 48000` and `pipe_bits_per_sample = 16`, matching Shairport Sync and the mixer. OwnTone 29.2 defaults pipe input to 44.1 kHz unless this is set.

The old play-start reset hook and pause mute bridge were removed. They were AirPlay 1-era workarounds and caused OwnTone FIFO interruptions, false mutes, volume jumps, and mixer backpressure in the AirPlay 2 pipeline.

## Runtime Files

Generated runtime data lives under `/var/lib/shiri`:

- `/var/lib/shiri/config.json`: persisted zones, rooms, speaker choices, and volumes.
- `/var/lib/shiri/groups/<zone>/config`: generated Shairport/OwnTone/mixer configs.
- `/var/lib/shiri/groups/<zone>/logs`: per-zone logs.
- `/var/lib/shiri/groups/<zone>/pipes/audio.pipe`: mixed PCM into OwnTone.
- `/var/lib/shiri/owntone-sender/state`: shared OwnTone sender namespace state.

The generated zone `config`, `state`, and `logs` directories are cleared on daemon startup so stale hook scripts cannot linger.

## Operations

Start Shiri:

```bash
sudo /home/ubuntu/Shiri/scripts/shiri_service.sh start
sudo /home/ubuntu/Shiri/scripts/shiri_service.sh stop
sudo /home/ubuntu/Shiri/scripts/shiri_service.sh restart
sudo /home/ubuntu/Shiri/scripts/shiri_service.sh status
```

Check the live stack:

```bash
ps -eo pid,ppid,args | rg 'app.py|shairport-sync|owntone|nqptp|airptpd|audio_mixer'
sudo ip netns list
curl -s http://10.211.0.2:3869/api/outputs | jq '.outputs[] | {name,type,selected,format,volume}'
```

Important logs:

```bash
tail -f /home/ubuntu/shiri-app.log
tail -f /var/lib/shiri/groups/zone_b18972bb/logs/shairport.log
tail -f /var/lib/shiri/groups/zone_b18972bb/logs/owntone.log
tail -f /var/lib/shiri/groups/zone_b18972bb/logs/mixer.log
```
