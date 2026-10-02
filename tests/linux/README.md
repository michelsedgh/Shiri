# Manual Linux candidate validation

## Receiver startup ownership

`check_receiver_readiness.py` is a manual Linux root proof of the production
receiver readiness gate. It is not collected by pytest or run in CI. Prepare
the stopped, empty `b265eb7d` candidate and its provisioned version-2 identity
map first. The trusted core is staged at
`/opt/shiri-v2-privilege-review-v2-src`; its runtime sources and receiver observer
must remain root-owned and immutable to managed users.

Run under the actual broker capability boundary, with host mount propagation:

```sh
sudo systemd-run --unit=shiri-v2-receiver-readiness-check --property=Type=exec \
  --property=RuntimeMaxSec=120 --property=TimeoutStopSec=15 --property=KillMode=control-group \
  --property='CapabilityBoundingSet=CAP_SYS_ADMIN CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE CAP_SYS_NICE CAP_KILL CAP_DAC_OVERRIDE CAP_CHOWN CAP_FOWNER CAP_FSETID CAP_SETUID CAP_SETGID' \
  --setenv=PYTHONPATH=/opt/shiri-v2-privilege-review-v2-src \
  /opt/shiri-v2/.venv/bin/python \
  /opt/shiri-v2-privilege-review-v2-src/tests/linux/check_receiver_readiness.py
```

The fixture refuses `CAP_SYS_PTRACE`, missing production capabilities and extra
capabilities. It takes the real candidate broker lock and creates only one
isolated namespace with an internal dummy interface and `192.0.2.91` TEST-NET
address. No host interface, DHCP, LAN connection, PTP, audio or backend is used.
Real transient services run as `shiri-receiver-7` with zero capabilities. A
genuine MainPID-owned IPv4 listener at port 7000 must pass on the wildcard and
the namespace address. A separate same-UID child owning that listener, a
loopback-bound listener, an idle process, an exiting MainPID and a mismatched
held namespace inode must fail at the intended ownership gate. No process,
socket, namespace, credential, invocation or cgroup mechanism is mocked.

Reports, exact unit logs and the fixture's separate durable namespace intent
remain root-private under `/var/lib/shiri-v2-receiver-readiness-review`.
Success requires exact cgroup termination, verified owned namespace removal,
an empty candidate manifest, and unchanged host network, legacy PID 2444,
DHCP clients and Loopback slots 0/2. A failed cgroup stop retains the namespace.
After a watchdog kill, preserve both the candidate process manifest and this
fixture's namespace intent for exact recovery; independently managed services
can outlive the harness. On 2026-09-30 at 21:19:42–21:19:44 UTC,
`readiness-review5` passed all seven real-kernel listener vectors, exact cleanup
and host/legacy preservation. The broker's effective, permitted and bounding
sets were `0xa034fb`, with zero inheritable/ambient capabilities and no
`CAP_SYS_PTRACE`. Finite kernel lease lifetimes are compared against the
measured observation interval with one-second quantization; every other
identity and configuration field remains exact. Raw before/after observations
are retained in the protected report. This establishes the observer admission
boundary, not real Shairport, stock-phone or group readiness.

## Bluetooth bridge socket publication

`check_bluetooth_publication.py` is a manual Linux root test of the version-5
bridge boundary. It is not collected by pytest or run in CI. It requires the
stopped, empty `b265eb7d` candidate, its provisioned version-2 50-account map at
`/etc/shiri-v2-test/daemon-identities.json`, trusted staged Python sources and
the reviewed helper at `/opt/shiri-v2-next4-deps/libexec/shiri-bind-policy`.
It takes the candidate's actual broker lock and refuses another installation,
active ownership or missing legacy-preservation targets. It provisions no
accounts or services and performs no network, Bluetooth or ALSA setup.

Run only after those manual prerequisites are prepared, under an outer watchdog:

```sh
sudo systemd-run --unit=shiri-v2-bluetooth-publication-check --property=Type=exec \
  --property=RuntimeMaxSec=120 --property=TimeoutStopSec=15 --property=KillMode=control-group \
  --property='CapabilityBoundingSet=CAP_SYS_ADMIN CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE CAP_SYS_NICE CAP_KILL CAP_DAC_OVERRIDE CAP_CHOWN CAP_FOWNER CAP_FSETID CAP_SETUID CAP_SETGID' \
  --setenv=PYTHONPATH=/opt/shiri-v2-privilege-review-v2-src \
  /opt/shiri-v2/.venv/bin/python \
  /opt/shiri-v2-privilege-review-v2-src/tests/linux/check_bluetooth_publication.py
```

