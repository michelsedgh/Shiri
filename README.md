# Shiri AirPlay Stack

Shiri exposes one virtual AirPlay receiver per room, mixes room audio with Shiri TTS, then sends the mixed stream to the selected speaker through OwnTone.

Current tested binaries on the Ubuntu host:

- Shairport Sync `5.0.4-AirPlay2-smi10-OpenSSL-Avahi-ALSA-soxr-metadata`
- OwnTone `29.2`
- NQPTP `1.2.8`
- libairptp / `airptpd` `0.5`

## Live Audio Path

```
iPhone / Apple Music
  -> Shairport Sync 5 AirPlay 2 receiver (`shiri_rx_<zone>`)
  -> ALSA loopback subdevice
  -> Shiri GStreamer mixer (music + TTS ducking/overlay)
  -> OwnTone raw PCM pipe input
  -> OwnTone 29.2 AirPlay 2 sender (`shiri_ot`)
  -> real AirPlay speaker
```

The mixer stays in the path because it is where Shiri can duck music and overlay TTS. Shairport Sync still owns the AirPlay receiver side; OwnTone owns the AirPlay sender side.

## Network and PTP Layout

AirPlay 2 timing uses PTP. The important detail is that the receiver side and sender side are not the same job:

- Shairport Sync is Shiri's AirPlay 2 receiver. It makes `liv`, `bathhhh`, etc. appear on the iPhone.
- OwnTone is Shiri's AirPlay 2 sender. It sends the mixed stream to real speakers.
- `nqptp` provides PTP timing information for Shairport Sync.
- `airptpd` provides PTP timing information for OwnTone/libairptp.

`nqptp` and `airptpd` both need the PTP event/general ports, normally UDP `319` and `320`. They cannot both own those ports cleanly in one network namespace. Shiri isolates the receiver and sender timing domains with Linux network namespaces.

### Process Placement

- Each Shairport receiver zone runs in its own namespace: `shiri_rx_<zone>`.
- Each receiver namespace has one LAN macvlan interface, one private Avahi, one private D-Bus, one `nqptp`, and one `shairport-sync`.
- All OwnTone sender instances run in shared namespace `shiri_ot`.
- `shiri_ot` has one LAN macvlan interface, one API veth, one private Avahi, one private D-Bus, one `airptpd`, and one OwnTone process per zone.
- The host namespace runs Flask, the web UI, the TTS WebSocket server, the GStreamer mixers, and ALSA loopback.

There is intentionally no host-level `nqptp`, no per-zone OwnTone sender namespace, and no direct host listener for OwnTone. The single sender namespace lets OwnTone's AirPlay 2 output instances share `airptpd`; receiver namespaces let each Shairport Sync instance own its own AP2 PTP timing.

### Namespace Map

| Namespace | LAN interface | Purpose | Timing daemon | Main process |
| --- | --- | --- | --- | --- |
| `shiri_ot` | `otlan0` | OwnTone sender side; discovers and streams to real speakers | `airptpd` | one OwnTone per zone |
| `shiri_rx_<zone>` | `rx<subdevice>` | Virtual AirPlay receiver visible to iPhone | `nqptp` | one Shairport Sync |
| host namespace | `enp0s1`, `otapi0` | Flask UI, mixer, ALSA loopback, HTTP access to OwnTone API | none | `app.py`, `audio_mixer.py` |

### Per-Zone Receiver Side

Each zone gets a Shairport Sync instance in a dedicated receiver namespace.

For zone `zone_b18972bb` / `liv`, the working shape is:

```text
namespace:       shiri_rx_b18972bb
LAN interface:   rx0
AirPlay name:    liv
timing daemon:   nqptp
receiver daemon: shairport-sync
ALSA output:     hw:Loopback,0,0
```

For zone `zone_fa7facc7` / `bathhhh`, the working shape is:

```text
namespace:       shiri_rx_fa7facc7
LAN interface:   rx1
AirPlay name:    bathhhh
timing daemon:   nqptp
receiver daemon: shairport-sync
ALSA output:     hw:Loopback,0,1
```

Shairport Sync is configured with unique ports per zone:

- RTSP port: `7000 + loopback_subdevice`
- UDP base: `6001 + loopback_subdevice * 100`
- UDP range: `100`
- AirPlay device ID offset: `loopback_subdevice + 1`
- interface: `rx<loopback_subdevice>`

