# Actual BlueALSA on a private test bus

`check_bluealsa_private_bus.py` is an opt-in manual Linux check of a maintained
BlueALSA binary. It never connects to the host system bus, pairs a device,
restarts a service or selects a physical speaker. Mock BlueZ supplies adapter
metadata and a local transport FD; the actual daemon exports PCM, runs its SBC
encoder, and processes its restricted controller commands.

Run as root under an external watchdog that terminates the whole process group
or systemd cgroup. Supply an independently recorded binary digest:

```sh
SHIRI_PRIVATE_BLUEALSA_TEST=1 python tests/linux/check_bluealsa_private_bus.py \
  --binary /path/to/maintained/bluealsad --expected-sha256 <recorded-sha256>
```

Stage `tests/native/private_bluealsa_exec.c` and the current Shiri Python
Bluetooth modules with the checker. It needs dbus-daemon, a C compiler,
dbus-next, and an existing unprivileged `nobody` account. The trusted binary must
be root-owned, unwritable by other users, free of set-ID bits, and compiled with
systemd support for `STATE_DIRECTORY`. No global storage fallback is accepted.

The fixture creates a private bus and storage directory. The daemon runs as
`nobody` with no supplementary groups. A test-only seccomp launcher admits Unix
sockets and denies Bluetooth, Internet and Netlink socket creation before exec;
it probes that restriction itself. The harness verifies the actual daemon's
UID, zero effective capabilities, NoNewPrivs, active seccomp and exact binary
path. Synthetic hci15 must be absent from the host.

Actual acceptance requires typed `SynchronousDrop` and `RestrictedController`
properties, exported `OpenRestricted`, real PCM pipe/controller descriptors,
rejection of legacy and prefix commands, real SBC/RTP output from synthetic
PCM, and completed `DropSync` within 200 ms. Independent cleanup steps close
worker/root FDs, release the exact lease, stop both owned daemons and remove the
private directory; absent uncreated resources count as clean.

This does not establish Bluetooth radio delivery, speaker-buffer retirement,
latency, drift, audible synchronization, multi-room physical operation or native
phone grouping. Portable tests check mock/provenance edges and import safety;
they are not a substitute for the actual daemon run.

## Recorded Linux result

On 2026-09-30 at 19:32:22–19:32:23 UTC, the check passed against maintained
BlueALSA `5.0.0-shiri-dropsync1`, binary SHA256
`ffa7d0d7ccf06e04699a3f98e149433a4eec2f51b014cee80f2ef12f69d66c32`.
The actual daemon exported typed capabilities and restricted PCM descriptors,
rejected all seven tested legacy/prefix commands, and encoded ten SBC/RTP packets
(4,385 bytes). Completed DropSync took 0.201 ms. The daemon retained UID 65534,
zero effective capabilities, NoNewPrivs and seccomp; all seven independent
cleanup checks passed. The checker SHA256 was
`0f966eb2bd993028645cffced173f1063da0565569d39cc305f8c8c921ca68eb`.

This proves the private daemon/descriptor/codec/controller path exercised here.
It does not prove the full broker/OwnTone production route, radio delivery,
retirement of already transmitted audio, physical latency or speaker sync.