The harness launches an actual bridge service as `shiri-bridge-7` and a gated
consumer as `shiri-output-7`. It verifies their invocation/cgroup authority,
zero capabilities, restricted bridge address families and denied host-bus,
device, credential and peer-mount access. Root captures the listener through
`O_PATH`, records its inode before publication, moves it to a protected
directory and binds only that socket read-only to the consumer. Both peers'
real `SO_PEERCRED` must match the exact MainPID/UID/GID. A new listener placed
at the producer's former path must not redirect the consumer. Consumer chmod
and unlink attempts must fail. No kernel credential or publication mechanism
is mocked.

The report and unit logs remain root-private under
`/var/lib/shiri-v2-bluetooth-publication-review`; `last-result.json` points to
the latest observations. Success requires exact consumer-before-producer
termination, removal of the protected published inode, an empty ownership
manifest, and unchanged legacy process, DHCP clients, slots 0/2 and host network.
A failed consumer stop retains its producer/publication. A killed watchdog can
leave independently managed units; preserve their manifest and recover exact
owned resources before retrying. On 2026-09-30 at 21:20:10–21:20:11 UTC,
`publication-review4` passed all eleven checks, including publication,
read-only socket-only mounts, exact peer credentials, protected cleanup and
host/legacy preservation. The actual bridge and output UIDs differed, all five
worker capability sets were zero, and `NoNewPrivileges` was enabled. The broker
used the reviewed `0xa034fb` capability set, including broker-only `CAP_FOWNER`
and `CAP_FSETID`. Earlier publication-review2 failed
before daemon execution because the fixture redundantly masked `/dev/snd`
inside `PrivateDevices`; the corrected fixture retains the real control-node
open-denial probes and changes no worker policy. This proof uses a synthetic
socket producer and consumer. It does not test BlueALSA admission, a live
controller, codec output, Bluetooth hardware or physical timing.

## Installed candidate services

`check_candidate_services.py` is a manual Linux root harness. It is not collected
by normal pytest and is not run by CI. It validates actual systemd services,
namespace/DHCP ownership and the rootless API against staged candidate binaries.

Prepare the Linux machine manually before running it:

- Stage the candidate source, unit templates and audio dependencies at
  `/opt/shiri-v2`, with Python at `/opt/shiri-v2/.venv/bin/python`.
- Build the pinned backends into `/opt/shiri-v2-deps`; install the dedicated
  namespace DHCP hook and its AppArmor permission.
- Provide systemd, an eligible bridged LAN interface and an ALSA Loopback card
  with at least eight substreams. **Slot 7 must be free.** The harness selects no
  house speakers.
- Keep `/var/lib/shiri-v2-test-runtime` intact between runs. Its installation
  identity and stable MACs are reused; existing owned resources must be cleared
  through their owning runtime before this harness can start.

Run from the staged candidate checkout:

```sh
sudo /opt/shiri-v2/.venv/bin/python /opt/shiri-v2/tests/linux/check_candidate_services.py
```

The paths and service names are deliberately explicit. The harness creates only
`shiri-v2-test-runtime.service` and `shiri-v2-test-api.service`, uses API port 18082
on localhost, and stores its configuration/token under `/etc/shiri-v2-test`. It
creates the non-login `shiri` account if needed. It archives the disposable API
database under `/var/lib/shiri-v2-test-api/archive` before each run; it preserves
the runtime installation identity and token.

The test creates eight disabled rooms to reserve slot 7 for its one enabled
room. It checks authentication, durable rootless database writes, revision
conflicts, real backend startup, host-visible namespace mount/inode ownership,
closed auxiliary OwnTone listeners and denied anonymous LAN control. It sends
SIGKILL only to the verified main process of the exact managed test runtime unit
and verifies automatic broker recovery. Finally, it disables/deletes all test
rooms and stops/disables only the test units.

Results are written to `/tmp/shiri-v2-service-check-result.json`, with service
logs at `/tmp/shiri-v2-test-*.service.validation.log`. The report checks an empty
ownership manifest, no remaining owned namespace/control links, closed slot-7
endpoints, and exact host link/address baseline restoration. Tokens are redacted.
Exit status 0 means all checks and final cleanup passed; inspect the report and
logs when it exits nonzero.

This harness passed on the Ubuntu candidate in 29.2 seconds on September 30,
2026. It does not test a stock phone, native grouped playback, physical speakers,
Bluetooth hardware, Cast input or completed daemon privilege separation.

