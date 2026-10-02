# Bluetooth output admission and final PCM

The new Bluetooth route is a descriptor-only output worker for one exact paired
A2DP speaker. It avoids sharing an ALSA Loopback playback node across rooms and
gives the worker no host D-Bus connection, ALSA device nodes, audio group, or
capabilities. The broker owns device admission and OwnTone owns final PCM gain
and presentation pacing.

This implementation is not yet an operational Bluetooth claim. The completed
BlueALSA flush path has passed an actual daemon test on a private bus; the full
broker/OwnTone route and paired physical acceptance remain separate gates.
Physical pairing, adapter setup,
USB passthrough, latency, drift and native phone grouping remain unverified by
these descriptor and protocol tests.

## Speaker-managed Bluetooth groups

The user requested compatibility with a Bluetooth-connected speaker group on
2026-10-01, without selecting a particular IKEA model. The transport uses the
exact paired main speaker's MAC as its one output identity. The same admission,
exclusive endpoint lease, source barriers and final music/TTS mix apply whether
that device plays alone or forwards audio to its own linked speakers. Do not
open an extra connection to each follower or infer separate routable rooms.

IKEA documents connecting the source device to one SOLSKYDD speaker, then
adding compatible speakers with their sound buttons. NATTBAD documents the
same main-speaker arrangement. This supports the design inference that Shiri
can feed the main endpoint, while IKEA controls the internal group. It does
not establish physical latency, jitter, synchronized rendering or an API for
querying group membership. [SOLSKYDD instructions](https://www.ikea.com/us/en/manuals/solskydd-bluetooth-speaker-beige__AA-2655914-2-100.pdf),
[NATTBAD instructions](https://www.ikea.com/us/en/manuals/nattbad-bluetooth-speaker-pink__AA-2554737-7-100.pdf).

Compatibility depends on the model and mode. NATTBAD's multi-speaker mode is
mono. KALLSUP specifies grouping only with other KALLSUP speakers, with no
stereo mode; do not assume every IKEA Bluetooth speaker shares one grouping
protocol. [NATTBAD specifications](https://www.ikea.com/us/en/p/nattbad-bluetooth-speaker-black-40601603/),
[KALLSUP specifications](https://www.ikea.com/us/en/p/kallsup-portable-bluetooth-speaker-yellow-green-50605120/).

All members of a linked group receive its zone's announcements. Separate room
targeting requires separate independently connected endpoints. Calibration must
cover the complete main-speaker/group path and be repeated after a membership,
stereo/mono mode or firmware change. Radio range, reconnect after power loss,
main-speaker loss, group persistence and member timing remain physical acceptance
checks, deferred by the user. No precise timing guarantee is inferred from
the product's multi-speaker feature.

The user explicitly excluded Bluetooth phone input on 2026-10-01. This design
covers Bluetooth speaker outputs and externally linked speaker groups.

## Why the prior route is limited

The previous host bridge writes through `hw:Loopback,1,<room-slot>` and captures
the reverse side for BlueALSA. ALSA subdevices share a character device node.
The broker reserves that entire playback node, so it correctly rejects a second
simultaneous BlueALSA room using the same card/device even with a different
subdevice. The replacement removes this shared node from the Bluetooth route.

Workers also need distinct UIDs. A same-UID process can access another worker's
filesystem through `/proc/<pid>/root` subject to the kernel's credential check;
a hidden host-bus path in one process is insufficient if the other has that
bus exposed. [Linux proc root access](https://man7.org/linux/man-pages/man5/proc_pid_root.5.html)
The descriptor worker receives neither that host-bus mount nor the other
process's UID.

## Exact device and descriptor ownership

`shiri.runtime.bluealsa.BlueALSA.admit(device, room_id, generation)` accepts
only an explicit `bluealsa:DEV=MAC,PROFILE=a2dp` endpoint. The root broker
reserves its normalized MAC before awaiting discovery. It verifies the service's
unique D-Bus owner and trusted UID, selects exactly one matching connected
A2DP-source/sink PCM, and records its adapter path, connection Sequence and
typed format/rate/channel properties. Ambiguous adapters and changed PCM
instances fail admission; no first-match or default device is used.

The maintained daemon's `PCM1.OpenRestricted` returns a writable PCM pipe and a
per-PCM SEQPACKET control socket limited to the exact `DropSync` command. The
broker validates both real descriptor types, then rechecks owner and PCM identity
after admission. It requires typed `SynchronousDrop=true` and
`RestrictedController=true` PCM properties before and after OpenRestricted;
missing, false and non-boolean capabilities reject admission. Stock `Open` is
never used for a room worker. The maintained method preserves its descriptor
signature and ownership lifecycle.
[Pinned BlueALSA Open implementation](https://github.com/arkq/bluez-alsa/blob/1a84465dd860d1be9dcf62339c6273e9e0632dd2/src/bluealsa-dbus.c#L463-L550)
It retains root copies and the exclusive MAC lease until the exact owning worker
unit is verified stopped. Its D-Bus reply handler also closes descriptors from
late, canceled or wrong-owner replies and incomplete messages at shutdown.

`HandoffServer` admits one exact kernel PID/UID once through a root-protected
socket. The request must match the room and launch generation. The response
passes just the validated pipe/controller pair with `SCM_RIGHTS`; there is no
bus address or arbitrary device URI in worker frames. The worker duplicates its
received pair and closes its received originals after construction. Failure
closes the worker copies while the broker keeps ownership until verified unit
stop. A daemon owner, PCM Sequence or capability change fails the lease closed.

## Final PCM transport

The maintained OwnTone `shiri-pcm` output connects to the private bridge listener
and remains the owner of final cubic volume and clock scheduling. The broker
pins that listener's inode, moves the socket into a root-only publication
directory, and exposes only that socket to the OwnTone output UID. The listener
survives the rename. The worker's root-only authorization operation fixes the
exact OwnTone MainPID before admitting its peer; the worker never removes the
broker's published path.

`shiri.runtime.pcm_transport` defines a 112-byte network-order `SHRIOUT1` header.
Every packet carries room UUID, launch nonce, stream nonce, generation and
sequence. DATA additionally carries negotiated format/rate/channels,
MONOTONIC presentation time, complete frame count and contiguous first-frame
index. Each payload is at most 16,384 bytes. START/READY and FLUSH/END/DROP_ACK
echo exact identities and capabilities; malformed, truncated or stale packets
fail the worker closed. Signed 16-bit, packed 24-bit, sign-extended 24-bit in
32-bit containers and signed 32-bit little-endian samples have explicit widths.

OwnTone preserves the native presentation anchor while accounting for its real
resampler's converted-frame deficit and each bounded native clock correction.
The bridge validates exact converted frame indices and both adjacent timestamp
cadence and drift from the first DATA timestamp. Their declared bounds are
3 ms plus 500 ppm of the corresponding converted duration plus 100 microseconds
for the producer's independently limited 200 ms converter backlog, with one
nanosecond for integer rounding. The producer validates native clock corrections
and converter backlog separately; a per-packet clock step cannot accumulate
without violating the origin bound. Warmup does not invent a receive-time origin.
The bridge introduces no second scheduler or sample conversion.
Its user queue plus pending kernel pipe bytes is bounded to 200 ms. Pipe writes
retain a partial tail, and each packet retains its original bounded deadline.
An absent reader or stalled pipe fails closed rather than accumulating audio.
TTS uses ordinary DATA: it never flushes the program or replaces its source.

## Required completion barrier

Stock BlueALSA's `Drop` command does **not** provide a completed flush barrier.
The controller returns `OK` after `ba_transport_pcm_drop()` enqueues a signal;
the audio thread clears the pipe and rewinds its raw buffer later. This ordering
can let an immediate successor generation enter the pipe and then be discarded
by the delayed old flush.
[Controller acknowledgment](https://github.com/arkq/bluez-alsa/blob/1a84465dd860d1be9dcf62339c6273e9e0632dd2/src/bluealsa-dbus.c#L430-L433),
[queued Drop signal](https://github.com/arkq/bluez-alsa/blob/1a84465dd860d1be9dcf62339c6273e9e0632dd2/src/ba-transport-pcm.c#L487-L502),
[later pipe/buffer clearing](https://github.com/arkq/bluez-alsa/blob/1a84465dd860d1be9dcf62339c6273e9e0632dd2/src/io.c#L353-L362).

The Python admission now rejects stock endpoints before opening PCM. It preserves
both capabilities in the protected descriptor receipt and the relay requires
actual boolean true values before inspecting or duplicating descriptors.
The relay cancels and joins old-generation writes, then sends only `DropSync`
and requires its exact `OK` completion within 200 ms. It has no fallback to
`Drop`. A missing, late, canceled or malformed completion fails the worker
closed and never produces a final-output DROP_ACK.

The restricted controller is also an isolation boundary. Stock controller
commands include `Drain`, which can wait synchronously for device consumption
inside the daemon's common D-Bus main loop. Handing that full controller to an
isolated worker would let it stall unrelated Bluetooth rooms. `OpenRestricted`
rejects Drain, legacy Drop, Pause, Resume, prefixes, empty requests and unknown
commands before entering any legacy handler. It accepts only exact `DropSync`,
whose completion has a bounded deadline. The capability describes the maintained
method; it does not certify a receipt created through stock Open.

The maintained daemon extension acknowledges that command only after the
selected PCM pipe and codec-side raw buffer have been cleared, with completion
tokens so a late request cannot acknowledge a successor. Its current capability
is restricted to SBC A2DP-source PCM. The actual maintained daemon has passed
the isolated check below; full production integration remains a separate gate.
Fake controllers explicitly emulate this completed flush; they cannot prove the
daemon implementation. A fixed sleep
or an empty pipe observation cannot prove codec-buffer retirement. Already
transmitted Bluetooth/device audio also requires separately measured physical
acceptance.

## Verification scope

Portable tests cover typed endpoint selection, wire vectors, contiguous frame
and presentation counts, malformed packet rejection, bounded pipe forwarding,
partial tails, canceled controller operations and descriptor reply cleanup.
Linux-only tests exercise real SEQPACKET/SCM_RIGHTS credentials and descriptors,
complete worker sessions, EOF/cancellation cleanup, and socket rename behavior.
They use real local pipes, not paired speakers. Run:

```sh
uv run --extra test pytest -q tests/test_bluealsa.py tests/test_pcm_transport.py \
  tests/test_bluetooth_output.py tests/test_bluetooth_output_lifecycle.py
```

On 2026-09-30 at 19:32:22–19:32:23 UTC, an actual Linux daemon test passed
against maintained BlueALSA `5.0.0-shiri-dropsync1` (binary SHA256
`ffa7d0d7ccf06e04699a3f98e149433a4eec2f51b014cee80f2ef12f69d66c32`).
On a private D-Bus with mock BlueZ metadata and a local transport FD, the real
daemon exported typed capabilities and restricted PCM descriptors, rejected
seven tested legacy/prefix commands, and encoded ten SBC/RTP packets (4,385
bytes). Completed DropSync took 0.201 ms. It ran as UID 65534 with zero effective
capabilities, NoNewPrivs and seccomp; all seven cleanup checks passed. The mock
provided no BlueALSA PCM or capability replies. See the
[manual check and scope](../tests/linux/README_bluealsa_private.md).

That result establishes the daemon/descriptor/codec/controller path exercised
by the fixture. It does not establish radio delivery, final physical sound,
retirement of already transmitted audio, multi-room speaker independence,
native phone grouping or the full broker/OwnTone route.

The worker session tests inject the expected final-output PID/UID to avoid
creating system accounts; the separate handoff tests check actual kernel peer
credentials. Full production integration must additionally verify the distinct
static bridge account, absence of host bus/device access, durable MAC lease
retention through cancellation/recovery, actual maintained OwnTone frames,
completed daemon flush ordering, multi-room independence and physical latency.
