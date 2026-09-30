# Required daemon privilege migration

Status: **required production gate, not implemented**. The API currently runs as
`shiri`, but the candidate broker launches network-facing backends and audio
workers as UID 0. The broker's systemd restrictions do not turn those children
into unprivileged services. Passing the current lifecycle and crash tests does
not close this gate.

This plan preserves OwnTone, Shairport and NQPTP timing. It changes service
credentials, access boundaries and lifecycle authority; it adds no speaker clock
or scheduler. Native Cast input remains a separate unresolved product gate.

## Credentials and provisioning

Provision static, non-login accounts during privileged installation, before any
room starts. Do not run `useradd`, choose arbitrary numeric UIDs or accept a user
name supplied through the API during reconciliation.

| Identity | Scope | Permitted resources |
| --- | --- | --- |
| `root` broker | Installation | Ownership manifest, DHCP, namespace setup, systemd management |
| `shiri` API | Administration | API database and authenticated broker control socket |
| `shiri-room-0` through `shiri-room-7` | One reserved room slot each | Its network backends and their private state |
| `shiri-audio-0` through `shiri-audio-7` | One reserved room slot each | Trusted mixing, TTS decoder and approved local output bridge |
| `shiri-discovery` | Private D-Bus helpers | Explicit helper sockets and bus configuration only |
| existing `avahi` account | Avahi helpers | Private Avahi runtime mounts only |
| `shiri-timing` | Shared sender PTP helper | Sender PTP sockets and sender timing shared memory only |

Audio accounts are distinct from both the API and network backend accounts. Do
not add the API to `audio`, room groups or hook groups. Installation must validate
an existing account's UID, primary group, non-login shell and managed purpose;
an unexpected preexisting identity is an installation error. Save a root-owned
UID/GID map alongside the installation identity and validate it at preflight.
Changing an account mapping requires all its owned services to be stopped first.

Accounts are provisioned once. Room files, socket groups and service definitions
are created lazily for the exact UUID only after the broker reserves its slot.
A slot account cannot be reused while its previous room's processes or IPC
resources remain owned. Generated files are never writable by the API account.

## Launch through owned systemd services

The root setup broker must remain in the host mount namespace so its owned
`/run/netns` bind mounts survive a service replacement. The first real systemd
test reached a running room but exposed plain namespace placeholder files to
the host: filesystem sandboxing had confined the actual mounts to the old
broker's private mount namespace. `MountFlags=shared` does not rejoin the host
peer group after systemd first changes propagation to slave. The current broker
unit therefore omits filesystem sandbox directives and explicitly leaves
`PrivateMounts=no`; capability/address-family limits remain. The rootless API
keeps its filesystem sandbox and child launchers keep private timing/Avahi mount
views. The installed candidate's real Linux service check now verifies
host-visible namespace inodes and SIGKILL/autorestart recovery with exact host
baseline restoration. This is not evidence of completed daemon confinement.

Use a transient service for each daemon, with a validated command and properties
created by the root broker. A name includes installation identity, room UUID,
backend role and a new launch generation; never adopt a service by prefix alone.
Persist the intended unit before launch and its actual invocation ID after
launch. Retain failed launch reservations until rollback verifies termination.