## Installation trust and state-adoption boundaries

`check_installation_boundaries.py` is a separate manual 64-bit Linux root
check. It creates one randomly named disposable tree under `/opt` and removes
that exact tree on exit. It invokes no apt/build command, changes no systemd
unit, and accesses no production application state.

```sh
sudo /usr/bin/python3 -I /opt/shiri-v2/tests/linux/check_installation_boundaries.py --source /opt/shiri-v2
```

It verifies actual root-owned custom prefixes and normal venv links, refusal
of writable/foreign-owned/linked executable paths, Linux OFD lock persistence,
retained SQLite companion preservation, hardlink/symlink refusal, an idle
connection in another process, exact quiescent database ownership/mode changes,
and exclusion of concurrent service installers. JSON results are printed to
stdout; no token is generated or printed. This checks the adoption helpers;
the complete installer sequence on a fresh host remains a separate gate.
All 16 checks passed on the Ubuntu candidate in 0.11 seconds on September 30,
2026; the retained report is `/tmp/shiri-v2-installation-boundaries.log`.

## DHCP address renewal

`check_dhcp_renewal.py` is a separate manual Linux root check. It creates a fresh
isolated namespace with a dummy interface, then verifies same-IP subnet changes,
changed-IP/subnet replacement and unchanged renewals. It preserves an unrelated
test address and verifies the host network baseline after exact-inode cleanup.
It obtains no LAN lease and does not test a DHCP server's lease table.

```sh
sudo /opt/shiri-v2/.venv/bin/python /opt/shiri-v2/tests/linux/check_dhcp_renewal.py
```

The candidate hook defaults to the same checkout. `--hook /path/to/hook.py`
selects another explicit hook; `--before-hook /path/to/previous-hook.py`
optionally reproduces the former stale-prefix bug first. Results are written
atomically to `/tmp/shiri-v2-dhcp-renewal-result.json`. The kernel reproduction
and all three corrected transitions passed on September 30, 2026; test
namespace removal and exact host network preservation passed too.

## Synthetic AirPlay input and direct-worker speech

`check_airplay_input.py` and `check_airplay_tts.py` are separate manual root
harnesses, not pytest tests or CI jobs. Run them one at a time after stopping
the dedicated candidate test services. Both preserve the existing candidate
installation identity, require an empty ownership manifest at
`/var/lib/shiri-v2-test-runtime`, and reject identities outside the known
`b265...` candidate. They require Linux root, pinned backends at
`/opt/shiri-v2-deps`, the staged candidate Python environment, wired interface
`enp0s1`, and all four Loopback slot-7 endpoints closed. They select no house
outputs. These fixed paths and guards are intentional; do not aim them at a
production installation.

```sh
sudo /opt/shiri-v2/.venv/bin/python /opt/shiri-v2/tests/linux/check_airplay_input.py
sudo /opt/shiri-v2/.venv/bin/python /opt/shiri-v2/tests/linux/check_airplay_tts.py
```

The input harness generates a private 440 Hz WAV and starts an owned synthetic
OwnTone sender. It discovers and selects only the candidate AirPlay 2 receiver
whose name and MAC-derived output ID match, then observes real Shairport →
Loopback slot 7 → mixer FIFO audio and music-start/stop hooks. The source must
keep playing, retain its exact output selection and advance program progress.
This harness passed in 34.7 seconds on 2026-09-30.

The TTS harness adds a real paced 880 Hz Opus WebRTC source directly through
the room AudioWorker RPC. It waits for actual sustained 440 Hz FIFO PCM before
taking its baseline; a `play` state or music hook alone cannot pass that gate.
It checks music ducking, decoded speech, restoration while the same connected
session sends silence, and bounded healthy close with no replay tail. Source
state/progress/queue identity, exact AirPlay selection and owned PID/birth
identities are sampled throughout the onset wait, negotiation and speech
stages. It sets and reads back source volume 20 after output selection.

The real Ubuntu TTS run passed in 32.5 seconds between 08:25:20 and 08:25:52 UTC
on 2026-09-30. Music amplitude ratios were 0.200005 while ducked, 1.000000 after
silence and 1.000000 after close; decoded 880 Hz speech amplitude was 423.0.
First qualifying music was observed 0.986 seconds after control-play
observation; the sustained gate passed at 1.259 seconds. Source progress
advanced from 10 to 8,200 ms, owned process identities stayed unchanged, and
reported FIFO drops remained zero. All seven final cleanup checks passed.