The iPhone sends audio to the Shairport Sync receiver. Shairport Sync writes decoded audio into the ALSA loopback subdevice. The host-side mixer captures the matching loopback capture device, overlays/ducks TTS, and writes PCM into that zone's OwnTone pipe.

### Shared OwnTone Sender Side

All OwnTone instances run inside `shiri_ot`, not one namespace per zone. This is deliberate:

- OwnTone's AirPlay 2 sender side should share one `airptpd`.
- Multiple `airptpd` instances in one namespace would conflict on PTP ports.
- Multiple OwnTone instances can coexist in `shiri_ot` because each one uses unique API/websocket/MPD ports and Shiri disables unneeded OwnTone mDNS publishing flags.

The host talks to OwnTone APIs through the veth pair:

- host side: `otapi0`, `10.211.0.1/30`
- namespace side: `otapi1`, `10.211.0.2/30`
- OwnTone API base: `http://10.211.0.2:<zone_port>`

OwnTone zone ports are allocated from the loopback subdevice:

- HTTP/API port: `3869 + loopback_subdevice * 10`
- WebSocket port: `3868 + loopback_subdevice * 10`
- MPD port: `6700 + loopback_subdevice`

Examples:

```text
liv:     OwnTone API http://10.211.0.2:3869
bathhhh: OwnTone API http://10.211.0.2:3879
```

OwnTone discovers real speakers from inside `shiri_ot` over `otlan0`. It streams to AirPlay 2 speakers through that namespace and uses `airptpd` for sender PTP timing. OwnTone also has `Local Output` / `ALSA`, which Shiri allows for local devices such as the VM Bluetooth speaker path.

### Avahi and D-Bus Isolation

Every namespace gets its own D-Bus config and Avahi daemon runtime:

- receiver runtime: `/var/lib/shiri/groups/<zone>/state/rx-runtime`
- sender runtime: `/var/lib/shiri/owntone-sender/state`

The isolated launcher bind-mounts namespace-specific `/dev/shm` and `/run/avahi-daemon` directories before entering the network namespace. This prevents Avahi and the PTP daemons from sharing the wrong host runtime files.

Receiver namespaces publish the virtual Shairport Sync AirPlay devices. The sender namespace browses real AirPlay speakers for OwnTone. OwnTone is launched with:

```text
--mdns-no-rsp --mdns-no-daap --mdns-no-web --mdns-no-cname
```

Those flags keep the multiple OwnTone instances from publishing extra server/web/DAAP identities while still allowing speaker discovery/output control.

### API and Audio Crossings

There are only two intentional crossings between host and namespaces:

- HTTP API crossing: host `otapi0` to sender namespace `otapi1`.
- Audio file crossing: host mixer writes `/var/lib/shiri/groups/<zone>/pipes/audio.pipe`; OwnTone in `shiri_ot` reads the same filesystem FIFO.

The AirPlay receiver traffic and AirPlay speaker output traffic stay on the LAN-facing macvlan interfaces inside their namespaces.

The LAN-facing AirPlay traffic uses macvlan:

- sender namespace: `shiri_ot/otlan0`
- receiver namespace: `shiri_rx_<zone>/rx<subdevice>`
- parent NIC: `enp0s1`

### Startup Order

For each zone, startup follows this shape:

1. Allocate an ALSA loopback subdevice.
2. Clear generated runtime dirs and recreate FIFOs.
3. Generate Shairport Sync and OwnTone configs.
4. Ensure the shared `shiri_ot` sender namespace exists. This creates `otapi0`/`otapi1`, `otlan0`, private D-Bus, private Avahi, and `airptpd`.
5. Create the zone receiver namespace. This creates `rx<subdevice>`, private D-Bus, private Avahi, and `nqptp`.
6. Start Shairport Sync in the receiver namespace.
7. Start that zone's OwnTone process in `shiri_ot`.
8. Wait for the OwnTone API through `10.211.0.2:<zone_port>`.
9. Start the host-side mixer.
10. Restore saved speaker selection by name first, then ID.

This lets multiple zones run at once without port collisions:

- Shairport Sync instances have separate namespaces, IPs, mDNS identities, RTSP ports, UDP ranges, and `nqptp` daemons.
- OwnTone instances share one sender namespace and one `airptpd`, but use separate HTTP/WebSocket/MPD ports and separate runtime DB/cache dirs.
- The host mixer instances are separate host processes, each bound to a different ALSA loopback capture subdevice and pipe.

### MAC and DHCP Policy

Shiri uses deterministic locally-administered macvlan MAC addresses:

- `shiri_ot` sender MAC is derived from `sender:owntone`.
- Receiver MACs are derived from `receiver:<zone_id>`.

That keeps the router seeing the same Shiri devices after every restart instead of a new random device each time. Startup also verifies DHCP plus gateway ping from each namespace; if DHCP works but ARP/unicast is broken, Shiri fails startup loudly instead of entering the half-working "speakers disappeared" state.

DHCP lease and pid files use AppArmor-allowed paths:

- leases: `/var/lib/dhcp/dhclient-shiri-<hash>.leases`
- pids: `/run/dhclient-shiri-<hash>.pid`

DHCP is intentionally run with Shiri's minimal namespace script at `/etc/dhcp/dhclient-script`, copied from `scripts/dhclient_namespace.sh`. The stock Ubuntu `dhclient-script` updates host DNS/time/network hooks, which is noisy and can disturb the VM while configuring namespace-only links. Shiri's script only sets the namespace interface address and default route.

Do not run `dhclient -r` for Shiri test interfaces unless you are certain they are macvlan identities. Releasing a DHCP lease from an ipvlan interface can release/confuse the host-MAC lease because ipvlan shares the VM MAC.

### Why Not Host Mode

Host mode is not the clean AirPlay 2 layout here. Shairport Sync AP2 input needs `nqptp`; OwnTone AP2 output needs `airptpd`; both want the PTP event/general ports in their own network namespace. Running both host-level timing daemons conflicts. Running receiver and sender timing in separate namespaces avoids that conflict.

### Why Not ipvlan

Do not switch this to ipvlan on this VM. ipvlan L2 shares the VM's host MAC, and testing showed it can request another DHCP lease on the same MAC as the host. After the router/VM reboot, the host held `192.168.1.188` while an ipvlan probe was handed `192.168.1.189` using the same MAC. That can confuse a consumer router's MAC/IP/ARP lease table and is the suspected cause of the earlier state where DHCP still worked but ARP replies to the Shiri namespace stopped.

ipvlan also failed before the reboot in the way that matters: it got a DHCP lease, but ARP to the router and Sonos stayed `INCOMPLETE`, so no unicast traffic worked. Shiri's production path is macvlan only.

### Router/VM Failure Mode We Saw

The bad state looked like this:

- host `enp0s1` could ping the router and Sonos.
- macvlan/ipvlan namespaces could get DHCP leases.
- namespace ARP to `192.168.1.1` and `192.168.1.241` got no replies.
- OwnTone could not reliably discover/select speakers because its AirPlay connection test could not reach the speaker.

A router and VM reboot cleared it. The likely trigger was repeated experimental network identities, especially ipvlan DHCP on the host MAC and random macvlan MAC churn. The code now avoids both by using only stable macvlan MACs and by deleting namespaces/clients without sending dangerous ipvlan DHCP releases.

Healthy startup logs should include lines like:

```text
Created macvlan otlan0 in shiri_ot on enp0s1 with stable MAC ...
LAN preflight OK for shiri_ot/otlan0 at ... via 192.168.1.1
Created macvlan rx0 in shiri_rx_<zone> on enp0s1 with stable MAC ...
LAN preflight OK for shiri_rx_<zone>/rx0 at ... via 192.168.1.1
```

If startup fails with `LAN preflight failed`, do not debug OwnTone first. Fix the VM/router bridge path first, because the namespace has DHCP but cannot do unicast LAN traffic.

### Current Known-Good Example

These IPs are DHCP leases and can change, but this is the working shape observed after the router/VM reboot on May 18, 2026:

```text
host enp0s1:              192.168.1.188
shiri_ot/otlan0:          192.168.1.140
shiri_rx_b18972bb/rx0:    192.168.1.149
shiri_rx_fa7facc7/rx1:    192.168.1.235
Living Room speaker:      192.168.1.241
```

`liv` restored `Living Room` as `AirPlay 2` / `alac`. `bathhhh` restored OwnTone `Local Output` as `ALSA` / `pcm`.

## Latency Policy

OwnTone uses `start_buffer_ms = 500` in `templates/owntone.conf`. This is the main startup-buffer tuning point for OwnTone's speaker output.

Shairport Sync's `audio_backend_latency_offset_in_seconds` is not a pipeline delay workaround. It is clamped to +/- 0.25 seconds and defaults to `0.0`, matching the Shairport Sync docs guidance for small hardware compensation only.

