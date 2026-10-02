# Required daemon privilege migration

Status: **required production gate, combined runtime validation pending**.
The new runtime requires fixed unprivileged daemon identities and launches
daemons through owned systemd transient services. It has no root-daemon fallback.
Portable tests cover account admission, unit replacement and exact cgroup
termination. Isolated Linux proofs now cover confined launch/termination,
filtered local PCM, receiver-listener ownership and socket publication.
Combined native PCM, room lifecycle and crash-recovery checks with the actual
backend/worker processes still must pass before this gate closes. Earlier
lifecycle/audio evidence used the previous privileged launch path and does not
establish this boundary.

This plan preserves OwnTone, Shairport and NQPTP timing. It changes service
credentials, access boundaries and lifecycle authority; it adds no speaker clock
or scheduler. Native Cast input is deferred by the user for this release.

## Credentials and provisioning

Provision static, non-login accounts during privileged installation, before any
room starts. Do not run `useradd`, choose arbitrary numeric UIDs or accept a user
name supplied through the API during reconciliation.

| Identity | Scope | Permitted resources |
| --- | --- | --- |
| `root` broker | Installation | Ownership manifest, DHCP, namespace setup, systemd management |
| `shiri` API | Administration | API database and authenticated broker control socket |
| `shiri-receiver-0` through `shiri-receiver-7` | One reserved slot each | Shairport, its native PCM socket and private receiver discovery/timing views |
| `shiri-output-0` through `shiri-output-7` | One reserved slot each | OwnTone state and expressly admitted local PCM nodes or one published bridge socket |
| `shiri-bridge-0` through `shiri-bridge-7` | One reserved slot each | Exact broker-admitted Bluetooth descriptors, private state and its final-PCM listener |
| `shiri-audio-0` through `shiri-audio-7` | One reserved slot each | Mixing, TTS, its private IPC and its OwnTone credential |
| `shiri-discovery`, `shiri-discovery-0` through `shiri-discovery-7` | Sender or one receiver | Private D-Bus and patched Avahi runtime only |
| `shiri-timing`, `shiri-timing-0` through `shiri-timing-7` | Sender or one receiver | PTP sockets and its timing shared memory only |

Audio accounts are distinct from both the API and network backend accounts. Do
not add the API to `audio`, room groups or hook groups. Installation must validate
an existing account's UID, primary group, non-login shell and managed purpose;
an unexpected preexisting identity is an installation error. All 50 daemon
accounts are checked before missing accounts are created. Account purpose and
the mapping bind them to one installation UUID and runtime state directory;
another installation cannot adopt those same slot identities. Save a root-owned
UID/GID map alongside the installation identity and validate it at preflight.
Changing an account mapping requires all its owned services to be stopped first.
The version-2 map adds eight distinct bridge identities. Upgrading the former
42-account map requires the stopped, quiescent service-installer migration:
it preserves every existing UID/GID, verifies the installation and state/lock
bindings, then atomically publishes the new map. Runtime admission refuses the
old map rather than silently sharing an output UID with a bridge. Known old
unit policies remain available for exact recovery; new `local-output` launches
with the former host-bus profile are refused.

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