Reports are `/tmp/shiri-v2-airplay-result.json` and
`/tmp/shiri-v2-airplay-tts-result.json`. Private source configs, logs, WAV and
observed PCM remain in mode-0700 directories beneath the dedicated runtime
state directory; generated credentials and speech SDP are not report fields.
The harnesses stop their owned sender and broker, verify an empty manifest,
closed slot and exact host networking baseline, and include failure/cleanup
details in the report. The TTS check also closes its speech session and peer.

The TTS route bypasses the rootless API and broker speech routing: that broker
correctly rejects speech when no output is assigned. Neither harness
proves stock-phone interoperability, native multi-zone grouping, Cast input,
physical-speaker output, API/broker TTS routing or acoustic synchronization.
OwnTone may consume some FIFO writes concurrently, so observed blocks do not
establish full-rate delivery. Continuity observations are control samples at
roughly 100 ms in the TTS harness, not proof about shorter interruptions.

## Authenticated API speech and final local output

`check_airplay_api_tts.py` exercises the actual rootless HTTP API → broker →
room worker → selected OwnTone ALSA output. It requires the same known
`b265...` candidate, empty ownership manifest, Linux root, pinned backends,
`enp0s1` and four free slot-7 endpoints described above, plus the existing
nonroot `shiri` account. Stage `check_airplay_tts.py` alongside it. Stop the
dedicated candidate test services and run only one harness at a time.

Run the entire harness and its children under a transient systemd watchdog:

```sh
sudo systemd-run --wait --pipe --collect --unit=shiri-v2-api-tts-check \
  --property=RuntimeMaxSec=240 --property=TimeoutStopSec=10 \
  --property=KillMode=control-group \
  /opt/shiri-v2/.venv/bin/python /opt/shiri-v2/tests/linux/check_airplay_api_tts.py
```

The watchdog bounds the transient service cgroup, including a potentially
blocked GStreamer shutdown thread that Python cannot forcibly cancel. A
watchdog expiry is a failed run; inspect the exact owned manifest/resources and
recover them through their owning runtime before retrying. It does not prove
driver shutdown latency. Normal success still requires all explicit cleanup
checks, not merely a successful supervisor exit.

The harness seeds seven disabled reservations and one fixed-identity slot-7
room in a disposable database. Its API child runs as `shiri` with no effective
capabilities; anonymous state reads must return 401, then a temporary token
creates an HttpOnly session. Authenticated API calls enable the room, assign
only local ALSA output ID 0, change its volume and admit/close real Opus speech.
Disabled offers, an unknown Nobly binding and wrong-room session close are
rejected. A positive external Nobly connector is not exercised.

The synthetic sender selects only the exact AirPlay 2 receiver by name and
MAC-derived ID. Input uses `hw:Loopback,0,7` → mixer capture on pair 1; OwnTone's
selected local output uses `hw:Loopback,1,7` → final capture on pair 0. The
observer never competes for the mixer FIFO. It records the actual negotiated
PCM rate/format and requires consecutive frame offsets, rejects later
discontinuities or observer losses, and checks 440 Hz music in every complete
post-onset block. Actual final-PCM transition gates replace assumed buffering
delays. API volume 100 → 50 → 100 must follow the pinned cubic software-volume
curve, then 880 Hz speech must duck music to 0.2, restore during same-session
silence, resume audibly and restore again after closing the audible session.
Source/output state, queue identity, progress, volume, selection and owned
PID/birth identities are sampled throughout.

The Ubuntu run passed in 33.4 seconds, 10:07:05–10:07:39 UTC on 2026-09-30.
API UID was 999 with zero effective capabilities. Measured volume-50 music ratio
was 0.124934 (expected 0.125), restored volume-100 ratio was 1.000000, speech
duck ratio was 0.199999, and music ratios after silence/close were 1.000000.
Final capture verified 882,240 stereo S16LE frames at 48 kHz across 919
consecutive buffers; only the initial buffer was discontinuous, maximum
callback gap was 25.5 ms, and observer/FIFO drops were zero. All ten cleanup
checks passed, including disposable room deletion, closed slot and exact host
network baseline restoration.

The private report is `/tmp/shiri-v2-airplay-api-tts-result.json`; source config,
temporary login token, API logs, WAV and observed PCM stay in their private
candidate directories. No credentials or SDP belong in repository artifacts.
Offline regressions in `tests/test_airplay_observation.py` run in the existing
audio-enabled CI job and require no Linux hardware or live app.

