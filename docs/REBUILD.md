# Clean rebuild review record

Recorded 2026-09-30. Every zone must expose native AirPlay 2 and Chromecast
input receivers, accepting existing phone casting controls without a Shiri
phone app. Each zone routes music and exact-zone TTS to its assigned mixed
speaker outputs. Native iPhone grouping must preserve timing through the
final speakers. [PRODUCT_REQUIREMENTS.md](PRODUCT_REQUIREMENTS.md) defines
the production goal. OwnTone remains the candidate speaker delivery backend;
its limitations must be addressed where they prevent the required behavior.
The user requested a full clean rebuild; legacy patching stopped when that
direction changed.

## Preservation and scope

The audited legacy code is preserved on `codex/robust-room-audio`, checkpoint
`a3ef0fc`. The clean implementation is on `codex/shiri-rebuild`, created from
the original main branch. The original Ubuntu VM installation was backed up
before rebuild deployment work. These are rollback/reference artifacts, not
evidence that the old running VM contained the newest Mac checkout.

Legacy configuration import is explicit, read-only by default and transactional
when applied. Source bytes are preserved; collisions fail; every imported room
is disabled. Unknown speaker protocols and local outputs without a physical
device require manual selection. No live networking or process state is
imported into the new ownership model.

The current candidate has independent room programs. Cast input, arbitration
between music inputs and verified native iPhone grouping at the final outputs
are missing required capabilities. These remain production gates; they are
not accepted exclusions. Nobly does not exist yet, so the candidate provides
its exact-zone speech boundary without a connected external client. Automated
microphone calibration is designed but not implemented. See
[ARCHITECTURE.md](ARCHITECTURE.md), [RECEIVER_RESEARCH.md](RECEIVER_RESEARCH.md)
and [CALIBRATION.md](CALIBRATION.md).

## Review loop

At the user's latest instruction, the current loop must finish all agreed
software and automated/VM tests before stopping. Native phone, microphone and
physical-speaker execution is deferred to a later acceptance phase. Remaining
implementation includes shared program timestamps through multiple zones,
calibration analysis and its administration interface, integrated source
ownership, a supported inbound Cast adapter and least-privilege daemon
execution. These are implementation tasks rather than substitutes for the
later physical evidence. The active goal remains open.

Each iteration follows the same completion rule:

1. Reproduce a concrete failure or define a measurable invariant.
2. Change the smallest responsible boundary, preserving room intent and owned
   resource identity.
3. Exercise a meaningful failure or concurrency test, plus relevant successful
   behavior.
4. Independently review ownership, timeouts, state transitions and truthful
   status reporting.
5. Record evidence and unresolved limits here. Do not promote a pending
   physical or Linux gate because a simulation passed.

## Accepted decisions and review corrections

| Area | Decision or correction | Verification scope |
| --- | --- | --- |
| Product/runtime boundary | Rootless typed API, separate privileged broker and per-room audio workers | API tests and import separation; daemons still retain UID0 and least-privilege execution is pending |
| Durable state | SQLite transactions, exclusive assignments, optimistic revisions, fail closed on unsupported/corrupt data | Concurrent writers, subprocess exits, actual SQLite full-storage rollback |
| Room routing | Exact Nobly binding, no silent destination fallback | Domain/store/API tests; external Nobly integration pending |
| Binding admission races | External Nobly binding admits offers under the edit guard; follow-ups require the returned stable room UUID | In-flight rebind, rebound close and lost acknowledgment tests use actual broker session ownership |
| Local/Bluetooth identity | Physical ALSA device distinguishes each local OwnTone ID `0` | Ownership and retained calibration tests; actual paired Bluetooth pending |
| Speech ownership | One explicit session per room, bounded negotiation and cancellation disposal | Real aiortc loopback and adversarial close/negotiation tests |
| Ducking | Audible media controls ducking; silent input cannot hold an unlimited session | Silence, idle expiry and attack/release tests; physical listening pending |
| FIFO behavior | Atomic complete-frame writes bounded by the platform's `PIPE_BUF`; drop instead of backlog | Missing/full/reconnecting reader tests and Linux GStreamer smoke test |
| Offset semantics | OwnTone per-output ±2000 ms, positive delay, acknowledgment/readback and session restart | Domain/profile tests and source verification; acoustic result pending |
| Recovery | Manifest-based resource ownership, process birth/command checks, namespace inode and MAC/alias checks | Independent failure tests and 2 real VM SIGKILL cases passed; broader crash matrix pending |
| Transport claims | Current OwnTone output limits documented; required native input/group behavior remains binding | Primary backend documentation; stock-phone input and real mixed-device matrix pending |