The root broker needs `CAP_FOWNER` alongside `CAP_CHOWN` to set modes on
admitted worker-owned directories, FIFOs, held PTP shared-memory records and
the held socket during publication. UID 0 with `CAP_DAC_OVERRIDE` can access
those objects but cannot change their mode after ownership passes to a worker.
Linux checks the inode owner or `CAP_FOWNER` for that operation.
[Linux 5.15 inode permission checks](https://github.com/torvalds/linux/blob/v5.15/fs/inode.c#L2054)
This capability remains on the root broker; input decoders, OwnTone, the mixer
and the Bluetooth bridge retain empty capability sets. PTP services receive
only their separately verified low-port binding capability. The broker does
not receive `CAP_SYS_PTRACE`.

The prepared native-input directory also needs its setgid bit retained so
audio-owned sockets inherit the receiver GID. The broker therefore needs
`CAP_FSETID` when it installs that directory's mode while outside the receiver
group. `CAP_FOWNER` alone does not stop Linux from silently removing setgid.
[Linux 5.15 mode admission](https://github.com/torvalds/linux/blob/v5.15/fs/attr.c#L116)
Directory preparation rechecks its exact inode, UID, GID and complete mode
after changing metadata; a stripped bit is an admission failure. `CAP_FSETID`
is limited to the root broker and is absent from all daemon worker sets.

Use a transient service for each daemon, with a validated command and properties
created by the root broker. A name includes installation identity, room UUID,
backend role and a new launch generation; never adopt a service by prefix alone.
Persist the intended unit before launch and its actual invocation ID after
launch. Retain failed launch reservations until rollback verifies termination.

Systemd performs `NetworkNamespacePath=` and the private bind mounts before
executing with `User=`/`Group=`. This removes the previous need for a root
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

The candidate VM proved that its systemd 249 build records `SocketBindDeny`
properties without attaching any bind BPF program: another room's HTTP port
remained bindable. New OwnTone launches therefore use a broker-owned map-free
cgroup bind program and a root-owned read-only pre-exec release gate. The broker
verifies the exact invocation, held cgroup inode, program instructions and local
and effective program IDs, then durably records that proof before releasing the
daemon. Persistent kernel attach references survive broker death. IPv4 UDP is
allowed, explicit IPv4 TCP binds are restricted to the room's HTTP port, and
IPv6 binds are denied. The sender namespace's automatic port range must exclude
all room HTTP ports because implicit `listen()` allocation bypasses the bind
hook. Combined kernel enforcement and crash-recovery evidence for this new
path remains required; a configured systemd property alone does not close it.
The standalone production C helper has passed 36 real Linux bind checks,
including all seven peer control ports, IPv6 refusal and persistent verification
after the caller changes UID. Those tests establish the helper's kernel behavior,
not yet the complete gated unit launch and broker-crash path.

Use `BindReadOnlyPaths=` for generated configuration and
`BindPaths=` only for that daemon's writable state and approved IPC. Hide the
host system D-Bus socket from every network backend. The version-5 Bluetooth
bridge also has no host bus, ALSA nodes, shared supplementary groups or network
namespace access; its allowed socket family is `AF_UNIX`, with empty capability
sets and `NoNewPrivileges`. Its UID differs from OwnTone's UID. Otherwise a
compromised output could reach the bridge's host-bus mount through its same-UID
`/proc/<pid>/root`, even when its own mount view hides that bus. Separate UIDs
and denied peer mount traversal are required, not merely distinct commands.

Only the root broker connects to the host BlueALSA bus. It admits an exact MAC,
A2DP output endpoint, unique service owner and negotiated format, and requires
typed completed-drop and restricted-controller capabilities. It opens the
restricted endpoint and passes the selected PCM and controller descriptors
once over credential-checked `SCM_RIGHTS`, bound to the bridge MainPID/UID,
room UUID and launch generation. It retains its admission until every exact
room unit has stopped. The bridge receives no authority to select another
device or invoke arbitrary bus methods. The maintained SBC daemon and real
linked codec check build successfully; live service/descriptor and paired-device
acceptance remain separate gates. See [BLUETOOTH_OUTPUT.md](BLUETOOTH_OUTPUT.md).

The bridge creates its final listener in private writable state. Root holds the
socket with `O_PATH`, persists its inode authority before publication, and
renames it into a root-only generation directory. It changes that held socket
to bridge-UID/output-GID 0660 and gives OwnTone a read-only bind of the socket
alone. The producer's writable directory is never mounted into OwnTone.
Published socket cleanup follows exact cgroup termination and refuses changed
inodes or unexpected files. The real-kernel version-5 socket-publication harness
(`publication-review4`) passed eleven checks on 2026-09-30 at
21:20:10–21:20:11 UTC under the final broker capability set `0xa034fb`: different bridge
and output UIDs, zero worker capabilities, real held-inode publication,
read-only socket-only mounts, reciprocal exact peer credentials, old-path
replacement refusal, denied peer-mount/bus/device/secret access, exact stops
and complete host/legacy restoration. Its producer/consumer are synthetic.
This establishes the Linux publication boundary, not combined Bluetooth
admission, codec output or radio timing. See the repeatable fixture in
[../tests/linux/README.md](../tests/linux/README.md).

## Files, traversal, IPC and shared memory

Keep `/var/lib/shiri-runtime` root-owned 0700. Its manifest, phone-volume queue,
UID map and secrets must not become room-readable merely to make existing
absolute command paths work. A UID drop alone would currently fail because a
worker cannot traverse that parent directory.

Create daemon-specific source directories below the root-private room record,
then let systemd bind the required directories to fixed worker-visible paths
such as `/run/shiri-worker/config`, `/run/shiri-worker/state` and
`/run/shiri-worker/pipes`. Systemd can reach the root-private source; workers can
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
secrets. Stock Avahi 0.8's `make_runtime_dir()` expects the configured `avahi`
UID/GID even with `--no-drop-root`. The pinned `0.8-shiri-user1` patch accepts
the actual invoking non-root UID/GID only in that explicit no-drop mode. It
preserves the original root/default behavior; each private runtime is prepared
for the corresponding discovery identity. Actual-source sanitizer tests cover
both branches and foreign ownership refusal.
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
audio UID and launch generation. Its sole operation is a native-volume event
with bounded volume, captured control revision, stable event ID and exact
source token/native generation. The handler derives the room from its binding.
Deny reconciliation, output
selection, diagnostics, speech offers and arbitrary commands on this socket.
Native music travels over a room-private credential-checked SEQPACKET socket;
the native profile has no shell music/volume hooks. Audio health and speech
control remain on an audio-account-private endpoint available to the root
broker. The control-intent operation independently requires kernel peer UID 0.
A receiver compromise cannot send speech to other rooms.

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

The previous durable process record used boot ID, PID birth, executable and
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
an inspection-required error. Hold the exact cgroup inode, freeze it, revalidate
invocation/properties, and capture pidfds before signalling. Revalidate before
final termination and use the held cgroup control descriptor, never a unit-name
kill. Thaw that held group on failure. Confirm it has no processes before
releasing speakers, slots or namespaces.
Systemd exposes transient-unit creation and unit properties through its manager
API. A unit name or PID alone is insufficient authority.
[systemd 249 manager API](https://github.com/systemd/systemd/blob/v249/man/org.freedesktop.systemd1.xml)

Exact process snapshots remain useful diagnostics where readable, but are no
longer the sole kill/recovery authority for unprivileged workers. Do not attempt
to signal a same-UID observer's report without separately proving its process
and pipe ownership; the transient-unit approach avoids introducing that extra
privileged identity helper. Recovery still validates owned namespace boot/inode,
MAC/type/parent/alias and owned DHCP records independently.

Receiver admission now waits for port 7000 to be owned by the exact Shairport
MainPID in its held receiver namespace before reporting the room running.
The trusted observer permanently adopts the receiver UID/GID, drops effective,
permitted, inheritable and ambient capabilities, sets `NoNewPrivileges` and
disables dumpability. Parent checks fence the actual invocation, birth, cgroup
and held namespace before and after observation. The isolated Linux proof
(`readiness-review5`) passed seven vectors on 2026-09-30 at
21:19:42–21:19:44 UTC under the final production broker capability set
`0xa034fb`, without `CAP_SYS_PTRACE`: wildcard/LAN listeners pass;
another same-UID process's listener, loopback binding, no listener, exit and a
changed namespace fail. Exact cleanup and host/legacy restoration also passed.
This observer proof uses a synthetic listener, not the complete real receiver
and native group playback path.

## Preserve service policy across manager reloads

A systemd manager reload must preserve the launched worker's namespace and
device restrictions. On the observed systemd 249 installation, transient
`RestrictNamespaces=yes` was written as an empty `RestrictNamespaces=` line.
Reloading that fragment changed the manager's typed mask from 0 to all allowed
namespaces. Shiri correctly refused the changed policy, retained ownership and
blocked control/recovery. This can leave physical workers running while phone
volume callbacks queue without reaching the saved room master.
[systemd transient serialization](https://github.com/systemd/systemd/blob/v249/src/core/dbus-execute.c#L1458),
[empty namespace-mask conversion](https://github.com/systemd/systemd/blob/v249/src/shared/nsflags.c#L50),
[fragment parsing](https://github.com/systemd/systemd/blob/v249/src/core/load-fragment.c#L3219).

New launches persist the exact intended companion path before starting the
service. After successful transient creation and strict original policy
verification, the broker durably records the invocation and held cgroup inode,
then seals this root-owned file before ordinary monitoring or daemon release:

```text
/run/systemd/system/<exact-unit>.d/90-shiri-namespace-policy.conf
```

```ini
[Service]
RestrictNamespaces=yes
```

The ownership receipt records boot, protected parent, directory/file inodes,
exact bytes and metadata through planned, directory, file and sealed phases.
Creating a drop-in before `StartTransientUnit` would prevent systemd's pristine
unit admission, so publication follows verified creation. Every subsequent
service check still requires the actual integer namespace mask 0 and the exact
receipt. Daemon cleanup precedes artifact cleanup. Interrupted creation can be
recovered from its durable unique-unit plan only when the original daemon is
proved stopped or absent and the protected path, bytes, modes and known inodes
match. Changed, foreign or linked artifacts refuse recovery; an old boot cannot
retarget a successor's artifact. See `shiri/runtime/namespace_policy.py`.

Retirement retries may accept an absent companion only for the same current-boot
inactive or failed invocation, typed zero main/control PIDs and its exact cgroup
proved empty or absent; present artifacts and every immutable policy check
remain mandatory. The isolated Ubuntu `unit-retire152` check passed two serial
non-media services with referenced inactive-unit repeated stops, recovery from
the unchanged durable reservation before forget, and an interrupted original
file unlink before directory removal. Its synchronous active-unit verifier
refused a missing companion, which was restored immediately to the same inode;
this was not an asynchronous stop-path test. All cleanup passed, with the
installed package and ledger unchanged. The original `unit-retire151` refusal
and `group149` cleanup failure remain preserved; installation of the complete
candidate and the grouped audio rerun remain separate pending gates.

The same reload also reverses the manager's `DeviceAllow` array: transient
admission prepends device entries, serialization keeps that order, and fragment
parsing prepends them again. Shiri compares the exact device/permission pairs
independent of array order, preserving their repetitions and checking both the
original observation and requested policy. Added nodes, wider permissions and
every other immutable property remain refused; historical receipts are never
rewritten. [Transient device setter](https://github.com/systemd/systemd/blob/v249/src/core/dbus-cgroup.c#L1473),
[device fragment parser](https://github.com/systemd/systemd/blob/v249/src/core/load-fragment.c#L3604),
[device-list insertion](https://github.com/systemd/systemd/blob/v249/src/core/cgroup.c#L566).

The real isolated Ubuntu `policy137` check passed two manager reloads with the
same service PID, invocation and held cgroup inode, actual namespace mask 0 and
unchanged device/permission pairs. Its unpatched comparison service reproduced
the all-allowed namespace mask and strict refusal. Both non-media sleep services
expired normally; exact cleanup passed and the installed map/ledger stayed
unchanged. The report is `/tmp/shiri-rvm-guest-co6loon8/policy137-poll.json`.
`policy135` remains failed on device-array ordering, with cleanup passed;
`policy136` remains failed because the system interpreter lacked `dbus_next`,
with its later exact cleanup separately retained. Those failed reports are not
relabelled. This establishes actual policy persistence and cleanup, not native
audio, phone controls or physical synchronization. Native protocol and actual
phone acceptance remain separate gates.

Existing units without a companion receipt still require their original full
ownership/policy checks. If their namespace mask has already changed, use a
reviewed exact-unit policy restoration followed by the qualified new manager's
ordinary owned retirement and coherent recreation. The new manager handles
historical device-array order without changing any saved device entitlement.
Do not rewrite the ownership ledger, relax mask checks, signal by prefix or
adopt a foreign drop-in. One-time operator repair artifacts need separate exact
receipts and cleanup. Restoring manager text does not apply a new seccomp filter
to an existing process or revive an already retired room control launch. See
[installation recovery](../install/README.md#fixed-worker-identities-and-upgrades).

## ALSA isolation is an additional gate

Distinct room UIDs and device cgroups cannot isolate substreams 0..7 of the same
ALSA PCM character device. Giving a malicious receiver access to that Loopback
node can allow opening a different room's substream. Do not claim per-room PCM
privacy merely because each command names a different slot.

The native profile removes ALSA access from Shairport and the mixer. Timed
music enters through the receiver-authenticated private socket, retaining
native presentation times. Its combined Linux timing/confinement proof remains
required. Local output receives only the exact playback character node; the
runtime rejects simultaneous routes sharing that node, even when SUBDEV differs.
The real libasound check found that even integer card configuration must open
the card control node to select a PCM subdevice. The candidate's filtered output
profile therefore admits that exact control node only with the recorded trusted
PCM launcher. Before executing a decoder it installs an inherited native-ABI
seccomp filter: card-control ioctls are denied except protocol-version query and
file-local preferred-subdevice selection. Mixer element/TLV operations,
compat/x32 execution and io_uring bypass paths are denied. The unit rejects an
unfiltered control grant, extra nodes or a mismatched PCM/control card. Actual
Linux checks have now opened both plain and plug PCM configurations on the
isolated virtual endpoint under the production filter, denied control mutations
and bypass attempts, verified wrong-identity refusal, and closed the endpoint
with exact unit cleanup. Full broker launch, native playback and recovery with
this profile remain separate combined gates; there is no unfiltered fallback.
Do not reload `snd-aloop` during candidate testing: the legacy application still
uses its current cards/slots. This requirement remains unresolved with the
legacy shared-card design. Giving a candidate local output access to a shared
Loopback node also exposes its other legacy substreams; that synthetic test
setup is not evidence of isolation from legacy audio on that card.
The descriptor-only Bluetooth route uses a framed final-PCM socket instead of
the old Loopback capture bridge. It therefore requests no Loopback, control
node or `audio` group access. This removes that shared-node requirement for
Bluetooth, without establishing physical timing or live daemon readiness.

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
   OwnTone/Shairport, and finally its mixer and distinct FD-only bridge. Keep rollback
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
AirPlay input needs its own end-to-end acceptance evidence. Future Cast input
must also pass receiver acceptance before it is enabled.