This verifies a synthetic AirPlay source and kernel Loopback output. It does
not prove stock-phone interoperability, native multi-zone grouping, Cast input,
physical/Bluetooth speaker behavior, acoustic sync or completed daemon privilege
separation. Control polling cannot exclude transients shorter than its roughly
100 ms interval, and block fits cannot exclude sub-block audio transients.

## Gated PCM and exact crash recovery

`check_pcm_guard.py` passed on the isolated Ubuntu candidate at 15:17:03 UTC
on 2026-09-30. It uses the actual profile-v4 service launcher, root-installed
socket helper and inherited PCM filter. It checks all eight managed HTTP ports,
IPv4 UDP and IPv6 bind rules, exact invocation release, non-root credentials,
direct/conversion PCM identity, wrong-device manifests and forbidden control
ioctls. Only virtual Loopback DEV1/sub7 receives 2,400 silent frames per mode.
The peer PCM node stays denied. Results include exact unit policy and helper
digests in `/tmp/shiri-v2-pcm-guard-result.json`; all owned services and slot-7
streams were closed afterward. This is kernel/device evidence, not acoustics.

`check_unit_recovery.py` is a separate manual root check with no audio or network
creation. Under an external watchdog it kills its disposable broker owner before
socket-policy attachment, after attachment but before release, and after release.
It verifies persistent kernel enforcement and exact recovery from the durable
manifest, including launch-gate removal and live log-reader cleanup. It requires
the same empty candidate and trusted source path. Its fresh result is
`/tmp/shiri-v2-unit-recovery-result.json`; preserve failure evidence before retrying.

## Two-zone native timing and exact-zone speech

`run_native_grouping.py` supervises `check_native_grouping.py` inside a fresh,
disconnected parent network namespace for the isolated `b265...` candidate.
The independent digital timing baseline has passed on Linux; the complete
group and speech check remains under validation. The harness keeps the
admitted kernel socket policy and local PCM launch gates intact. It needs
`/opt/shiri-v2-next8-deps`, `/etc/shiri-v2-test/daemon-identities.json`, an empty
ownership manifest, the existing `shiri` API account and all four free Loopback
sub-7 endpoints and `dnsmasq-base`. Run it alone, with the other observation
harnesses, `loopback_capture_probe.py`, `isolated_group_lan.py`,
`group_failure_evidence.py` and the supervisor
staged at the same trusted location:

```sh
sudo systemd-run --wait --pipe --collect --unit=shiri-v2-native-group-check \
  --property=RuntimeMaxSec=400 --property=TimeoutStopSec=15 \
  --property=KillMode=control-group \
  /opt/shiri-v2/.venv/bin/python /opt/shiri-v2/tests/linux/run_native_grouping.py
```

Two fixed rooms occupy logical slots 6 and 7. Their enrolled virtual outputs
use distinct Loopback playback nodes, DEV 1/sub 7 and DEV 0/sub 7; observers read
the opposite capture directions. Input arrives through the actual private native
socket from tracked system-manager units running as each room's exact receiver
UID. The synthetic producers declare one group and common presentation timeline,
with deliberately different arrival leads and measured RAW/MONOTONIC brackets.
A known amplitude code is measured independently at both final OwnTone outputs,
using a common GStreamer clock. Correlation must identify the same program and
the measured relative offset and drift must each stay within 2 ms on a 1 ms
analysis grid. Output displacement relative to the declared stimulus calendar is
checked against an independent kernel-digital baseline. Before enabling either
room, finite fixtures run in canonical filtered output-UID units and write a
six-second code directly through the pinned virtual PCM. Their real MONOTONIC
kernel status timestamps, queue delay and submitted frame counts establish the
presentation origin. The same capture pipeline then measures its offset from
that reference. Every retained anchor, raw PCM and absolute capture timestamp is
reported; startup/tail silence is outside the finite measurement window.
The native virtual capture explicitly negotiates its declared 48 kHz stereo
S16 format before playback opens, so an unconstrained observer cannot lock the
shared Loopback pair at ALSA's default rate. This fixture setting does not
change physical output negotiation or resample the observed PCM.

The virtual DHCP gateway and receiver networks have no physical uplink. The
supervisor enters only its held network namespace, preserving the host mounts
needed to verify cgroup v2. Test units launched by the system manager explicitly
join the tracked parent or their generated child namespace. The original host
snapshot and legacy process identities are checked before and after the test.
The parent is removed only after the candidate manifest, namespace PIDs and
virtual interfaces are empty; replacements and foreign resources are preserved
for exact recovery.