Shairport Sync's backend buffer is set to `0.1` seconds and the Shiri mixer uses 10 ms buffers with 30 ms mixer latency. These are small bridge costs; the remaining user-perceived delay mostly comes from the AirPlay 2 source's buffered stream and OwnTone's AirPlay 2 output buffer. The Living Room AirPlay 2 output rejected `150ms` because its minimum latency is `250ms`; `500ms` is the current stable low-latency setting.

OwnTone pipe input is explicitly configured as `pipe_sample_rate = 48000` and `pipe_bits_per_sample = 16`, matching Shairport Sync and the mixer. OwnTone 29.2 defaults pipe input to 44.1 kHz unless this is set.

The play-start reset hook and pause mute bridge have been removed. They were sender-buffer workarounds and caused OwnTone FIFO interruptions, false mutes, volume jumps, and mixer backpressure in the AirPlay 2 pipeline.

Speaker selection allows real AirPlay 2 outputs and OwnTone's `Local Output` / `ALSA` output. The ALSA output is needed for local devices such as the VM's Bluetooth speaker path. Shiri still excludes its own virtual AirPlay receivers (`liv`, `bathhhh`, etc.) so OwnTone cannot accidentally select Shiri as a speaker and create a loop.

## Runtime Files

Generated runtime data lives under `/var/lib/shiri`:

- `/var/lib/shiri/config.json`: persisted zones, rooms, speaker choices, and volumes.
- `/var/lib/shiri/groups/<zone>/config`: generated Shairport/OwnTone/mixer configs.
- `/var/lib/shiri/groups/<zone>/logs`: per-zone logs.
- `/var/lib/shiri/groups/<zone>/pipes/audio.pipe`: mixed PCM into OwnTone.
- `/var/lib/shiri/owntone-sender/state`: shared OwnTone sender namespace state.

Important per-zone state files:

- `receiver_netns.txt`, `receiver_iface.txt`, `shairport_ip.txt`: Shairport receiver namespace state.
- `owntone_api_ip.txt`, `owntone_bridge_ip.txt`, `owntone_port.txt`: OwnTone API and LAN sender state.
- `mixer.pid`, `shairport.pid`, `owntone.pid`, `nqptp.pid`: process ownership for clean shutdown.

Generated zone `config`, `state`, and `logs` directories are cleared on daemon startup so removed hook scripts cannot linger.

## Operations

Start Shiri:

```bash
sudo /home/ubuntu/Shiri/scripts/shiri_service.sh start
sudo /home/ubuntu/Shiri/scripts/shiri_service.sh stop
sudo /home/ubuntu/Shiri/scripts/shiri_service.sh restart
sudo /home/ubuntu/Shiri/scripts/shiri_service.sh status
sudo /home/ubuntu/Shiri/scripts/shiri_service.sh cleanup
```

Use `cleanup` only when Shiri is stopped or wedged. It kills Shiri-owned daemons and deletes `shiri_*` namespaces.

Check the live stack:

```bash
ps -eo pid,ppid,args | rg 'app.py|shairport-sync|owntone|nqptp|airptpd|audio_mixer'
sudo ip netns list
curl -s http://10.211.0.2:3869/api/outputs | jq '.outputs[] | {name,type,selected,format,volume}'
```

Check namespace addresses and LAN reachability:

```bash
sudo ip -n shiri_ot -4 addr show dev otlan0
sudo ip -n shiri_rx_b18972bb -4 addr show dev rx0
sudo ip -n shiri_rx_fa7facc7 -4 addr show dev rx1

sudo ip netns exec shiri_ot ping -c 2 192.168.1.241
sudo ip netns exec shiri_rx_b18972bb ping -c 2 192.168.1.1
```

Check the expected speaker selections:

```bash
curl -s http://10.211.0.2:3869/api/outputs | jq '.outputs[] | {name,type,selected,format,volume}'
curl -s http://10.211.0.2:3879/api/outputs | jq '.outputs[] | {name,type,selected,format,volume}'
```

Expected examples:

```text
liv:     Living Room selected, type AirPlay 2, format alac
bathhhh: Local Output selected, type ALSA, format pcm
```

Important logs:

```bash
tail -f /var/lib/shiri/app.log
tail -f /var/lib/shiri/groups/zone_b18972bb/logs/shairport.log
tail -f /var/lib/shiri/groups/zone_b18972bb/logs/owntone.log
tail -f /var/lib/shiri/groups/zone_b18972bb/logs/owntone_wrapper.log
tail -f /var/lib/shiri/groups/zone_b18972bb/logs/mixer.log
```