Systemd performs `NetworkNamespacePath=` and the private bind mounts before
executing with `User=`/`Group=`. This removes the current need for a root
`unshare -> shell -> ip netns exec -> daemon` chain. The systemd 249 documentation
defines these namespace and mount settings, including the fact that POSIX shared
memory needs mount isolation rather than `PrivateIPC=` alone.
[systemd execution properties](https://github.com/systemd/systemd/blob/v249/man/systemd.exec.xml)

Common worker policy: `NoNewPrivileges=yes`, empty capability bounding and
ambient sets, `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`,
`ProtectControlGroups=yes`, a finite task/memory limit, bounded stop deadline,
`KillMode=control-group`, and only explicitly required address families. Worker
units cannot write their unit/configuration/executable paths or manage systemd.
No shell command is accepted from a room definition. Existing verified binary
pins remain mandatory.

Use `BindReadOnlyPaths=` for generated configuration and
`BindPaths=` only for that daemon's writable state and approved IPC. Hide the
host system D-Bus socket from every network backend. The host local-output bridge
gets the BlueALSA bus only when its device is explicitly configured. It receives
no broker management capability.

## Files, traversal, IPC and shared memory

Keep `/var/lib/shiri-runtime` root-owned 0700. Its manifest, phone-volume queue,
UID map and secrets must not become room-readable merely to make existing
absolute command paths work. A UID drop alone would currently fail because a
worker cannot traverse that parent directory.

Create daemon-specific source directories below the root-private room record,
then let systemd bind the required directories to fixed worker-visible paths
such as `/run/shiri-room/config`, `/run/shiri-room/state` and
`/run/shiri-room/pipes`. Systemd can reach the root-private source; workers can
reach only the mounted view. Generate backend paths against that view, including
cache/database paths, audio FIFO, metadata FIFO and hook socket. Test every
ancestor's actual traversal permissions under the worker UID.

Configuration remains root-owned, read-only to the intended daemon's group.
OwnTone database/cache state belongs to its room account; the mixer state and
control socket belong to its audio account. Broker-captured logs remain
root-owned and rotated. Give the producer and consumer access to each shared
FIFO through an explicit per-slot IPC group; do not use world-write permissions
or a group shared across all rooms. Keep Shairport metadata outside OwnTone's
watched audio directory and preserve the known-good enabled/private FIFO
configuration until the pinned metadata-disabled crash is fixed upstream.

Receiver NQPTP and Shairport must see the same per-room `/dev/shm` bind mount.
Shared sender airptpd and the OwnTone processes must see the same sender timing
shared memory, with readers unable to modify the helper's state. Separate helper
writes from readers with appropriate ownership and read-only mount views; prove
the pinned OwnTone shared-memory access mode before enforcing that view.

Run private D-Bus as an unprivileged helper. Replace the current root wildcard
policy with explicit peers: Avahi alone owns `org.freedesktop.Avahi`, and the
corresponding receiver or sender clients may call only the discovery interfaces
they need. The sender bus admits the configured room backend UIDs, not arbitrary
local users. Separate private bus runtime directories from room configuration
secrets. Avahi 0.8's `make_runtime_dir()` expects its runtime directory to belong
to the configured `avahi` UID/GID, even with `--no-drop-root`; provision that
ownership before starting it unprivileged.
[Avahi 0.8 startup](https://github.com/avahi/avahi/blob/v0.8/avahi-daemon/main.c#L1311)

## PTP capabilities and pinned-source constraints

Start network-input decoders and OwnTone without Linux capabilities. Set
OwnTone's `general.uid` to the intended room account and execute it with that
account already selected. Its pinned startup changes effective credentials when
launched as root; relying only on that internal change would retain a root
launch path and does not establish the required external boundary.
[OwnTone 29.3 startup](https://github.com/owntone/owntone-server/blob/29.3/src/main.c#L187)

Receiver NQPTP is a separate non-root service with only
`CAP_NET_BIND_SERVICE` to bind PTP UDP 319/320. Provide a finite memory-lock limit
large enough for its shared-memory mapping and a bounded real-time priority
limit for its requested FIFO priority 5. Its pinned Linux source attempts that
priority and maps the timing segment with `MAP_LOCKED`; a scheduling failure is
logged, but a failed shared-memory mapping is fatal. Do not hand
`CAP_SYS_NICE`, `CAP_IPC_LOCK`, `CAP_NET_ADMIN` or broker capabilities to
Shairport to compensate for untested service limits.
[NQPTP 1.2.8 startup](https://github.com/mikebrady/nqptp/blob/c925f27c1fd12e4033ac477e5a405969b0b0260b/nqptp.c#L178)

Shared airptpd uses the timing account and only `CAP_NET_BIND_SERVICE`. The
pinned foreground entry point binds the default PTP ports 319/320 and has no
root-UID admission check; its timing implementation creates a 0644 POSIX shared
memory segment. This supports a minimal capability trial, not a claim that the
complete unprivileged configuration has passed hardware testing.
[airptpd entry point](https://github.com/owntone/libairptp/blob/7e2252e0258525b3480b54b1906038fee230e981/daemon/airptpd.c),
[shared-memory implementation](https://github.com/owntone/libairptp/blob/7e2252e0258525b3480b54b1906038fee230e981/src/daemon.c#L87)

Keep DHCP in the root broker setup boundary. Its namespace-only hook now receives
the host namespace identity captured before namespace entry; it does not need
PID 1 namespace access or `CAP_SYS_PTRACE`. DHCP never edits host DNS, interfaces
or services. Broker setup currently needs `CAP_CHOWN` for owned helper paths;
that capability must not be inherited by the new worker units.

## Room-bound hooks and backend authentication

Keep the general broker control socket restricted to root and the API UID.
Provide a distinct per-room signal socket that accepts only that active room's
receiver UID and launch generation. Its schema permits bounded volume values
and `music-start`/`music-stop`; the handler derives the room from the socket/peer
binding rather than trusting a submitted UUID. Deny reconciliation, output
selection, diagnostics, speech offers and arbitrary commands on this socket.
Music signaling reaches a separate signal-only worker socket; audio health and
speech control remain on an audio-account-private control endpoint available to
the root broker. A receiver compromise cannot send speech to other rooms.

Derive a different OwnTone administrator secret for every room from a root-only
installation secret. Supply only that room's secret to its backend and the
broker client. Remove the current private-veth/namespace CIDR trust bypass,
including loopback trust if the backend treats it as implicitly authorized.
OwnTone's authorization code accepts trusted peers before checking the
administrator password; sharing a sender namespace makes the bypass material.
Verify unauthenticated requests fail from host, LAN and another room UID while
the broker's authenticated requests still work.
[OwnTone authorization](https://github.com/owntone/owntone-server/blob/29.3/src/httpd.c#L1290)

MPD, its artwork HTTP port and the separate notification websocket are unused
and now explicitly configured to zero. Preserve those closures. Check the
actual build's alternate `/ws` path separately. Do not assume disabling a
listener or protecting the JSON API authenticates every native protocol path.

## Recovery without broad ptrace privilege

The current durable process record uses boot ID, PID birth, executable and
command line. After a worker UID drop, the bounded broker may be unable to read
`/proc/<pid>/exe`: Linux applies a ptrace access check to that link. A new UID
policy must not silently turn that failure into "dead" or relax matching to
PID/name alone. Do not add `CAP_SYS_PTRACE` as a shortcut.
[Linux proc executable access](https://man7.org/linux/man-pages/man5/proc_pid_exe.5.html),
[ptrace access rules](https://man7.org/linux/man-pages/man2/ptrace.2.html)

Move worker lifecycle authority to systemd. Persist boot ID, unit name,
invocation ID, expected immutable ExecStart/User/namespace properties and control
group. Query and validate the system manager's record before stopping a unit;
a mismatched invocation or property leaves the reservation intact and reports
an inspection-required error. Stop only the exact owned unit and confirm its
control group has no processes before releasing speakers, slots or namespaces.
Systemd exposes transient-unit creation and unit properties through its manager
API. A unit name or PID alone is insufficient authority.
[systemd 249 manager API](https://github.com/systemd/systemd/blob/v249/man/org.freedesktop.systemd1.xml)

Exact process snapshots remain useful diagnostics where readable, but are no
longer the sole kill/recovery authority for unprivileged workers. Do not attempt
to signal a same-UID observer's report without separately proving its process
and pipe ownership; the transient-unit approach avoids introducing that extra
privileged identity helper. Recovery still validates owned namespace boot/inode,
MAC/type/parent/alias and owned DHCP records independently.

## ALSA isolation is an additional gate

Distinct room UIDs and device cgroups cannot isolate substreams 0..7 of the same
ALSA PCM character device. Giving a malicious receiver access to that Loopback
node can allow opening a different room's substream. Do not claim per-room PCM
privacy merely because each command names a different slot.

Test one Loopback card per room, permitting only that card's device nodes, or
replace receiver access with a trusted PCM front end that exposes only a
room-bound pipe/socket or validated preopened handle. Direct use of a passed
ALSA descriptor is not assumed compatible with Shairport's backend API. The
alternative must be tested for native timing preservation before selection.
Do not reload `snd-aloop` during candidate testing: the legacy application still
uses its current cards/slots. This requirement remains unresolved with the
current shared-card design.

## Implementation and acceptance sequence

Installer trust checks now validate root ownership, permissions and ancestors
throughout an existing executable tree before privileged pip/build/service
use and after installation. Prefix/ancestor symlinks are refused; interpreter
links may resolve to trusted regular system files. Arbitrary external directory
links, special files and set-ID executables are refused without repairing or
broadly changing their ownership.

Service-state adoption requires a stopped API and a database with no other
openers or SQLite companions. It holds a Linux OFD byte lock, reads only the
held main descriptor with immutable SQLite validation, and rechecks companions,
inode and metadata before changing that descriptor's owner. It deliberately
does not validate, delete or chown WAL/SHM/journal files: an operator must first
preserve and checkpoint them as their known current owner. See
[../install/README.md](../install/README.md). Portable failure regressions cover
these boundaries; complete fresh-host installation remains a separate gate.

1. Provision/validate the static identities and read-only mapping, with candidate
   paths kept separate and no default-service activation.
2. Add a systemd runner and unit ownership/recovery tests before switching any
   daemon. Prove worker UID/GID, empty capabilities and inaccessible broker
   manifest/API token under the actual hardened test services.
3. Move sender helpers, then one disabled test room's receiver helpers, then
   OwnTone/Shairport, and finally its mixer/local-output worker. Keep rollback
   and speaker/slot leases until exact unit termination is proven.
4. Verify dedicated hooks, per-room API authentication and private D-Bus policy
   using allowed and deliberately denied peers. Parse generated configurations
   with the pinned backend inside the owned test namespace; regex assertions
   alone did not catch the earlier libconfuse semicolon error.
5. Resolve the Loopback substream boundary, then repeat native AirPlay playback,
   TTS/ducking, offset readback, 50 start/stop cycles and SIGKILL/reboot recovery.
   Verify host and legacy resources remain unchanged and owned manifests clear.

No privilege gate is marked complete until those checks run on Linux with the
real pinned binaries. Physical Bluetooth, mixed-speaker timing, native grouped
AirPlay and mandatory Cast input need their own end-to-end acceptance evidence.