The absolute-horizon allowance is declared before the grouped program:
baseline queue-origin spread + negotiated ALSA period duration + largest query
bracket + 2 ms analysis-grid allowance. Query brackets above 1 ms, incomplete
fixtures, changed playback triggers or origin dispersion above two periods
(plus query uncertainty) fail the baseline. The group check subtracts only the
independently measured capture offset; missing the resulting horizon fails the
run even when the two outputs are mutually synchronized. No speaker profile is
modified and these kernel/digital timestamps do not measure physical acoustics.
The fixture ignores only snd-aloop's uninitialized zero timestamp within two
initial negotiated periods; it never uses those values as clock anchors. Its
nonblocking drain is confirmed from refreshed kernel SETUP state. A short
window crossing a known amplitude-code edge may project onto the 880 Hz fit;
the fixture applies a stimulus-derived worst-case projection bound, independent
of captured values. The later constant-music speech-leakage checks keep their
measured low noise floor. Fixtures use independently tracked disposable owner
UUIDs, so normal disabled-room reconciliation remains active. Their protected
API paths live outside `/tmp`, preserving daemon `PrivateTmp` isolation.

The idle real receivers are stopped with their exact owned-unit proofs, then
synthetic producers take the canonical `room:shairport` reservations. Broker
health sampling pauses only during this pre-evidence replacement and resumes
before the first PCM packet; launch/security gates remain active. Production
manifest loading after a crash can therefore recognize and recover those units.

Authenticated rootless API speech through the exact Nobly binding must reach
only room A. Every complete post-onset PCM buffer must retain music; after the
constant baseline, each room-B buffer must retain its original music amplitude
within 5% and exclude measurable 880 Hz voice. Room A must duck to 0.2, restore
during silence, resume voice and restore after close. Exact frame offsets, PTS,
later discontinuities, buffer drops, output selection, queue identity, progress
and owned process identities remain guarded. A later explicit source takeover
must replace A's 440 Hz probe with 660 Hz and reject effects from its retired
session. A's END must release ownership while B's original observer continues.
The intentional A source cutover is separate from the uninterrupted group/TTS
interval.

Results will be `/tmp/shiri-v2-native-grouping-result.json`; private final PCM,
timestamp lists and logs remain in the disposable candidate directory. Success
requires explicit owned-unit, peer, capture, API, manifest and slot cleanup,
plus preservation of legacy PID 2444, slots 0/2 and host networking. The outer
watchdog bounds the harness and API/capture children, while daemon units launched
by the system manager have independent cgroups. A killed parent can leave those
units: preserve the ownership manifest and recover its exact resources before
retrying. Receiver producers also have their own 90-second bound.

On failure, `group_failure_evidence.py` copies the exact producers' last status,
owned daemon log tails and historical journals before stopping producers or
deleting room directories. Journals require the admitted unit, invocation and
boot IDs together, including when the producer or worker has already exited.
Reused generated room/sender role log tails are explicitly historical and
unattributed (`invocation_scoped: false`); they can contain earlier test
invocations. Exact journal rows are separately marked `invocation_scoped: true`
and validated against all three IDs, so an old logfile error is not evidence of
a fault in the current run.
The mode-0600 `failure-evidence/diagnostics.json` survives room cleanup under the
run's private artifact directory; both inner and supervisor reports retain its
path and digest. It includes no configs, command payloads, credentials, SDP or
unrelated service logs. Collection has a six-second deadline and at most a
half-second concurrent child-reaping grace; each source is limited to 64 KiB,
the final artifact to 2 MiB and the scope to 15 exact room/sender units. Partial
capture failures are reported separately and do not replace the original PCM
failure or skip required cleanup. Portable regressions cover these boundaries;
the manual Linux replay must verify the journal/artifact path on the candidate.

Offline tests in `tests/test_native_group_observation.py` exercise lost frames,
late discontinuities, ambiguous/wrong codes, excessive measured offsets and
short silence, duck or speech leakage hidden by stage medians, canonical receiver
restart recovery and unreliable kernel queue evidence. These prove
observer sensitivity without opening any kernel PCM device or native socket.
The manual harness is a synthetic protocol and digital Loopback check. It does
not establish stock-phone interoperability, real iPhone grouping, physical
speaker/acoustic timing, Bluetooth or Chromecast input. Buffer fits cannot
exclude shorter sub-buffer transients; control samples do not prove behavior
between polls.

## Per-zone abrupt fault isolation