The offset review found that changing OwnTone's stored value during playback
does not change that active output session. The adapter uses the paused-output
restart behavior rather than treating a successful readback as live timing
proof. [OwnTone 29.3 implementation](https://github.com/owntone/owntone-server/blob/29.3/src/player.c#L2743-L2771)
This administrative calibration sequence must never be triggered by speech
offer, media, close, timeout or failure handling. TTS lowers music mix gain only
while its producer and timeline continue.

## Evidence recorded so far

| Check | Result | What it establishes |
| --- | --- | --- |
| Initial Mac Python suite | 93 tests passed | Typed API/domain/store and bounded audio/session behavior under the exercised cases |
| Latest completed Mac Python suite | 453 passed, 9 skipped in 13.68 seconds | Includes playback-converter, numeric-card and final-audio observation regressions; foundation snapshot before the next implementation phase |
| Latest completed full Ubuntu candidate Python suite | 459 passed, 3 skipped in 29.37 seconds | Refreshed at 10:48:27 UTC; real Linux audio and maintained OwnTone software-volume checks, converter/numeric-card and observation regressions |
| Pure source ownership policy | 32 tests passed | Newest-input grants, exact producer/overlay token and epoch fences, stale callbacks/queued effects and TTS gain-only behavior; no receiver/runtime integration |
| Audio tests | 20 passed, including real local aiortc Opus decode to mono PCM | Negotiation, one-session ownership, timeout/cancellation and media validation; no GI or speakers in the Mac run |
| Expanded domain/store tests | 146 passed | Strict inputs, transactional ownership/revisions, full-storage rollback, process-exit durability, read-only migration, configured device exclusivity, durable phone receipts and startup semantic/canonical audits |
| Independent runtime failure/configuration tests | 52 passed within the focused runtime/domain/store run | Boot/command identity, ownership handoff, cleanup retries, malformed manifests, untagged-link recovery, bounded HTTP, pinned settings, patched-backend preflight, converter-preserving local routes and numeric-card rejection before runtime effects |
| Web/client/browser suite | 25 passed in 1.654 seconds, including actual browser tests | Refreshed for the foundation checkpoint; exercised control UI and client behavior, no physical room playback |
| Ubuntu 22.04 GStreamer smoke | Passed | Real `audiomixer` oscillator to nonblocking FIFO at 48 kHz stereo S16 |
| Expanded real Linux audio suite | 4 tests passed | Real PCM/FIFO clocks, bounded absent/full readers, local Opus speech with continuous ducked music, and reverse Loopback bridge shutdown/reopen; no paired Bluetooth or house output |
| Ubuntu 22.04 room lifecycle | 50/50 enable/disable cycles passed, then clean shutdown | Real namespace/DHCP/daemon/ALSA-slot startup and teardown, with no speakers selected |
| Ubuntu VM SIGKILL recovery | 2 tests passed in 25.88 seconds | Owned broker/daemon/network recovery in the exercised phases; recorded in `/tmp/shiri-v2-crash-tests.json` and `/tmp/shiri-v2-crash-tests.log` |
| Ubuntu synthetic AirPlay 2 input | Passed in 34.7 seconds | OwnTone source → exact candidate Shairport identity → actual Loopback slot 7 → mixer FIFO, including music-start/stop hooks; no stock phone, physical outputs or group-sync claim |
| Ubuntu AirPlay plus direct-worker Opus speech | Passed in 32.5 seconds | Actual AirPlay input/Loopback/FIFO music ducked to 0.200005 and restored to 1.000000; 880 Hz speech decoded, sampled source progress/selection and process identities retained; rootless API/broker speech routing and physical/phone/group/Cast gates not exercised |
| Ubuntu authenticated API → broker → worker → OwnTone final PCM | Passed in 33.4 seconds | Synthetic AirPlay music plus real Opus overlay, cubic local output volume and exact-room speech guards at reverse Loopback slot 7; continuous captured frame offsets and sampled program/PID identities, all ten cleanup checks passed |
| Installed Ubuntu candidate services | 9 checks passed in 29.2 seconds | Rootless authenticated API/CRUD, host-visible namespace ownership, restricted backend listeners, installed-unit SIGKILL/autorestart and exact host-baseline cleanup |
| Ubuntu installation/adoption boundaries | 16 checks passed in 0.11 seconds | Disposable-prefix ownership/symlink guards, retained SQLite companions, OFD locking, idle foreign readers and exact owner/mode adoption; no fresh-host installation claim |
| DHCP address/prefix renewal | Original bug reproduced; 3 corrected transitions passed on the Linux kernel | Same-IP subnet replacement, changed-IP/subnet replacement and unchanged renewal preserve unrelated addresses in an isolated dummy namespace; no router lease-table claim |

The initial Linux smoke captured 380,160 bytes over approximately two seconds,
with peak amplitude 3,277. The writer reported 384,000 bytes written, zero
dropped, and shutdown completed within three seconds. This used a test
oscillator; it does not establish Shairport capture, ALSA Loopback,
OwnTone-to-speaker playback or acoustic synchronization. The reproducible
opt-in checks are retained in
[`tests/test_linux_audio.py`](../tests/test_linux_audio.py).

Test counts are a dated snapshot and may change as the suite grows. Keep final
run output with the release candidate rather than relying on these counts.
Completed full Ubuntu runs retain their refreshed log at
`/tmp/shiri-v2-linux-tests.log` and wrapper results at
`/tmp/shiri-v2-final-checks.json`. The earlier 378-test run also recorded the
16-check installation/adoption boundary harness. That harness's JSON output is
retained in `/tmp/shiri-v2-installation-boundaries.log`. Linux audio
opt-in was enabled, so this run includes actual GI, local Opus and Loopback
checks. The three separately guarded lifecycle cases were skipped in that
suite. They do not erase the independent 50-cycle run, two SIGKILL cases or
installed-service kill/recovery evidence recorded below, and those separate
runs do not cover every unexercised crash phase.

The subsequent independent runtime review opened regressions for boot-scoped
process identity, malformed ownership-manifest preservation, live speaker
handoff, disabled-room cleanup retries and interface-creation crash windows.
Local audio validation now rejects arbitrary ALSA plugins and canonicalizes
equivalent hardware routes. The independent runtime regression file passes
52 tests after the backend-version, converter and numeric-card regressions. It simulates the
actual guest's ignored `NEWLINK` alias, verifies same-boot recovery of an
untagged creation and prevents deletion under substituted MAC, parent, alias,
link kind, boot or namespace inode. Two real Linux SIGKILL recovery cases have
now passed; the broader startup/teardown failure matrix remains open. BlueALSA
access now uses a separate host output worker to
preserve private sender discovery; physical verification is still required
before Bluetooth support can be claimed operational.

Configured local endpoints are now reserved transactionally at room creation
or edit, including disabled rooms without speaker assignments, matching the
broker's device-open behavior. Phone volume commits now retain bounded durable
receipts, so acknowledgment replay preserves the original commit revision and
cannot overwrite newer UI intent. Tests cover concurrent duplicate/distinct
events, ID collisions, restart/abrupt exit, bounded retention and a real
`SQLITE_FULL` failure during receipt insertion that rolls back room and event
changes together.

Final review reproduced two additional integrity failures: a Nobly binding
could move after resolution but before admission, and inconsistent stored
derived keys could evade uniqueness despite SQLite's physical quick check.
The fixes serialize exact admission with binding edits and require stable UUID
follow-ups. Store startup now validates actual column/index/foreign-key/check
semantics and canonical room, speaker, profile and receipt data, rejecting
unmanaged tables/views/triggers while preserving database bytes. Regressions
cover retained-WAL preservation, missing or changed mandatory constraints,
harmless SQL formatting, deleted-room history, blocked in-flight rebinding,
rebound close and lost-acknowledgment retarget attempts. The focused
Store/API/service suites passed 118 tests in 1.38 seconds; the completed Mac and
Linux runs include these changes.
An abrupt-exit regression exposed SQLite checkpointing on the last failed
writable startup connection. Existing databases now receive WAL-aware
read-only validation before writable initialization, preserving committed main
and WAL bytes on rejection without ignoring uncheckpointed intent. Missing or
empty main files with any retained WAL, shared-memory or journal companion,
including dangling symlinks, are rejected before writable access. Abrupt-exit
tests preserve every file after main-file truncation or deletion; genuinely new
empty files without companions can still initialize. The normal write-guarded
initialization rechecks the accepted database afterward.

The full API audio check exposed OwnTone's hardware-mixer fallback interpreting
an exact PCM address as a control device. Loopback has no suitable hardware
mixer. The maintained `29.3-shiri-swvol1` extension instead applies per-session
cubic software gain to a private copy at each final ALSA write, including
buffered and draining audio. It creates no global ALSA volume controls and
keeps the hardware-mixer default when the option is absent. The final patch
SHA-256 is `f9250ebec36873ea39fff78ca3bbc5c424b023ead267868331f985ad0da1fe15`.
Its adjacent negative-offset guard rejects unsigned delay underflow and safely
handles `INT_MIN`; boundary tests preserve valid timing and the original delay
on invalid input. Independent native scaling/control tests and the actual
final-PCM run provide evidence for this bounded extension.

A later runtime review found physical-key canonicalization also changed an
explicit `plughw` playback address to `hw`, discarding requested conversion.
Startup now separates the canonical reservation identity from the playback
endpoint and preserves `plughw` for named cards. Regressions cover exact
rendered named routes, device/subdevice indexes, disabled-room alias conflicts
and live exclusive leases. Independent reproduction then kept saved
`plughw:7,0,0` unchanged while a
reordered `/proc/asound/card7/id` redirected playback; unavailable-map fallback
also opened the numeric endpoint. Room creation, edits, stored-room validation
and actual playback now reject numeric `CARD` with named-device examples.
Runtime rejection happens before hardware-map reads, existing-runtime cleanup,
slot acquisition or network/device startup; there is no numeric fallback.
Existing numeric databases are rejected without rewriting their main/WAL
bytes. Legacy migration requires an operator-reviewed copy with a named
endpoint and leaves the original source unchanged. The standalone identity
normalizer retains numeric aliases solely for explicit migration/audit work.
Automatically assigned names of identical USB cards can reorder too. Stable
provisioned card IDs or serial/udev binding and mismatch rejection must support
any claimed physical identity guarantee. The focused domain/store/runtime
suites passed 266 tests with one Linux-only skip in 9.77 seconds; the subsequent
full Mac and Ubuntu reruns above include these regressions.

The real Ubuntu lifecycle run completed 50 enable/disable cycles on Loopback
slot 7. The receiver retained its deterministic MAC and ownership alias, with
the observed address `192.168.1.151`; both were read back. The manifest was
empty of live resources after each stop and final shutdown. No physical
speaker was selected. This validates repeated normal startup and teardown;
overlapping requests, multiple-room failure isolation and process-kill
recovery remain distinct checks.

Reproducible, opt-in Linux lifecycle and crash tests are now in
[`tests/test_linux_lifecycle.py`](../tests/test_linux_lifecycle.py). They require
root, an explicit wired interface, a `shiri-test-...` prefix, a dedicated
state directory whose final name matches that prefix and a closed Loopback
slot 7. The installation identity is preserved between runs; retained
resources fail the initial gate for inspection. Reports are written beneath
the dedicated directory, alongside broker logs. The two crash cases have now
passed on the VM, with result/log artifacts at `/tmp/shiri-v2-crash-tests.json`
and `/tmp/shiri-v2-crash-tests.log`. The 50-cycle case has the separate real
run recorded above; broader interrupted phases still need evidence.

The installed candidate service check passed between 08:09:56 and 08:10:25 UTC
on 2026-09-30. The API ran as UID 999 with no capabilities, strict filesystem
protection and a group-scoped mode-0660 broker socket. Anonymous state access
and invalid login returned 401; authenticated CRUD persisted disabled rooms,
and a stale revision returned 409. On slot 7, all six room/sender daemons became
live, host-visible namespace paths resolved to the recorded kernel inodes, and
the sender exposed only TCP port 3939. Anonymous LAN OwnTone access returned
401; unused MPD/WebSocket listeners were closed.

Only the dedicated test runtime unit's main process was SIGKILLed. Systemd
restarted it, retaining installation UUID, receiver identity/MAC and all six
healthy daemons. Disabling/deleting disposable rooms left the manifest empty,
no owned namespace paths or control links, all slot-7 PCM directions closed,
and host links/addresses exactly equal to their baseline. The artifact is
`/tmp/shiri-v2-service-check-result.json`, retained on both Mac and guest. This
passes the installed candidate's exercised service/recovery path; it does not
establish a clean installation on a fresh host or least-privilege execution of
the backend/audio children, which still retain root authority.
The passed service harness is retained at
[`tests/linux/check_candidate_services.py`](../tests/linux/check_candidate_services.py)
for reproducing that dedicated candidate scope.

```sh
sudo env SHIRI_LINUX_LIFECYCLE_TESTS=1 \
  SHIRI_TEST_INTERFACE=eth0 \
  SHIRI_TEST_PREFIX=shiri-test-lifecycle \
  SHIRI_TEST_STATE_DIR=/var/lib/shiri-test-lifecycle \
  SHIRI_TEST_BINARY_DIR=/opt/shiri/backends \
  /opt/shiri/venv/bin/python -m pytest -s tests/test_linux_lifecycle.py
```

## Measurable release gates

The following combine the required product gates with measurable robustness
criteria. A pending gate must acquire an actual result, environment and artifact
before it can be marked passed. Backend limitations do not remove a requirement.

| Gate | Acceptance criterion | Current status |
| --- | --- | --- |
| Automated regression | Full Python and web/browser suites pass for the candidate; lint and diff checks clean | Latest completed Mac 387/9 skips and Ubuntu 393/3 skips; new converter/observer regressions await full rerun; browser 25 passed with unchanged frontend |
| Pinned runtime | Builder pins source revisions and the maintained patch hash; preflight requires OwnTone `29.3-shiri-swvol1`, Shairport version/features or its build-manifest SHA fallback, matching NQPTP shared-memory interface, GI/plugins and eight ALSA slots | Patched final ALSA path and 16 Linux installation/adoption boundary checks passed; fresh-host installation still pending |
| Installed services | Rootless authenticated API persists revisions; installed broker restarts after owned SIGKILL with host-visible resource identity; disable removes owned resources and leaves host baseline unchanged | 9 real Ubuntu candidate checks passed; fresh-host installation still pending |
| Least-privilege daemons | Root broker owns setup/cleanup; backend/audio workers run under separate nonroot room credentials with minimal capabilities, read-only configuration/executables and narrowly scoped writable state; hook identities permit only bounded signals for their exact room | Required permission boundary missing; candidate daemons still retain UID0/capabilities |
| Native AirPlay 2 input | Stock iPhone discovers each enabled zone and uses its existing controls to play through assigned outputs without a Shiri phone app | Candidate receiver implemented; stock-phone whole-path test pending |
| Native Chromecast input | Every enabled zone advertises a real Cast receiver; stock-phone clients authenticate and start/control required media and audio-streaming paths | Required adapter missing; admission research in progress |
| Competing source ownership | Repeated AirPlay/Cast takeovers and competing phones follow an explicit policy; stale volume/disconnect events never alter the successor | Pure policy tested; native adapters, PCM enforcement and runtime integration missing |
| Native iPhone grouping | Multiple zone receivers are selected using native controls; measured final outputs preserve supported sync through startup, regrouping and long playback | Required whole-path verification missing; independent-player timing gap unresolved |
| Stable local hardware identity | The admitted physical device stays stable across restart/reordering; missing or changed identity rejects playback rather than selecting a successor | Numeric CARD rejected at configuration, existing-store startup and runtime admission; stable provisioned names or verified hardware fingerprints for identical USB devices remain required |
| Real input/output path | Physical AirPlay source → Shairport → ALSA Loopback → mixer → OwnTone → selected speaker produces valid uninterrupted program audio for 30 minutes | Pending |
| Targeted speech | Two enabled zones; 20 sessions per zone including music overlap, conflict and cancellation; zero wrong-zone output; music continues advancing without pause/seek/restart/reconnection or phone disconnect; gain restores smoothly | Authenticated rootless API/broker/worker → actual OwnTone final Loopback PCM passed with exact-room guards, duck/silence/close restoration and advancing sampled programs; multi-zone physical output and stock-phone continuity pending |
| Device matrix | Actual AirPlay 1, AirPlay 2, Chromecast and configured paired Bluetooth/ALSA devices tested individually and in supported mixed groups | Pending; device availability required |
| Constant timing correction | Manual offset acknowledgment, stopped/playing application, retention across restart and measured sign/effect at fixed geometry | Profile tests passed; backend/acoustic gate pending |
| Calibration quality | At least 20 valid bursts across three restarts; before/after lag distribution, rejected attempts, jitter and 30-minute drift reported | Design recorded; measurement module and physical gate pending |
| Room lifecycle | 50 enable/disable cycles and overlapping start/stop requests; no leaked owned namespace/process/slot; healthy room continues during another room failure | 50 basic real Ubuntu cycles passed; overlap and multi-room fault isolation pending |
| Crash recovery | Terminate broker at multiple startup/teardown stages; restart recovers owned resources without affecting unrelated host namespaces/processes or leases | 2 real VM SIGKILL cases and installed-unit SIGKILL/autorestart passed; broader startup/teardown matrix pending |
| DHCP/VM bridge | Verify lease renew/release, secondary-MAC reachability, mDNS discovery, gateway/ARP behavior and no host lease mutation over repeated cycles | Repeated candidate DHCP/discovery and exact host-baseline checks passed; lease renewal/router/ARP matrix pending |
| Outage behavior | Speaker disappearance, backend exit, FIFO reader loss, broker unavailable and constrained storage remain bounded and visible; saved intent survives | Component tests passed; full runtime fault injection pending |
| Rollback | Restore the recorded backup and legacy checkpoint in a rehearsal; capture exact restoration steps and recovered state | Backup/checkpoint preserved; rehearsal pending |

No fixed mixed-protocol sync tolerance is claimed before measurement. Record
per-device and per-condition observed error, then choose a supported product
claim based on those distributions. OwnTone itself warns that Chromecast cannot
be precisely aligned with other outputs. [OwnTone Chromecast documentation](https://owntone.github.io/owntone-server/audio-outputs/chromecast/)

That output limitation must inform backend fixes or architecture changes. It
does not waive the per-zone Cast input requirement or native iPhone grouping
gate. A synthetic OwnTone-to-Shairport input check is useful component evidence,
but it cannot substitute for stock-phone interoperability or final-speaker
timing measurements.

The manual [`check_airplay_input.py`](../tests/linux/check_airplay_input.py)
passed on the dedicated Ubuntu
candidate between 08:00:59 and 08:01:34 UTC on 2026-09-30. It generated a private
60-second 440 Hz WAV and selected only the exact candidate AirPlay 2 receiver
by its name and MAC-derived decimal output ID. Actual Shairport → ALSA Loopback
slot 7 → mixer FIFO capture retained 3,475,200 bytes / 868,800 stereo S16 frames,
with 99.7369% median energy in the tested 440 Hz band and zero channel difference.
The source advanced 10.13 seconds; real music-start/stop hooks were observed.
Another candidate OwnTone reader could consume FIFO writes, so the captured
blocks do not establish a full-rate delivery result. All five cleanup checks
passed: source stopped, broker closed, manifest empty, slot closed and exact
host baseline preserved. Results are retained at
`/tmp/shiri-v2-airplay-result.json`; private logs, WAV and PCM are under
`/var/lib/shiri-v2-test-runtime/airplay-source-adxdbtu3` on the guest.

The extended manual [`check_airplay_tts.py`](../tests/linux/check_airplay_tts.py)
passed between 08:25:20.195 and 08:25:52.689 UTC on 2026-09-30 (32.5 seconds).
Real Opus WebRTC speech entered the room AudioWorker RPC while the synthetic
AirPlay source kept its exact receiver selection and queue item. Fitted 440 Hz
music amplitude ratios were 0.200005 during ducking, 1.000000 while the same
connected speech session sent silence and 1.000000 after healthy close. The
mixed FIFO contained 880 Hz speech with fitted amplitude 423.0; worker gain
reported 0.2 during speech and 1.0 after restoration. First qualifying music
appeared 0.986 seconds after control-play observation, and the sustained PCM
onset gate passed at 1.259 seconds. Earlier health/play flags alone had produced
an all-zero baseline, so the harness now gates on actual 440 Hz PCM without
lowering quality thresholds.

Across 80 control samples, source progress advanced from 10 to 8,200 ms with
unchanged PID/birth identities and zero reported FIFO drops. All seven cleanup
checks passed: speech session/peer closed, source stopped, broker closed,
manifest empty, slot closed and exact host baseline preserved. The result is
retained at `/tmp/shiri-v2-airplay-tts-result.json` on Mac and guest. The route
directly exercises the worker because no house outputs are selected; it does
not test rootless API or broker speech routing. These sampled component results
do not prove stock-phone continuity, physical outputs, native grouping, Cast
input, full-rate FIFO delivery or acoustic synchronization. Usage and fixed
candidate/root/slot guards are in [`tests/linux/README.md`](../tests/linux/README.md).

The authenticated API-to-final-PCM harness passed from 10:07:05.862 to
10:07:39.246 UTC on 2026-09-30, in 33.4 seconds. A synthetic OwnTone AirPlay 2
source fed the candidate receiver, while the API ran as UID 999 with zero
effective capabilities. Anonymous access returned 401; authenticated room
assignment selected only local output `0`. Speech traversed HTTP → broker →
room WebRTC worker → mixer → the patched OwnTone ALSA output. Disabled-room
admission returned 409, an unknown external binding returned 404, and a close
against the wrong stable room UUID returned 409 without closing the admitted
session.

Final reverse-Loopback capture observed local volume `100 → 50 → 100`: the
50-percent amplitude ratio was 0.124934084, consistent with the specified
cubic ratio 0.125, then restored to 1.0. Speech ducked 440 Hz music to
0.199998665 while adding decoded 880 Hz audio. Silence from the same connected
speech session restored music to 1.0; speech resumed and an authenticated
close restored it to 0.999999345. Each transition required a fresh sustained
PCM window. Individual post-onset blocks were also checked, so a median could
not hide a silent interval and readiness retries could not discard failures.

Capture retained 882,240 stereo S16 frames across 919 blocks with contiguous
sample offsets, only the initial discontinuity flag, a maximum callback gap
of 25.5 ms and zero reported FIFO drops. Sampled source and candidate items,
selection, advancing progress and PID/birth identities remained stable. All
ten cleanup checks passed, including stopped speech/source/API/capture,
closed broker, empty manifest, deleted disposable rooms, closed slot 7 and
exact host-baseline preservation. The result is retained at
`/tmp/shiri-v2-airplay-api-tts-result.json`; private PCM and source artifacts
are under `/var/lib/shiri-v2-test-runtime/api-airplay-source-25wiuwg8` on the
guest. About 100 ms control polling cannot rule out shorter control
transients, and per-block audio checks do not establish sub-block continuity.
This is a synthetic-input/kernel-output result, with no stock phone, physical
speaker, native grouping, Cast input or least-privilege-daemon claim.

The candidate had no assigned physical outputs. This establishes synthetic
AirPlay 2 input/PCM and hook behavior while leaving stock-phone, Cast input,
native grouping and final-speaker timing gates open. The source SETUP hook
precedes the player's `play` state, so the harness waits for the latter before
asserting advancing playback. Pinned-source verification also confirmed fixed
receiver PCM settings belong in `alsa`; the verbose automatic-selection flags
are not an authoritative readback of those sets. Do not change their placement
based on that log alone.

## Next review priorities

The DHCP renewal review reproduced a stale-address bug: renewing the same IP
with a different subnet mask left both prefixes configured because `ip addr
replace` matches the prefix as well as the address. The hook now removes the
exact old address/prefix when either changes. Three regressions and the actual
Linux kernel check passed, including unrelated-address preservation, test
namespace removal and the exact host baseline. The repeatable manual harness
is [`tests/linux/check_dhcp_renewal.py`](../tests/linux/check_dhcp_renewal.py);
its report is `/tmp/shiri-v2-dhcp-renewal-result.json`. This verifies address
transitions independently of the still-pending router lease-renewal matrix.

Complete the physical Shairport/ALSA/OwnTone path and stock-iPhone tests while
evaluating a real per-zone Cast input adapter. Implement shared music-source
ownership, and verify or correct native iPhone group timing through the final
outputs. Extend the passed lifecycle run with overlapping requests and crash
recovery fault injection while checking the ownership manifest and router
lease table. Both missing product capabilities and the original networking
complaints remain part of this rebuild.

Next implement the staged calibration workflow and retain measurements across
AirPlay, Chromecast and paired Bluetooth. Use that evidence to decide whether
an OwnTone adapter fix is sufficient or a backend change is justified. A future
Sendspin experiment must use the same device, load, recovery and measurement
criteria and keep the product domain/API intact.

Production replacement requires both native input receivers, safe input
arbitration, verified native grouping, and the outstanding Linux/physical
checks, including least-privilege daemon execution. Component and browser
tests provide supporting evidence.