`run_native_zone_faults.py` is a separate opt-in mode of the guarded two-zone
fixture. Stage it, `native_zone_faults.py` and the current grouping dependencies
at one trusted root-owned location. It requires the same stopped, empty
`b265...` candidate, provisioned daemon identities and four free Loopback sub-7
endpoints; its current pinned backend prefix is `/opt/shiri-v2-next9-deps`.
Run it alone under the whole-process watchdog:

```sh
sudo systemd-run --wait --pipe --collect --unit=shiri-v2-native-zone-faults \
  --property=RuntimeMaxSec=400 --property=TimeoutStopSec=15 \
  --property=KillMode=control-group \
  /opt/shiri-v2/.venv/bin/python /opt/shiri-v2/tests/linux/run_native_zone_faults.py
```

After the normal group/speech proof, the test sends SIGKILL to room A's exact
OwnTone MainPID, waits for the actual broker health monitor to heal A, reconnects
a fresh synthetic receiver, then repeats with A's audio-worker MainPID. Signal
admission holds the process pidfd and exact kernel cgroup; boot, invocation,
reservation, process birth and cgroup membership must still match while frozen.
The test signals only that held pidfd, retains the production ownership record,
and thaws the held cgroup on success, failure or cancellation. It does not stop
the unit or change saved room intent to simulate the crash.

An independent sticky observer of B starts before the original all-room
observer is joined. B retains its original capture, shared clock, sender,
service invocations, music owner/generation, selected output, item, volume and
program-progress origin through both faults and the later takeover/END tests.
Each actual PCM buffer must retain the original music gain within 5%, exclude
A's speech and preserve validated frame offsets/caps/discontinuity receipts.
Final observer shutdown checks buffered PCM and cannot erase a late failure.

A's old stream is deliberately lost during room recovery. The production
monitor runs throughout crash and autoheal; it pauses only during the exact
synthetic receiver replacement, and resumes before the fresh producer's run
command. B's independent observer remains active throughout this explicit
preparation interval. Historical A PCM and fresh stream identities are retained
separately. This proves neither phone auto-resume nor uninterrupted original A
audio; a full broker restart also remains outside the B-preservation claim.

Each recovery has a 25-second acceptance deadline, the fault phase a 60-second
deadline, each producer a 180-second lifetime and each observer a 48 MiB limit.
The supervisor bounds its child to 280 seconds; the external 400-second watchdog
also bounds capture/API threads. Independently managed daemon units still
require exact manifest recovery after a killed supervisor. The inner report is
`/tmp/shiri-v2-native-zone-faults-result.json`; the supervisor report is
`/tmp/shiri-v2-native-zone-faults-supervisor-result.json`. Acceptance requires
both injected faults, fresh ready A instances/streams, continuous original B
evidence, exact cleanup and unchanged host/legacy resources.

Portable tests exercise the actual orchestration with explicitly simulated
service recovery and real generated PCM/continuity guards, plus stale-invocation,
wrong-room, held-inode, cancellation, buffered-failure and clock-progress
refusals. A Linux-only test signals and reaps only its own direct child through
a real pidfd. This mode awaits a serial candidate Linux run; it provides no
physical speaker, stock-phone grouping, Bluetooth or Cast-input evidence.

## Per-zone digital latency matrix

`run_native_latency_probe.py` is a separate opt-in mode of the guarded two-zone
fixture. Stage its exact source, `native_latency_probe.py` and the coherent
grouping dependencies at one trusted root-owned location. It requires the same
idle disposable candidate, empty ownership manifest, provisioned identities and
free Loopback sub-7 endpoints. Run it alone under a whole-process watchdog:

```sh
sudo systemd-run --wait --pipe --collect --unit=shiri-v2-native-latency-probe \
  --property=RuntimeMaxSec=600 --property=TimeoutStopSec=15 \
  --property=KillMode=control-group \
  /opt/shiri-v2/.venv/bin/python /opt/shiri-v2/tests/linux/run_native_latency_probe.py
```

Each immutable run declares one common native horizon. The production horizon
remains four seconds. A separate, explicitly staged three-second experiment
uses the same assertions; it does not change the production setting or establish
a universally safe minimum. The supervisor names and hashes the actual source
files before and after execution. Its source receipt supplements the external
reviewed staging manifest; cached bytecode is not the admitted source.

After the normal two-zone group/speech proof, nine rows measure room A at
output offsets -2000, 0 and +2000 ms: cold idle speech, warm idle speech on the
same real Opus connection, and cold native program playback. Offset changes
require A disabled, its owned units stopped, the exact playback endpoint closed
and matching saved/backend offset readbacks. A separate kernel-digital capture
baseline is measured with that playback endpoint free before each profile; it
does not include or subtract the configured speaker correction.

Idle rows report the first actually emitted coded Opus marker through final
digital output, including encoding, transport, decoding and mixer cadence.
They do not assign an invented timestamp to the internal mixer. Native rows
compare the observed coded program with its declared presentation time plus
the horizon and configured offset. Their absolute acceptance bound remains the
independently measured queue-origin spread, negotiated ALSA period, query
bracket and 2 ms analysis-grid allowance. Absolute errors and their bounds are
retained numerically. The initial coded two-zone receipt retains the existing
2 ms relative alignment gate; matrix rows preserve B's original constant
carrier and do not claim a new row-by-row grouped-alignment measurement.

An independent sticky observer of B starts before the original control
observer is joined. It preserves B's source/generation, service invocations,
sender, selection, item, volume, cumulative program progress and every PCM
buffer through the matrix and the later takeover/END checks. All queued A and
B PCM is checked before shutdown and after proven GStreamer NULL. The latter
check excludes only intentional callback stoppage from the live-age check;
recorded callback gaps, framing, sample offsets and content remain guarded.
Cold/warm silence requires every sample in both channels within four counts of
zero. A is restored to offset zero with a fresh exact synthetic receiver before
the established source-transition tail.

One separate adverse row delivers cold native PCM 1950 ms after its declared
presentation, with offset zero. A qualified rejection requires both actual
native mapping admission and the exact current OwnTone invocation's initial
presentation-anchor rejection. Missing audio alone cannot establish rejection.
This distinguishes source-lead admission from cold-start scheduling limits.

The matrix phase is bounded to 300 seconds, producers to 480 seconds, each
capture to 128 MiB and control evidence to 10000 records. The supervisor bounds
its child to 480 seconds; the external 600-second watchdog also bounds capture
and API threads. Independently managed daemon units still require exact
manifest recovery after a killed supervisor. Reports are
`/tmp/shiri-v2-native-latency-probe-result.json` and
`/tmp/shiri-v2-native-latency-probe-supervisor-result.json`; row placeholders,
measurements and failed tail receipts survive partial failure.

Portable regressions cover coded-marker admission, exact stopped-room offset
changes, independent calendar corrections, late queued failures, slow healthy
NULL transitions and DC/noise masquerading as silence. The matrix awaits its
serial candidate Linux run. Its scope is virtual digital playback; it proves
no physical/acoustic delay, native phone behavior, Bluetooth or Cast input.

## Private Bluetooth production route

`run_native_bluetooth_route.py` extends the same guarded, idle candidate
fixture with the real production broker and descriptor-only Bluetooth worker,
maintained OwnTone framed output, private BlueALSA daemon and real SBC encoding
and decoding. It requires the known candidate installation and legacy baseline,
empty ownership, provisioned identities and free Loopback endpoints. Its private
D-Bus/BlueZ mock supplies the exact selected MAC; it accesses no host system
bus, radio or physical speaker. Run it alone from the coherent root-owned test
tree under an external 400-second process watchdog:

```sh
sudo systemd-run --wait --pipe --collect --unit=shiri-v2-native-bluetooth-route \
  --property=RuntimeMaxSec=400 --property=TimeoutStopSec=15 \
  --property=KillMode=control-group \
  /usr/bin/env SHIRI_PRIVATE_BLUETOOTH_ROUTE_TEST=1 \
  /opt/shiri-v2/.venv/bin/python /opt/shiri-v2/tests/linux/run_native_bluetooth_route.py \
  --binary /opt/shiri-v2-bluealsa/bin/bluealsad \
  --expected-sha256 VERIFIED_BLUEALSA_BINARY_SHA256
```

Use the independently recorded digest of the actual admitted binary. The test
checks cubic volume, targeted speech duck/silence/restore, source takeover and
END while preserving B's original source, progress and PCM observer. SBC uses
explicit lossy signal bounds; RTP continuity and decoded frames establish the
software transport, not physical presentation timing. Final success requires
verified encoder stop and complete packet draining to real EOF before final
audio checks and retention. Cancelled drains, CRC failures and audible retired
tails cannot pass. Reports are
`/tmp/shiri-v2-native-bluetooth-route-result.json` and
`/tmp/shiri-v2-native-bluetooth-route-supervisor-result.json`.

The original late-shutdown false acceptance was reproduced and corrected.
Independent portable checks passed 68 cases with two platform skips; full
serial Linux route execution remains pending. This fixture does not establish
paired-radio playback, speaker latency, stock-phone grouping or Cast input.
