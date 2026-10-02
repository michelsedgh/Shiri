# Clean rebuild review record

## Current candidate — October 2

The coherent rebuild completed software qualification and is installed as
LIVE182. The user confirmed fresh iPhone playback without changing web volume,
phone volume reaching the speakers and web room master, web volume moving the
iPhone slider, and resume after a requested 40-second pause. The post-test
readback retained all nine daemon identities and their strict policy, with
running services/room and matching room/backend master. Software gates cover
native builds, encrypted playback/speech/control, finite speech performance,
Bluetooth's digital route, lifecycle/recovery, reboot and exact rollback.
See [the live test handoff](LIVE_TEST_HANDOFF.md) for the current installation
and next tests, and [the checkpoint](CHECKPOINT_2026-10-02.md) for exact evidence.
Physical acoustic alignment, native grouped-zone phone playback, mixed
transport speakers and per-speaker balance checks remain later measurements.
The latest CI rerun is pending after a focused test-clock repair; that repair
does not change the qualified production package or native binaries.

Current minimum route defaults are B40/H140 for local or framed Bluetooth,
B250/H350 for Cast or Pulse, and B500/H600 for AirPlay. A grouped program uses
its slowest route's policy and configured offsets. Earlier four-second settings
below are historical review records and do not describe the current defaults.
Chromecast input is deferred and Bluetooth input is excluded.

## Historical candidate and review evidence — October 1

The entries below retain their original checkpoints, build identities, failures
and then-pending gates. They are historical snapshots; their pending language
does not supersede the current status or [live handoff](LIVE_TEST_HANDOFF.md).

- The user explicitly deferred Chromecast input after the current open-source
  receiver review. This revised scope supersedes earlier entries below that
  called Cast input mandatory. AirPlay 2 receivers, mixed speaker outputs
  (including Cast outputs), exact-zone speech and minimum latency remain
  required. The review loop continues; Bluetooth input remains excluded.
- The setup UI is available for feedback at `http://127.0.0.1:8765/` on this
  Mac. This temporary preview has its own database and simulated speakers;
  it makes no house audio or room-configuration changes. Its browser smoke
  check passed without JavaScript errors. Phone access and real audio remain
  separate acceptance steps. The current frontend also passed all39 client
  and enabled Chromium browser tests, including room/speaker conflicts,
  mobile layout and calibration review.
- Bluetooth is an **output** route. Bluetooth phone input is excluded by the
  user. A speaker-managed group is assigned through its main paired endpoint;
  the group shares one zone's music and speech.
- The current coherent348-file candidate passed **3146 tests,81 platform
  skips**, Ruff and all five shell syntax checks. Its reviewed24-file speech
  ownership layer is integrated with the earlier networking/deadline and
  diagnostic fixes. Independent composition review preserves all324 unowned
  current files and the complete authenticated39-field diagnostic appendix.
  The frozen source archive is separate from actual Linux qualification;
  minimum-default changes remain private and unpromoted.
- The earlier coherent340-file source candidate63 passed **3092 tests,81 platform
  skips**, Ruff and shell syntax checks. It integrates the reviewed native
  jitter/readiness work, seven Python cancellation/deadline changes and four
  Bluetooth speech diagnostic files. Independent checks reproduce the exact
  Jammy `asyncio` function behavior on the Mac; full execution in the installed
  Python3.10 environment remains a separate qualification.
- The isolated58 rebuild completed native14-patch compilation, the coherent
  49-file wheel and service configuration, then failed the final dependency
  check: distro PyGObject declares a missing `pycairo` dependency. The installer
  now declares `python3-cairo` and runs `pip check`. Actual61 installed only the
  signed Jammy Cairo package; package audit, imports, native/source identity,
  service policy and reboot62 admission passed. That repaired checkpoint
  still contained source335/package49, so it did not qualify the later50-file
  package. Failed55/58 receipts remain failed and are preserved.
- Actual67 installed the exact source340/package50 wheel using a local
  hash-pinned requirement. Package audit, imports, installed-source equality,
  native11 identity, all production dependencies and persistent ownership
  remained correct. Its fresh Python3.10 test environment failed before pytest:
  the transient job's16MiB file limit also capped extracted dependency libraries,
  producing `Errno27: File too large`. The run remains failed, with typed
  retirement and normal VM shutdown verified. The next combined owner15
  build will separate the installation file bound from the bounded log capture
  and run the complete348-file suite on actual Python3.10.
- Actual68 compiled and staged the reviewed15-patch OwnTone backend, promoted
  only the OwnTone binary and its two added provenance fields, and installed
  the exact348-file source/50-file package. Native credential checks, package
  imports, `pip check`, installed broker preflight and all29 owned-job
  retirements passed. The complete actual Python3.10 suite reached3221 passes,
  four skips and two failures: cancellation-supervisor tests expected a custom
  attribute to survive the task's `CancelledError` boundary. Their direct-child
  cleanup assertions passed; durable cancellation evidence is being checked
  before a narrow repair and complete suite rerun. This remains a failed
  qualification, with no current playback or minimum-default promotion claim.
  Final installed/persistent/dependency admission matched the post-install
  admission exactly; the isolated VM stopped normally and the house VM stayed
  unchanged.
- Actual70 preserved native68's installed backend/package and copied its exact
  source348 into `candidate-owner2`, changing only the two failed test bodies
  in one file. They now assert cancellation and the exact held durable
  supervisor receipt, retaining all direct-child/namespace cleanup checks.
  The complete actual Python3.10 suite passed **3223 tests, four skips** in
  125.50s. Full source/package/native/dependency/protected-state admission
  passed afterward and on a fresh reboot, including installed broker preflight.
  Both owned boots stopped normally, and the original house VM/image remained
  unchanged. This clears Linux build/regression admission; current low-buffer
  playback, speech/Bluetooth, recovery, soak and rollback gates remain open.
- Actual69 repeated the unchanged H140/B40 music test on the current backend.
  Both complete21-second outputs were retained, but original full, early and
  late waveform replay measured a4ms relative offset with zero drift, failing
  the unchanged2ms group limit. Actual71 copied the original captures without
  changing source, installed files or protected state. Independent inspection
  confirms a250Hz kernel and the loopback system timer's4000µs resolution;
  actual playback timestamp clock type was unavailable, so trigger times do
  not establish absolute presentation accuracy. The test-clock hypothesis
  remains unproven and minimum defaults remain unpromoted. All fixture
  retirement, final admission and normal VM shutdown checks passed.
- Actual72 passed complete finite speech on the idle route, then failed the
  native route's post-tail quiet check; its warm native row was not launched.
  Read-only73 retained the original audio and clocks. Independent replay
  shows that the prescribed music restoration ramp accounts for the apparent
  residual: one carrier phase frozen from pre-voice audio, the exact wire
  ducking gain and native gain slope explain the entire quiet interval within
  the original noise threshold. A narrow measurement repair is under review;
  the original failure remains failed, and the repaired route has not passed.
- Actual74 reached sustained audible speech and restored advancing music in
  both zones, then failed the immutable whole-session waveform comparison
  during the first cancellation pair. The comparison's cancellation boundary
  is being checked against the native queue behavior; natural EOF must still
  preserve the complete speech tail. The remaining stress pairs and Bluetooth
  job were not launched. Full installed/protected admission, empty ownership,
  job retirement and normal VM shutdown passed after both72 and74.
- Actual75 ran the full patched production BlueALSA/SBC output route. Music,
  volume changes, active speech, silence restoration and resumed speech passed.
  Its first qualified close-restoration block then failed the original
  forbidden-tone check while music was still approaching full volume. Exact
  retained audio/timestamp replay reproduces that failure; its cause remains
  under investigation. No packet loss, speech underflow or expired speech was
  observed. Takeover/END stages were not reached. Empty ownership, complete
  cleanup, final installed/protected admission and normal VM shutdown passed.
- Actual76 completed all50 original enable/disable cycles and the original
  broker SIGKILL recovery at network reservation. Active-room recovery reached
  its old saved-process assertion, which indexed `pid` on a current
  `systemd-unit` ownership record and raised `KeyError`. The typed retirement
  assertion is being repaired in the test; PID/birth checks remain required
  for direct processes, and systemd workers require unit/cgroup evidence.
  This run remains failed. Original kernel/network/Loopback preservation,
  empty ownership, fixture retirement, full final admission and normal VM
  shutdown all passed.
- MUSIC56 stopped before playback because the repair helper created a Python
  bytecode cache inside the strictly admitted source tree. Read-only63 proved
  it was the sole extra file/directory and all335 declared files were unchanged.
  Checkpoint64 preserved that exact cache, the failed test work and reboot62
  profile, then reran the same music guards with a fresh boot-bound profile.
  Both complete21-second outputs and the unchanged alignment checks passed;
  its aggregate failed because the external cleanup assumed a successfully
  completed transient unit would still exist. Checkpoint65 corrected only
  that outer typed retirement, preserving every playback guard. Its repeated
  music test retained all1,008,000 frames per output but failed the unchanged
  2ms relative timing limit. Exact read-only66 retrieval and original-analysis
  replay reproduce a steady4ms offset with zero measured drift. Minimum-buffer
  promotion remains unqualified while startup/measurement timing is investigated.
- The complete Linux music experiment passes at B500/H750, with both outputs
  retaining the complete21-second waveform. A separately reviewed B40/H140
  candidate for wired/direct Bluetooth routes is under Linux qualification
  on the rebuilt backend. A private default-policy proposal passes3098 tests
  with81 platform skips and retains40/250/500ms ALSA/Cast-or-Pulse/AirPlay
  floors even with positive corrections. It explicitly retains the failed65
  timing gate and has not replaced the production default.
- Original Bluetooth53 artifacts decode byte-for-byte. A reproduced queue
  starvation inserts480 silent speech frames between two otherwise valid
  packets while the music calendar keeps advancing. The integrated correction
  adds20ms of initial speech reserve, preserves the original250ms packet
  expiry and records ingress/underflow evidence. Actual Linux component checks
  passed; unchanged whole-route speech guards still need execution. The bounded
  diagnostic layer and narrowly reproduced Python3.10 cancellation corrections
  are now integrated in candidate63.
- An independent actual-C replay of production speech packets found a separate
  cancellation fault: queued speech from request A can play after request B
  owns the API, carrying A's previous ducking gain. The current80-byte wire has
  no speech identity or acknowledged distinction between cancellation and
  natural EOF. The integrated additive backend change binds
  the voice queue to the existing readiness speech ID, confirm cancellation,
  reject stale speech and preserve the complete tail at natural EOF. Actual68
  compiled and installed this backend; complete-suite and real digital-route
  qualification remain open release gates.
  The independently reviewed15th-patch proposal passes3116 tests with81 platform skips and
  independent actual-source C sanitizer checks. It changes only six native
  files and preserves the original250ms packet lifetime,20ms initial speech
  reserve and source/output transports. Coherent integration now passes the
  larger3146-test suite; a complete backend build and actual Linux route
  qualification remain required. Cancellation
  cannot retract speech already mixed and dispatched to downstream outputs
  without flushing the shared music; the remaining queue/lease retirement
  boundary and unavoidable downstream tail must be reported separately.
- The stopped VM disks were copied and compared before the native upgrade.
  The original house VM and legacy application are preserved. Future Chromecast
  input needs an approved receiver stack or certified receiver with capture
  and control; the public Google app SDK does not provision this VM.
  The current evidence and alternatives are in
  [CAST_INPUT_FEASIBILITY.md](CAST_INPUT_FEASIBILITY.md). Phone, radio,
  microphone and physical-speaker acceptance are deferred by the user.

Current evidence: `/tmp/shiri-ui-preview-result.json`,
`/tmp/shiri-current-web72-result.json`,
`/tmp/shiri-finite72-controller-result.json`,
`/tmp/shiri-retrieve73-result.json`,
`/tmp/shiri-finite72-independent-baseline-restoration-proof.json`,
`/tmp/shiri-routes74-controller-result.json`,
`/tmp/shiri-bluetooth75-controller-result.json`,
`/tmp/shiri-bluetooth75-retained-original-shyzcspt/manifest.json`,
`/tmp/shiri-bluetooth75-original-scalar-replay-result.json`,
`/tmp/shiri-lifecycle76-controller-result.json`,
`/tmp/shiri-minimum69-controller-result.json`,
`/tmp/shiri-retrieve71-result.json`,
`/tmp/shiri-music69-original-replay-result.json`,
`/tmp/shiri-native70-controller-f2teahht/result.json`,
`/tmp/shiri-native70-test-only-shared-apply-result.json`,
`/tmp/shiri-native68-controller-6lxcntqh/result.json`,
`/tmp/shiri-native68-exact-guest-report.json`,
`/tmp/shiri-app67-controller-5dm7noi2/result.json`,
`/tmp/shiri-native63-after67-retained-proof.json`,
`/tmp/shiri-coherent-owner1-root-full-result.json`,
`/tmp/shiri-coherent-owner1-shared-apply-result.json`,
`/tmp/shiri-source-owner1-archive-result.json`,
`/tmp/shiri-speech-owner-composed-independent-scope-review.json`,
`/tmp/shiri-speech-owner-test-design-20261001/composed_owner_python_review.json`,
`/tmp/shiri-coherent63-root-full-result.json`,
`/tmp/shiri-coherent63-shared-apply-result.json`,
`/tmp/shiri-source63-archive-result.json`,
`/tmp/shiri-clean-os-native-music54-result.json`,
`/tmp/shiri-upgrade58-controller-93ke9jx9/result.json`,
`/tmp/shiri-repair61-controller-h_2vs26g/result.json`,
`/tmp/shiri-readonly-inspect60-result.json`,
`/tmp/shiri-readonly-inspect63-result.json`,
`/tmp/shiri-minimum64-controller-result.json`,
`/tmp/shiri-minimum65-controller-result.json`,
`/tmp/shiri-retrieve66-result.json`,
`/tmp/shiri-music65-original-replay-result.json`,
`/tmp/shiri-production-minimum-freeze-result.json`,
`/tmp/shiri-speech-owner-independent-native-review.json`,
`/tmp/shiri-installer61-cairo-shared-apply-result.json`,
`/tmp/shiri-before63-stopped-preservation-result.json`.
These are separate scoped receipts; an upgrade or component pass does not
qualify final playback or the complete production goal.

## Active latency correction (September 30 user request)

Minimal latency is required for both music and speech. The four-second native
relay was introduced by this rebuild as conservative timing headroom; it was
not established as a requirement of the previous app. Mixing speech on native
ingress made announcements wait through that relay. That design is being
replaced with a separate bounded, authenticated speech channel into OwnTone's
last shared PCM mix before output conversion and transmission. Music keeps its
source timeline and OwnTone remains the output scheduler. The October 1 user
instruction confirms the same minimum-buffer requirement for music. The explicit
B500/H750 ordinary-room experiment now passes actual isolated Linux startup,
complete first-frame/body/tail and continuous two-zone PCM. The production
candidate still uses H1000; lower route-specific buffers and horizons are the
next qualification, including the real receiver callback and output startup.

The latest component correction fixes the actual Bluetooth40 first failure,
`AF_UNIX path too long`, using per-operation duplicate directory descriptors
for short Linux socket syscall addresses. Canonical room/generation paths,
worker binds and durable ownership records remain intact. Held socket inodes
also make constructor failure rollback and normal cleanup replacement-safe.
The six production/test changes and four finite-utterance diagnostic files
are integrated in the working tree. The independent coherent Mac check passed
249 cases with 43 platform skips. Actual isolated Ubuntu source-only45 passed
208 selected cases with no skips in 11.56 seconds, including 13 actual Linux
long-path/RPC/rollback cases and the 43 finite-speech checks. The installed35
package and all native binaries stayed byte-identical; the test unit, ownership
and QEMU cleanup passed. This is a component pass, not complete Bluetooth-route
or deployed-candidate qualification. Reports:
`/tmp/shiri-source44-independent-mac-tests-result.json`,
`/tmp/shiri-clean-os-source45-result.json`,
`/tmp/shiri-bt-socket-v2-finite4-shared-apply-result.json`.

Source-only44 failed before running tests because pytest is absent from the
production virtual environment. Its exact failed result is preserved;45 uses
the existing separate suite36 environment with unchanged source44 bytes. The
same read-only boot retrieved 343,141 bytes of the exact latency42 diagnostics,
SHA-256 `351830bc679e53e989d00e4be2b425aa53def1d84f8fb74cd50db0ca1f49e21c`.
The target audio worker and OwnTone were alive in that invocation, but LOG-level
records do not establish the cause of stopped playback during connected
silence. A bounded player/worker/RTP snapshot before the unchanged warm-path
gate is being reviewed. The finite emitted-Opus checker rejects clipped opening,
body and codec tail; its real cold HTTP/final-output integration is still open.

The complete 300-file working candidate46 passed **2547 tests, 73 platform
skips in 72.91 seconds**, with Ruff clean and every declared source digest
unchanged (`/tmp/shiri-working-integration46-full-tests-result.json`). The stopped
installed35 VM including the latest failures and45 component pass was preserved
again before installation (`/tmp/shiri-before46-stopped-preservation-result.json`).
The first app-only46 attempt stopped before installation because the test
environment lacks the optional `build` module. The corrected47 attempt used
the already verified offline pip wheel builder, validated all 48 package file
digests inside the wheel, installed it with a SHA-pinned local lock and verified
actual imports from the production virtual environment. Wheel SHA-256 is
`475c010f73fdda1c1779d7e61d5d8985f02dba4d99ad4d7489cd87cb0a6c131f`.
Every native binary/helper/metadata digest stayed unchanged.

Actual Bluetooth47 then started both rooms completely; the production FD
handoff and bridge RPC passed, with no D-Bus rejection and no first startup
exception. It stopped before PCM at the test's descriptor-authority check:
the checker expects a `None` namespace while the real `UnitSpec` and broker
serialize the no-namespace state as an empty string and omit
`NetworkNamespacePath`. A test-only canonical representation correction and
stronger FD-only authority mutants are being reviewed. Zero SBC frames were
delivered, so this does not qualify the whole Bluetooth route. All 11 inner
cleanup gates, outer cgroup retirement, empty real ownership and normal QEMU
exit passed; the original house VM stayed unchanged. Reports:
`/tmp/shiri-clean-os-native-bluetooth46-result.json`,
`/tmp/shiri-clean-os-native-bluetooth47-result.json`.

The reviewed epoch-evidence correction is integrated separately. Optional
health and RTP diagnostics record errors without replacing the original warm
player assertion or swallowing task cancellation. Its frozen regressions and
the combined independent 500-case check passed. The explicit finite Linux
supervisor remains under review: reusing a WebRTC peer does not prove a warm
output, so a second utterance must require the already playing original item,
source, selected route and units before releasing its waveform. No finite
HTTP/final-output, minimal-latency or production-ready result is claimed yet.

The stricter descriptor-authority checker is now integrated and passed actual
Bluetooth48 admission. The next gate failed before native producers emitted
data: `Native source incarnation changed during TTS`. Both producer receipts
were at their granted stage with zero frames, and the actual SBC transport
captured zero packets. This is a source-admission observation failure, not a
speech-latency measurement. Its original report and 303,586-byte failure
diagnostic are preserved. Every inner cleanup gate, empty ownership, exact
outer unit retirement and normal QEMU exit passed. Report:
`/tmp/shiri-clean-os-native-bluetooth48-result.json`.

The explicit finite Linux supervisor and strengthened warm qualification are
now integrated as eight test-only changes. An independent coherent check
passed **606 tests, 2 platform skips in 26.15 seconds**. A warm utterance must
find the original item already playing before waveform release, then retain
the same item, source, route, output offset and unit identities throughout
delivery. A stopped player fails before optional diagnostic RPCs. The controller
independently reads the exact original emitted-Opus reference, final PCM and
timestamp artifacts to verify the complete opening, body and codec tail.
It keeps completeness separate from an unqualified latency budget and does
not invent a backend timer-ready acknowledgement. Actual cold/warm Linux
execution remains open. Integration proof:
`/tmp/shiri-finite-warm-v2-shared-apply-result.json`.

Actual finite49 stopped before audio because the new epoch supervisor created
the common work ancestor as root-only mode0700. The child's existing-directory
check did not change that mode, preventing the rootless API from reaching its
own mode0700 database below the separate group-traversable fixture. Read-only50
retrieved the exact API traceback. The two-file fixture correction creates and
requires the common ancestor as root0755 while keeping individual supervisor
directories root0700 and receipts0600. It refuses unsafe or inaccessible
preexisting directories without changing their permissions. Actual Ubuntu51
passed all six service-credential, SQLite traversal, private-receipt refusal
and unsafe-path checks; independent review found no blocker. Reports:
`/tmp/shiri-clean-os-native-finite49-result.json`,
`/tmp/shiri-clean-os-inspect50-result.json`,
`/tmp/shiri-work-parent51-shared-apply-result.json`.

Finite51 then completed its idle cold and warm utterances through the actual
authenticated API, OwnTone and kernel Loopback capture. The inner fixture
verified complete emitted-Opus prefix/body/tail, an already-running unchanged
warm player, the original other-zone audio and all cleanup gates. The aggregate
controller rejected adjacent final timestamps because it assumed exact sample
calendar increments to two nanoseconds. Read-only52 retrieved every original
reference, PCM and timestamp artifact with held-file identity and SHA proof;
the GStreamer timestamps contain small variations while the live sample-offset
continuity check passed. The controller must retain and independently recheck
the original per-buffer sample metadata rather than widen an unsupported
timestamp tolerance. The native second fixture was correctly not launched, and
no aggregate completeness or latency qualification is claimed. Reports:
`/tmp/shiri-clean-os-native-finite51-result.json`,
`/tmp/shiri-clean-os-inspect52-result.json`,
`/tmp/shiri-finite51-retained-original-artifacts/receipt.json`.

The separate Bluetooth startup generation guard is integrated after an
independent 287-case check with eight platform skips. The actual native
controller reproduces a valid granted owner with zero blocks and no PCM
generation until its first packet. That state is allowed only during exact
pre-PCM startup; source readiness, zone, protocol and owner remain strict.
Accepted PCM or onset permanently requires the full generation. Bounded
observations are retained before assertions, including the first rejection.
No production code or PCM thresholds changed. Integration proof:
`/tmp/shiri-bt53-startup-shared-apply-result.json`.

Actual Bluetooth53 passed that startup guard, both source incarnations and the
full descriptor route, then delivered 2,407 actual SBC/RTP packets. Music and
volume plateaus passed. The speech stage rejected block2355 at its forbidden
frequency guard. MUSIC54's next boot retained the three exact transport files
and diagnostics before preserving53's work directory. Independent replay
decoded all packets into byte-identical PCM and timestamps: blocks2355/2356
contain a brief own-voice disturbance with fitted660Hz above the unchanged
threshold8. That frequency fit alone does not establish wrong-zone routing;
the cause remains under investigation. All14 inner cleanup gates, empty real
ownership, exact outer-unit retirement and normal QEMU exit passed. Reports:
`/tmp/shiri-clean-os-native-bluetooth53-result.json`,
`/tmp/shiri-bt53-retained-original-artifacts-result.json`,
`/tmp/shiri-bt53-exact-spectral-replay-result.json`.

Actual MUSIC54 passed on the unchanged installed46 application/native35 backends
using the independently reviewed seven-file explicit test fixture. B500/H750,
the two producers, captures and source calendar were frozen before BEGIN.
Both final outputs retained exactly1,008,000 frames, the entire21-second coded
prefix/body/tail. Relative offset and early/late drift were0ms on the1ms analysis
grid across15.76 seconds, with absolute corrected origin errors−1.43/−1.38ms
inside independently declared capture uncertainty. Live original source, units,
NPT, selected outputs and frame continuity remained valid; all13 inner cleanup
gates and outer preservation/normal shutdown passed. This is synthetic native
input and digital Loopback evidence, not stock-phone, Bluetooth-radio, physical
speaker or speech qualification. Report:
`/tmp/shiri-clean-os-native-music54-result.json`.

The privately composed readiness1, finite ACK-v3 and current MUSIC fixture
passed a complete root review: **2,897 tests,80 platform skips in89.11 seconds**,
Ruff clean, all318 declared source hashes unchanged. Independent ACK-v3 review
also passed404 cases and exact patch reconstruction. Original Gst sample
offset/end/duration/DISCONT metadata is retained for fresh qualification, rather
than inventing it for the failed51 artifact. The readiness/native upgrade and
new OwnTone post-input-peek clock correction remain separate pending gates.
Report: `/tmp/shiri-ready-ack-music54-root-full-result.json`.

The latest Bluetooth clarification adds speaker-managed groups through their
one exact main endpoint. The user subsequently excluded Bluetooth phone input;
its private feasibility research made no shared or VM changes. The
design keeps compatibility generic; IKEA model-specific grouping constraints
are documented in [BLUETOOTH_OUTPUT.md](BLUETOOTH_OUTPUT.md). Group members share
their zone's music and announcements; individual member routing is not invented.

The new candidate computes each room's output buffer from its selected negative
offsets rather than charging every room for the full unused -2000 ms allowance.
Ordinary rooms use 500 ms output lead and a common 1000 ms native relay candidate;
configured negative corrections can raise those values. All enabled rooms share
one relay horizon, including rooms with different output buffers. Administrative
changes retire the affected program incarnations; speech never changes this
plan. These numbers are software candidates until cold and warm final-output
measurements pass. Some AirPlay devices can impose larger buffering, and Cast
adds its own transport delay; physical acceptance remains deferred by the user.

The installed fresh Ubuntu candidate before this correction passed full backend
and application installation, actual reboot, rootless API authentication and
service startup/cleanup. Its next full Linux suite completed with **1985 passed,
3 skipped and 1 failed**. The failure constructed a negative session creation
time on a machine with only 17 seconds of uptime. The test now uses a controlled
valid monotonic origin and checks the real 30-second silent lease boundary;
production lease semantics did not need changing. A full rerun of the revised
candidate remains required. The fresh VM and the original house VM are separate;
no physical speaker or stock-phone acceptance is claimed by these checks.

The first frozen lower-latency candidate (283 files, archive `1df4a34d19b5ec3f053bdf25cb89039160ab8ab5768d067146a9335515bf9435`) completed the Mac suite with **2234 passed, 54 skipped and 9 failed**. Failures exposed old valid-version/buffer expectations and an extracted supervisor fixture missing its explicit legacy lab state. A separate actual clean Ubuntu build passed the composed late speech C sanitizer suite (34,715 checks) and real credential/ancillary/inode cleanup fixture (99 parent checks), then stopped because the preceding extracted timer scaffold lacked the new speech calls. The failed installer and stopped disk are preserved; repairs retain the actual player callback and its timing assertions. Full installation and output latency remain unproved for this candidate. Reports: `/tmp/shiri-candidate33-mac-suite-result.json`, `/tmp/shiri-clean-os-full-install33-result.json`, `/tmp/shiri-clean-install33-failure-preservation-result.json`.

The repaired 283-file candidate34 passed the complete Mac suite: **2254 passed, 54 skipped in 69.93 seconds**, with all source digests unchanged and Ruff clean (`/tmp/shiri-candidate34-mac-suite-result.json`). Actual Ubuntu build34 passed the late credential checks and both repaired timer paths, then exposed the same missing-hook/quality fields in the older partial-read scaffold. That failure remains `/tmp/shiri-clean-os-full-install34-result.json`. The partial-read repair now passes all six composed-source sanitizer fixtures and rejects ordering/PCM mutation preimages. The software-volume checker also admits the exact new reviewed `-speech1` marker; every production candidate34 byte is retained for the next complete installer rerun.

Candidate35 (283 files, archive `62842690a5a69a5328e63e4591eed98baaa4b4eba2522b9cddb2b8e3a036f648`) passed the complete Mac suite with **2254 passed, 54 skipped in 69.39 seconds**. Its unmodified complete installer then passed on the fresh Ubuntu ARM64 VM, including all native builds, the actual 34,715 late-speech sanitizer checks and 99 credential/socket checks, package installation and service configuration. The installed product remained inactive during the full Linux suite: **2304 passed, 4 skipped in 75.73 seconds**. The repeated suite also proved that its completed transient unit was already collected, had zero process IDs and no cgroup; the preceding suite's redundant-stop cleanup failure is preserved separately. Reports: `/tmp/shiri-clean-os-full-install35-result.json`, `/tmp/shiri-candidate35-mac-suite-result.json`, `/tmp/shiri-candidate35-linux-suite36-result.json`.

The first low-buffer grouping run stopped before audio because the isolated test LAN lacked `dnsmasq-base`; no output timing result is claimed. Its empty ownership, namespace retirement and outer cgroup retirement passed (`/tmp/shiri-clean-os-suite36-group35-result.json`). A separate actual-source audit reproduced an AirPlay 2 receiver arithmetic overflow at the candidate's smallest negative-offset lead. Negative-offset qualification is therefore open even though ordinary B500/H1000 rooms pass the policy tests. The next loop adds the private LAN prerequisite, measures actual final PCM and corrects the receiver lead boundary before testing the offset matrix. The original house VM remains untouched.

The subsequent baselinegroup36 passed on the exact installed candidate35 with actual H1000/B500 worker health frozen before PCM. Independent kernel capture calibration preceded the run. The two final Loopback outputs measured **0 ms relative displacement and 0 ms drift** on a 1 ms analysis grid across 15.76 seconds; their declared-calendar errors were −1/−2 ms, within independently declared capture uncertainty. Every active music block retained the PCM/control fences: 1,528 blocks per room passed, with zero dropped bytes. Exact-zone WebRTC speech ducked advancing music, silence and close restored it, the untouched room retained its original source and gain, successor takeover and END passed, and every owned namespace/process/capture retired. The first fully qualified duck/voice blocks arrived 669/636 ms after the active/resumed trigger; these are stable spectrum-transition observations, not coded first-sample or cold-start latency measurements. Report `/tmp/shiri-clean-os-native-group36-result.json`, timing subset `/tmp/shiri-native-group36-timing-summary.json`. This supersedes the previous four-second relay baseline for the exercised kernel-digital route; phones, physical outputs, negative-offset timing, cold speech and lower buffers remain separate gates.

Only the missing signed `dnsmasq-base` security package was added to the private lab. Actual APT simulation, archive SHA/size/control checks and the exact dpkg delta passed; all existing package versions, APT configuration and operator service-policy inode/bytes stayed unchanged. No permanent Shiri service was activated. The previous no-audio failure was preserved before the new run; QEMU exited normally and the original house VM identity stayed unchanged.

The exact tested candidate35 is now a separate local checkpoint on
`codex/shiri-low-latency-candidate35`, commit
`d30c0a81c0e8398fcfd11a458209adfdfaed73b9`. An alternate index preserved its
283 frozen files plus `.gitattributes`; the active branch/index/worktree stayed
unchanged. Its stopped installed VM and baseline captures were also preserved
without resetting the current VM (`/tmp/shiri-clean-installed35-preservation-result.json`).
No changes were published.

Candidate36 keeps every installed35 production byte and adds the independently
reviewed SBC observer repairs plus the preceding evidence record. The observer
checks passed **295 tests, 2 skipped**, with Ruff clean. Actual private Bluetooth37
then failed before playback: BlueALSA rejected `OpenRestricted` while the first
zone was starting. Its real daemon UID, binary, Unix-only guard and complete
ownership/namespace/transport EOF cleanup were recorded; no radio, PCM or Bluetooth
latency pass is claimed. Report `/tmp/shiri-clean-os-native-bluetooth37-result.json`.
The next diagnostic run must retain the actual D-Bus rejection and bounded daemon
log before disposing its private directory; changing transport admission without
that cause would not be justified.

The reviewed route-aware lead correction now retains 500 ms for selected
AirPlay speakers and 250 ms for the other outputs after applying each saved
offset. Ordinary B500/H1000 plans are unchanged; AirPlay −2000 requires
B2500/H3000 while wired and Bluetooth retain B2250/H2750. The extracted receiver
arithmetic passes all 4,001 offsets and preserves the old overflow preimage.
An independent coherent candidate36 plus this correction passed the complete
Mac suite with **2309 passed, 60 skipped**, with Ruff and source hashes verified
(`/tmp/shiri-route-policy36-coherent-v2-full-suite-result.json`). The only preceding
suite failure was an obsolete error-message assertion and is retained separately.
The correction is integrated in the working tree; the isolated VM still runs
the protected installed35 production bytes. Linux, actual negative-offset output
and physical-device qualification remain open for the corrected policy.

Diagnostic38 (285 frozen files, installed35-identical production) repeated the
Bluetooth startup failure and retained five authenticated `OpenRestricted`
errors plus the exact 800-byte daemon log before teardown. The mock transport
was acquired and released once; it then refused five reacquisitions with
`One exact synthetic transport only`. This fixture lifecycle can mask an earlier
startup error, so the first exception must be retained and the transport model
reviewed before attributing the failure to production. All owned cleanup passed;
there were zero SBC packets or decoded frames. The stopped VM was booted once
for bounded, SHA-verified read-only retrieval39, then stopped normally. Retrieval
success is not a route pass. Reports: `/tmp/shiri-clean-os-native-bluetooth38-result.json`,
`/tmp/shiri-clean-os-bluetooth39-artifact-inspection-result.json`.

The six-fixture latency harness is under independent review. Its nine rows
measure cold/warm speech and native calendars at −2000, 0 and +2000 ms without
replanning the untouched room during a run. Signal, timeline and cleanup success
are separate from latency-performance acceptance and complete-utterance delivery.
The reproduced old validator admitted 10-second speech; the repeating marker
also accepted a missing 360 ms utterance prefix. These are retained test-harness
preimages, not evidence that production exhibited either behavior. A measured
first qualifying coded run must not be reported as the first nonzero speech
sample or complete prefix/tail delivery. Actual latency characterization and a
finite emitted-Opus prefix/tail comparison remain required.

Diagnostic40 retained the original Bluetooth startup error before retries:
`AF_UNIX path too long` while constructing the handoff listener. Admission had
succeeded, but no bridge worker had started. The room UUID and launch UUID make
the canonical handoff path exceed Linux's Unix-socket limit. The bridge control
path needs the same review. A held-directory FD proposal is under review; its
canonical paths and durable unit/publication identities remain intact. Every
owned resource retired and QEMU stopped normally. Report
`/tmp/shiri-clean-os-native-bluetooth40-result.json`.

The frozen six-epoch harness passed **609 combined tests, 2 skipped** in an
independent coherent tree. Actual matrix41 stopped in its first −2000/idle
fixture at the unchanged synthetic clock's five-millisecond sampling budget.
It retained 1,207 original-room PCM blocks and complete cleanup. An unchanged-
source rerun42 passed that stage and observed a first qualifying cold speech
run at 441 ms final PTS / 466 ms callback from the encoder-frame reference.
These are partial, raw capture observations, not a complete-utterance or minimum-
latency pass. The run then failed `Silent connected peer lost the warm OwnTone
path`; the warm row and remaining fixtures were not launched. Both failures and
their artifacts remain preserved, with no signal/performance promotion. Reports
`/tmp/shiri-clean-os-native-latency41-result.json`,
`/tmp/shiri-clean-os-native-latency42-result.json`.

The complete working-tree integration (293 files, corrected route lead plus
reviewed matrix and Bluetooth diagnostics) passed the Mac suite with
**2480 passed, 60 skipped**, Ruff clean and every frozen input unchanged
(`/tmp/shiri-working-integration43-full-tests-result.json`). The isolated VM's
installed production package is still the protected candidate35. Cold readiness,
full finite utterance delivery, warm silence behavior and actual corrected
Bluetooth routing remain open gates; lowering H from 1000 to 750 ms has not yet
been qualified.

Updated 2026-10-01. Every zone must expose native AirPlay 2 and Chromecast
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

The tested software snapshot is saved locally on
`codex/shiri-software-baseline`, commit
`49a1a76fe402d9e7708f9ef7868eb320048f6fdc`. Its tree contains all 250 exact
fault29 source-manifest bytes plus the retained `.gitattributes`, with executable
installer modes preserved. An alternate Git index created this checkpoint;
the active rebuild branch, normal index and unfinished working files remained
unchanged. Nothing was pushed or published. The checkpoint records the full
software suites and exercised VM passes alongside failed stress28 and open
Cast, phone, physical-device and clean-OS gates. Subsequent detector and failed-
reconnection cleanup refinements remain working changes until separately
verified.

Legacy configuration import is explicit, read-only by default and transactional
when applied. Source bytes are preserved; collisions fail; every imported room
is disabled. Unknown speaker protocols and local outputs without a physical
device require manual selection. No live networking or process state is
imported into the new ownership model.

The current candidate supports independent room programs and preserves the
phone's common presentation timestamps through the patched AirPlay relay when
receivers are grouped. Exact music-source ownership, stale-effect fencing and
within-zone/cross-zone recorded calibration are implemented. The new Linux
launcher passed combined device, socket and filtered-PCM checks; final-output
grouping, source takeover and exact per-zone OwnTone/audio crash recovery passed
the exercised kernel-digital runs below. Repeated speech, broader failure paths
and final device acceptance remain under validation. Native
Cast input still requires a supported receiver SDK and credentials; stock-phone
and physical-speaker checks remain production gates. Nobly does not exist yet,
so the candidate provides its exact-zone speech boundary without a connected
external client. See
[ARCHITECTURE.md](ARCHITECTURE.md), [RECEIVER_RESEARCH.md](RECEIVER_RESEARCH.md)
and [CALIBRATION.md](CALIBRATION.md).

## Review loop

At the user's latest instruction, the current loop must finish all agreed
software and automated/VM tests before stopping. Native phone, microphone and
physical-speaker execution is deferred to a later acceptance phase. Remaining
work includes repeated speech, latency/offset qualification, broader lifecycle
and failure cases, full Bluetooth routing and a supported inbound Cast adapter.
Combined digital grouping and the exercised source/crash transitions have
passed; their phone and physical-device counterparts remain deferred.
Calibration analysis, retained history and its interface
are implemented. These are software tasks rather than substitutes for the later
physical evidence. The active goal remains open.

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
| Product/runtime boundary | Rootless typed API, separate privileged broker and distinct static identities for per-room daemons | API/import tests and real Linux file/device isolation pass; complete gated playback confinement is still under validation |
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
| Latest completed Mac Python suite | 2,254 passed, 54 skipped in 69.39 seconds | Exact immutable 283-file candidate35, including actual offline package build and dependency refusal checks; all source digests unchanged and Ruff clean. Report `/tmp/shiri-candidate35-mac-suite-result.json`; separate repeated-speech and physical gates remain open |
| Latest completed full Ubuntu candidate Python suite | 2,304 passed, 4 skipped in 75.73 seconds | Exact installed candidate35 on actual Python3.10/ARM64, source and production environment unchanged, completed transient unit collected with zero PIDs and absent cgroup. Report `/tmp/shiri-candidate35-linux-suite36-result.json`; its preserved combined receipt separately records the initial missing lab prerequisite |
| Fresh Ubuntu signed dependency resolution | Actual APT update, simulation and download plan passed without changing installed packages | Four official archive signatures verified inside the fresh ARM64 guest; 16 package indexes and all 383 selected archives (281,078,018 bytes) matched their signed SHA-256 metadata. The fixture served these public bytes through one explicit private guest forward with ordinary APT signature checks. Sources/configuration were restored, guest shutdown was graceful and the old VM process identity was preserved. Reports `/tmp/shiri-clean-os-apt-resolution-result.json` and `/tmp/shiri-jammy-offline-packages-result.json`; full installation remains separate |
| Fresh Ubuntu missing audio driver | Exact helper installation and actual module load passed | Initially absent `snd-aloop` became a real ALSA Loopback device on unchanged kernel `5.15.0-194-generic`. Only matching extras `5.15.0-194.204` and absent `wireless-regdb` were added; every original package version/architecture/state and operator policy bytes/inode were preserved. Independent focused tests passed 82 cases. Report `/tmp/shiri-clean-os-kernel-install-result.json`. An earlier private-server encoded-URL failure stopped before installation and is retained as attempt1; full installer/service/reboot acceptance remains open |
| Fresh complete Ubuntu installation | Actual unmodified installer, six maintained backends, BlueALSA, package and service configuration passed | 03:14:31–03:16:50 UTC October 1 restored official Jammy ARM64 guest, exact 261-file source archive `9c7f3fc8a2091243297b70bc18c92e5c420d49ef5f90286c9e051cf3cbf7d954`. Actual native checks/builds passed; application installed from 45 verified offline wheels and local source, GI/audio imports and CLI passed, actual loopback remained available, dpkg audit had no unfinished output, and generated units validated without activation. Source bytes, operator policy bytes/inode and running kernel were preserved. All fixture configuration and build units cleaned up; QEMU exited gracefully and old VM process identity stayed unchanged. Report `/tmp/shiri-clean-os-full-install-result.json`; service startup/reboot, native playback and hardware acceptance remain separate |
| Fresh installed-services reboot/startup | Actual API/runtime readiness, authenticated access and shutdown passed | Installed official Ubuntu guest rebooted with a real Loopback device and clean package audit. Both installed units started with zero restarts; readiness returned `ready:true, simulation:false`, anonymous state access returned 401 and token-authenticated empty state returned 200. API UID/GID 997/998 had zero effective capabilities and no-new-privileges, with its exact PID bound only to localhost:8080. Installed HTML loaded. Both services stopped with empty ownership and no MainPID; guest exited gracefully and the old VM process identity stayed unchanged. Report `/tmp/shiri-clean-os-installed-startup-result.json`. No zones were enabled; this does not establish receiver playback, grouping, physical devices or startup under every failure |
| Fresh backend dependency audit | Actual clean build exposed missing AirPlay 2 UUID development dependency; corrected clean install passed | Attempt2 built private Avahi and NQPTP, then Shairport configuration correctly refused missing `uuid_generate`. `install/build_backends.sh` now explicitly installs `uuid-dev`; the old VM's existing packages had hidden the omission. Read-only review of all six pinned configure/Makefile paths and actual mirrored protobuf/GLib archives found no further required omissions for selected build flags. `/tmp/shiri-clean-os-full-install-attempt2-result.json` preserves the failure. Attempt1 retained an incomplete recommendation mirror, corrected using actual APT's 462-archive plan; attempt3 separately retained a too-small fixture file-size limit during ordinary initramfs maintenance. Each retry restored the stopped clean snapshot; no fix-broken/package repair bypass was used |
| Bluetooth complete-tail observer review | False acceptance reproduced and corrected; independent focused checks passed 68/2 | An actual malformed encoded SBC packet during encoder shutdown escaped the original pre-close success snapshot. The fixture now verifies exact producer stop, drains the queued transport to real EOF, then validates and retains complete PCM/packet evidence. The supervisor checks ordered retirement boundaries and matching final counts. Report `/tmp/shiri-bluetooth-root-final-review-result.json`; original freeze and false-pass reproduction are retained. This is portable fixture evidence; full broker/OwnTone/BlueALSA Linux route and physical radio tests remain open |
| Pure source ownership policy | 32 tests passed | Newest-input grants, exact producer/overlay token and epoch fences, stale callbacks/queued effects and TTS gain-only behavior; no receiver/runtime integration |
| Audio tests | 20 passed, including real local aiortc Opus decode to mono PCM | Negotiation, one-session ownership, timeout/cancellation and media validation; no GI or speakers in the Mac run |
| Expanded domain/store tests | 146 passed | Strict inputs, transactional ownership/revisions, full-storage rollback, process-exit durability, read-only migration, configured device exclusivity, durable phone receipts and startup semantic/canonical audits |
| Independent runtime failure/configuration tests | 52 passed within the focused runtime/domain/store run | Boot/command identity, ownership handoff, cleanup retries, malformed manifests, untagged-link recovery, bounded HTTP, pinned settings, patched-backend preflight, converter-preserving local routes and numeric-card rejection before runtime effects |
| Web/client/browser suite | 39 passed in 5.73 seconds, including 9 actual Chromium cases | Device enrollment, conflict/retry and calibration controls. Retained simulated, imported or incomplete evidence remains qualified after runtime changes; both zones' offsets and saved evidence remain unchanged |
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
| DHCP release on the disconnected LAN | All three acknowledged leases released and final server lease table empty | Actual sender and two receiver MAC/IP pairs matched the private server's received DHCPRELEASE records in the 17:39 UTC run. The group playback run separately failed on input capacity; network cleanup passed. This does not verify the house router |
| Next-profile native timing | Four actual C sanitizer cases passed, including POSIX and timerfd paths | Receiver incarnation/group/flush provenance, partial-skip deadline, first-sample marker boundaries, absolute first player tick and stale-operation fencing; new-profile Linux output timing still pending |
| Next-profile native phone volume | Lost-ACK, generation-change and newer-UI races passed | Stable bounded event identities and original revision bases; same-owner flush reissues latest volume under the new generation without overriding newer UI intent |
| Next-profile opened PCM identity | Actual C sanitizer checks and Ubuntu direct/conversion opens passed | Correct actual PCM admitted; wrong subdevice, boot, node inode and duplicate manifest rejected before hardware parameters; both routes also passed the inherited control-ioctl filter |
| Combined Ubuntu backend build | Twelve maintained OwnTone layers, Shairport checked/bounded clock sampling, actual-source sanitizer checks, configured FFmpeg conversion, build and installation passed | Fresh `/opt/shiri-v2-next9-deps` completed at 23:10:57 UTC from 83 hash-verified build files; archive `eae26ac37a89e4fc3f2c2d4ccc8e907d818bb49efea0e61273a8180d14695afe`. OwnTone SHA-256 `b1334b584be6df9d82e318b0946071c1331b64447a0f84bc678396d9f842104d`; Shairport `6255b528d20e62f35c6344cddf8eb0e937a5af88a292c338b3af4e6e7da536d4`. Exact patch hashes are retained in the manifest and `/tmp/shiri-v2-backend-build-review9-result.json`. Previous prefixes and live legacy deployment preserved |
| Actual Ubuntu bind isolation review | Original configured policy was reproduced as unenforced | Effective IPv4/IPv6 bind program queries returned zero; an output UID could bind a peer room's control port. The candidate now uses a verified broker-installed policy and launch gate |
| Broker bind helper on the actual Ubuntu kernel | 36 bind checks passed at 14:38:06 UTC | Exact cgroup IPv4/IPv6 programs and translated instructions verified before and after an unprivileged child; own TCP port allowed, seven peer ports denied, IPv4 UDP allowed, IPv6 denied, helper privilege refusal and exact cleanup passed |
| Actual Ubuntu isolated PCM review | Original plain and conversion opens reproduced a mandatory control-node dependency | Numeric ALSA configuration avoids card-name lookup but stock libasound still opens the card control device. The candidate grants only its matching control node behind an inherited filter |
| Combined Ubuntu service launcher | Profile v4 passed at 15:17:03 UTC | Kernel policy persisted and reverified before the exact invocation was released; eight TCP-port boundaries, IPv4 UDP/IPv6 policy, non-root/no-capabilities, direct/conversion actual guarded PCM and filtered silent playback passed. Exact cgroup termination, empty ownership and all four slot-7 endpoints closed |
| Combined Ubuntu launch crash recovery | All three stages passed at 15:36:14 UTC | Exact broker child killed while gated, after policy attachment, and after release. Unreleased payloads stayed stopped, policy survived owner death, fresh authority recovered exact resources, empty manifest/cgroups and the legacy deployment were verified |
| Ubuntu log-reader lifetime | Passed at 15:35:35 UTC | Actual armed child with idle output and no broken-pipe dependency died with its exact parent; the reader also passed the three-stage recovery check |
| Output control and offset arithmetic refinements | 15 RAOP transport, 46 Cast transport, 21 RAOP sequence and 556,952 arithmetic cases passed under sanitizers | Actual composed backend source and GCC build; stale/duplicate response, malformed control-message and signed sequence-overflow preimages reproduced. Cast transport checks exercise outbound control, not a Cast input receiver |
| Two-zone observation preparation | Independent six-second digital baselines passed for both virtual PCM directions | The full grouped program/speech check is still pending. Test isolation now uses a disconnected parent namespace; an actual VM probe reproduced `ip netns exec` hiding cgroup mounts and verified held-FD network-only `nsenter` preserves them. No final group timing claim |
| Native input startup capacity | Stock two-second capacity failure reproduced; maintained bounded six-second capacity passed actual-source sanitizer checks and the Linux startup observation | The two-zone 18:08 UTC run had zero worker drops in both rooms. It separately failed because onset detection included leading silence; the corrected observer has offline regressions and awaits a new full run. The larger capacity covers the declared common presentation horizon and per-output offset range; it does not change OwnTone's output scheduler |
| Grouped PCM failure observation | 63 focused observer, cleanup and supervisor checks passed in 2.73 seconds at 18:31 UTC | Confirmed onset uses the earliest individually audible buffer, preserving earlier gaps even when window confirmation arrives later. Strict per-buffer checks continue afterward; failed captures retain bounded PCM, absolute timestamps and the offending block. No group timing or hardware acceptance claim |
| Startup logging and selected-output volume | Concrete startup failure reproduced and targeted runtime regressions passed | OwnTone foreground logging uses `/dev/null` instead of reopening journald's socket as a file. Output selection reapplies the current intended master volume after OwnTone restores saved per-device values; failed acknowledgments retain ownership and trigger reconciliation |
| Descriptor-only Bluetooth Python path | 141 actual Linux tests passed in 0.62 seconds at 18:44 UTC | Real pipes, Unix SCM_RIGHTS and SEQPACKET with mock endpoint/controller; no host system-bus or speaker access. Covers restricted-controller capabilities before/after Open, transferred descriptor cleanup, adjacent and cumulative clock bounds, and exact cancellation ownership. Three earlier lifecycle failures reproduced Python 3.10's simultaneous-ready/cancel race. Private daemon/controller integration later passed at 19:32 UTC, and separate v5 publication/confinement checks at 21:20 UTC. Combined broker/OwnTone/BlueALSA audio and paired-radio checks remain open |
| Maintained BlueALSA private build | Actual Ubuntu GCC build, 41 C sanitizer cases, stock premature-ACK reproduction and linked SBC history reset passed at 18:42 UTC | Private `/opt/shiri-v2-bluealsa-review2-deps`; patch SHA-256 `964c756acfc82d8ab6877e9be3741fdf754b835fff1b7024b7347c1be46705a2`, binary `0c3c6f106b196b4ac8ac33599feb43b368ac81293b0215f7818c7988019ed226`. The first compile caught a cleanup/setjmp variable lifetime warning, corrected before retry. Existing ALSA files/packages, service invocation identities, policy and PCM-open states were preserved; no host daemon or policy activated |
| Full-source FFmpeg conversion review | Actual configured OwnTone outputs/transcode/framed fixture compiled; execution exposed a missing-timestamp arithmetic overflow under UBSan | The 18:47 UTC observation aborted at `packet_prepare` before a usable conversion report. The later configured FFmpeg acceptance at 19:31 UTC passed all 20 cases and source-history retirement; the twelve-layer composed build also passed. The earlier failed observation remains recorded. Stub-only checks do not establish converter correctness |

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

## Actual conversion and receiver review, 19:31 UTC

The composed OwnTone conversion acceptance passed on Ubuntu with real FFmpeg,
full `outputs.c`/`transcode.c` and the framed output adapter under ASan/UBSan.
All 20 rate/channel/format/warmup cases passed. The actual mixed-format preimage
hid a later converter behind an empty first converter and retained 8, then 16,
bytes. The corrected dense output array exposes the later converter and drains
all bytes. Actual wire gain tests preserved the original samples. A real
source-seal reset left zero retired FIR bytes; its unreset control retained 116.
This is software conversion evidence, not a physical output or phone sync test.
Report: `/tmp/shiri-v2-resampler-acceptance5-result.json`; archive SHA-256
`b46d2bc7693a4c54720b9a21a6fc0a788f10c7528f91d9441ca778ed7c1efa6b`.

The actual private BlueALSA daemon passed at 19:32 UTC using review3 binary
`ffa7d0d7ccf06e04699a3f98e149433a4eec2f51b014cee80f2ef12f69d66c32`,
compiled with STATE_DIRECTORY support, a private D-Bus and mock BlueZ transport.
Its real SBC thread emitted 10 RTP packets (4,385 bytes); completed DropSync
took 0.201 ms. Seven legacy or malformed controller commands were rejected.
The daemon ran as UID/GID 65534 without capabilities, with no-new-privileges and
a Unix-only seccomp guard. All seven resource cleanup checks passed. The first
attempt exposed a missing introspection method in the mock, corrected before
retry. This does not exercise a radio, house speaker or combined broker path.
Report: `/tmp/shiri-v2-bluealsa-private2-result.json`.

The grouped runtime exposed a separate real Shairport startup failure: normal
no-backend-options startup calls the custom backend with argc -1. The adapter
previously rejected that invocation despite a valid private socket and peer UID.
It now admits exactly -1 or 0; malformed negative and positive option counts
still fail. Six focused timing/configuration checks passed. The grouped run is
retained as failed; receiver startup readiness and a rebuilt whole-path run
remain required. A combined backend build then stopped on an omitted new test
file in the archive, before OwnTone installation. Its corrected complete
archive exposed two strict GCC signed-comparison errors. Explicit unsigned
bounds fixed both. A fresh complete build, all required checks, installation
and trusted-prefix validation then passed at 19:48:03 UTC.

The exact stopped candidate identity migration passed at 20:08:46 UTC. It held
the installation and broker locks, preserved every original name, UID, GID,
GECOS, home and shell for 42 accounts, then atomically published the v2 map with
eight separate bridge accounts. All 50 UIDs/GIDs are distinct. All four task
UID fields were quiescent and the host network, ownership manifest, legacy
PID2444 and audio slots 0/2 remained unchanged. A root-only copy of the v1 map
was retained before publication. An earlier refusal found the old empty root
lock had mode 0644; an exact held-inode repair under both locks changed only
its mode to 0600. Startup now refuses unsafe or replaced existing lock files.
Report: `/tmp/shiri-v2-identity-migration3-result.json`.

Actual receiver readiness passed seven vectors and all cleanup gates at
21:19:44 UTC under the corrected production capability set, without CAP_SYS_PTRACE.
Exact MainPID IPv4 listeners passed; same-UID child-only listeners, loopback
bindings, no listener, MainPID exit and namespace mismatch failed at the
intended gates. Exact units stopped, the held namespace was removed, and the
ownership manifest became empty. Earlier attempts retained their raw host
baseline differences: only six finite lease timers decreased over the measured
interval. The repeat admits those countdowns within measured read windows and
one second of kernel quantization, while keeping other host and legacy
identities exact. Report: `/tmp/shiri-v2-readiness-review5-result.json`.

The live bridge test caught a normal zone UUID plus Bluetooth role exceeding
the old unit-name grammar. The corrected grammar explicitly parses the
installation, canonical zone UUID or sender, role and launch nonce. The next
attempt passed identity admission but failed during the manual fixture's
systemd mount setup: it redundantly masked `/dev/snd` after PrivateDevices
created a device tree without that directory. Removing the fixture-only mask
preserved real control-node open-denial checks and changed no worker policy.
Report: `/tmp/shiri-v2-publication-review2-result.json`.

The root broker separately needed CAP_FOWNER to set modes on admitted
worker-owned directories, FIFOs and held IPC/timing inodes. CAP_CHOWN and
CAP_DAC_OVERRIDE do not authorize those chmod operations. The corrected broker
set contains CAP_FOWNER and CAP_FSETID, with no CAP_SYS_PTRACE; child units still
receive their own empty capability sets. The latest publication run passed all
eleven gates at 21:20:11 UTC: actual v5 bridge credentials and confinement, held O_PATH and
durable publication, a read-only mount of only the socket, exact peer
credentials in both directions, resistance to replacement at the old pathname,
and denied peer-root, bus, device and credential access. Both exact services
stopped before the protected inode was removed; ownership became empty and the
host and legacy deployment were preserved. This is real kernel/service/socket
evidence with a fixture producer, not combined BlueALSA audio or radio evidence.
Report: `/tmp/shiri-v2-publication-review4-result.json`.

The grouped test confirmed why CAP_FSETID is also necessary: a configuration
directory requested as mode 2750 became 0750 under the previous broker set.
Its replacement command file consequently became root:root 0640 instead of
root:output-group 0640. The same missing flag would affect the production
audio input directory, whose new sockets must inherit the receiver group.
Directory preparation now verifies the exact inode, owner, group and mode
after the operation and refuses a silently stripped flag. The test's command
publications set the intended group and mode on the held temporary descriptor
before atomic replacement. Both readiness and publication passed again under
the final broker mask 0xa034fb; all five worker capability sets remained zero.

The grouped test's next attempt failed before broker startup when its isolation
harness tried to read PID1's network namespace without CAP_SYS_PTRACE. No
production module makes that read. All inner and supervisor cleanup checks
passed. The harness is being changed to retain the original namespace descriptor
before entering the disconnected test network. That admission passed in the
next run, which stopped while the fixture tried to chmod its seeded database
after transferring ownership. File modes now precede the API ownership handoff.
All cleanup checks passed in both attempts. Reports:
`/tmp/shiri-v2-native-group-supervisor-review18-result.json` and
`/tmp/shiri-v2-native-group-supervisor-review19-result.json`.

An additional timing review found that the generated non-framed OwnTone
configuration used a 500 ms start buffer despite accepting corrections down
to -2000 ms. The native ALSA backend can retain such a value without using it
when its magnitude exceeds the buffer. Every generated room profile now uses
the pinned backend's 2250 ms default. The actual composed input and ALSA
scheduling statements passed all 4001 accepted integer offsets under GCC
ASan/UBSan on Ubuntu at 21:27:14 UTC: none were ignored and every native final
deadline remained P + 4 seconds + offset. The exact 500 ms preimage failed
1500 cases, -501 through -2000 ms. This proves the software scheduling seam,
not acoustic timing or Cast synchronization. Report:
`/tmp/shiri-v2-common-buffer-review1-result.json`.

Real Opus/WebRTC testing reproduced a speech-close cancellation leak: the old
worker marked disposal complete before its first awaited transport close.
Cancellation left a connected peer which subsequent cleanup skipped. Cleanup
now belongs to a tracked, shielded worker task, with a finite admission bound
and a bounded shutdown join. Speech-only cleanup faults do not change music
health or invoke source/player commands. Expiry removes speech ownership and
continues the volume tick without awaiting a stalled peer. Focused tests include
actual connected transport closure after request cancellation, receiver and
negotiation reentry, Unix RPC disconnection and continued full-volume native
program samples. All 85 focused tests passed on Ubuntu at 21:21:27 UTC and on
the Mac. Replacing only the shielded await with a direct await makes the actual
connected-peer regression fail. The combined playback run remains open.
Report: `/tmp/shiri-v2-speech-suite1-result.json`.

The 21:27–21:29 UTC grouped run reached both rootless zone workers, receiver
readiness, independent digital baselines and shared-clock playback. Its partial
timing measurements found zero relative offset on the 1 ms analysis grid and
zero early/late drift over 15.756 seconds. Baseline-corrected absolute offsets
were -5 ms and -1 ms, inside the separately declared capture bounds. These are
partial software measurements; the complete grouped acceptance failed.

During the speech stage, untouched zone B's final observed buffer contained
576 normal 440 Hz music frames followed by 384 exact stereo-zero frames: an
8 ms silent tail. Previous buffers retained continuous phase and unity gain.
The per-buffer check remains unchanged. Recovered, identity-scoped journals
show B's native ingress raising TimingError and retiring its source at the
interruption; its producer then received BrokenPipeError. The current wrapper
discarded the specific timing reason, so packet-clock validation and producer
evidence must be retained before a repeat. The corrected diagnostic path now
records an allowlisted reason, bounded packet timing/frame counters and the
pre-message fence state before exact-source retirement. Its real Unix socket
tests, native timing and source-ownership regressions passed all 75 cases on
Ubuntu at 21:58:31 UTC, including rejection of a wrong-UID SEQPACKET peer.
It does not relax admission or correct the still-unidentified failing packet.
Report: `/tmp/shiri-v2-ingress-suite1-result.json`.
All fourteen inner cleanup checks
and both supervisor checks passed, preserving the legacy app and host baseline.
Reports: `/tmp/shiri-v2-native-group-supervisor-review21-result.json`,
`/tmp/shiri-v2-native-grouping-review21-result.json` and
`/tmp/shiri-group21-owned-journal-review.json`.

An independent extraction of the actual OwnTone ALSA write path also reproduced
sample loss after a short positive device write: accepting 576 of 960 frames
discarded the remaining 384, both directly and from the prebuffer. This is a
separate confirmed backend defect; the retained group evidence does not prove
it caused that interruption. The maintained `-alsa1` layer now retains raw
tails, consumes only acknowledged frames, applies current gain on submission
and fails explicitly on queue exhaustion. Actual source cleanup frees pending
tails before a successor can play them.

Final review also reproduced initial buffering restarting after the uint32
playback counter wraps, approximately every 24.85 hours at 48 kHz. A real
streaming callback wrapped a reachable counter to 704, queued all 960 input
frames and submitted none while reporting success. Startup completion now
belongs to the playback session, with widened initial arithmetic and unchanged
modular sync calculations. Direct/queued rollover, subsequent low-counter
callbacks, 250/2250 ms startup boundaries and fresh playback sessions passed
actual-source Clang/GCC sanitizers. The combined twelve-layer Ubuntu build
passed at 22:15:29 UTC, before starting the next grouped run. Source takeover,
completed speech continuity and gated daemon recovery remain unverified in the
failed group21 run.

Group22 ran from 22:20:46 to 22:21:47 UTC with the rebuilt backend and failed
the relative timing gate before speech. Its retained final captures are
byte-identical, SHA-256
`df2f24b090d18afcc771b4660b8d2d15c8740957d74a8fe7ff4b9c42bb9bf6ed`,
with 1301 blocks each. Independent review reproduced a measurement defect:
the observer selected buffer 500 in both captures, whose recorded first
timestamps differed by 3.897 ms, then extrapolated that one timestamp across
the whole analysis window. This manufactured a 4 ms result despite later
same-index timestamp differences having a -0.059 ms median. The measurement
now uses all contributing actual buffer anchors, validates every recorded
anchor before window selection, and carries the immutable actual sample/block
continuity receipt into analysis. All 110 focused observation tests passed,
including the old single-anchor and missing-receipt preimage probes, genuine
5 ms offsets, drift, missing/repeated frames and timestamps moved outside the
window. Independent retained-data replay measured 0 ms in full, early and late
windows; the 2 ms gate, unique-code correlation and drift checks remain
unchanged. This retrospective replay does not promote the failed run. The
fresh grouped acceptance remains pending.

Its new failure capture succeeded in 105 ms, retaining all fifteen exact unit
receipts and both producer statuses with no capture errors. Both producers
were healthy; exact current invocation journals contained no native timing
fault, underrun or xrun. Historical room log tails are now explicitly labeled
as lacking current-invocation attribution. The diagnostic artifact is 351496
bytes with SHA-256
`031b3d37f4cb43b69f9f0c7f83c2d1d2f79444014c5fed939475eac9d32987d3`.
All cleanup checks passed. Reports:
`/tmp/shiri-v2-native-group-run-review22-result.json`,
`/tmp/shiri-v2-native-grouping-review22-result.json` and
`/tmp/shiri-native-group-review22-diagnostics.json`.

The fresh grouped run at 22:51:52–22:53:09 UTC used the reviewed observer,
exact twelve-layer backend build and all 222 hash-verified candidate files.
It measured zero relative offset on the 1 ms analysis grid in full, early and
late windows, correlation 0.999394, and zero early/late drift. The observed
program matched both declared presentation times at 0 ms; independent digital
capture correction yielded 0 ms and 1 ms inside the predeclared bounds.
Target A passed audible speech/music ducking and silent-input restoration;
untouched B passed every sampled PCM block through that interval.

The complete run still failed before resumed-voice/close/takeover completion.
Identity-scoped diagnostics proved B's native RAW-clock mapping bracket was
1,347,960 ns, exceeding the unchanged 1 ms gate. Its prior frame/sequence fence
was continuous; the exact producer status independently recorded the same
maximum bracket and subsequent broken pipe. The worker rejected that mapping,
retired its source and exited. A remained alive. This proves transient producer
clock-sampling preemption, independently of the earlier measurement defect and
ALSA partial-write correction. The receiver and synthetic producer are being
changed to retry the complete clock triple at most four times within a total
five-millisecond budget; accepted brackets remain at most one millisecond and
native frame/presentation identity is preserved. Exhaustion must fail explicitly
without sending an invalid mapping. The repaired actual-source sanitizer checks, Ubuntu backend build and
matching-candidate full Mac/Ubuntu suites have since passed. Fresh grouped
validation remains open; the earlier full-suite passes predate the repair.

All recorded resource cleanup checks passed, including exact units, namespaces,
manifest, Loopback slot and original host/legacy baseline. The monitor's same
RPC failure remains in the error receipt. The diagnostic artifact is 390594
bytes, SHA-256 `3ca7c35d0e8ac0b4bccbe0a77308a00d12e6175ba213982463c653c2971dfe0f`.
Reports: `/tmp/shiri-v2-native-group-run-review23-result.json` and
`/tmp/shiri-native-group-review23-diagnostics.json`. This is partial software
evidence, not completed grouped, stock-phone or physical-speaker acceptance.

Group24 stopped before playback because the new startup marker check rejected
Shairport's actual dash-separated feature string. The check now validates the
exact backend token and pinned feature suffix, excludes configuration-path text,
and retains the actual `7bad231-dirty-AirPlay2-smi10-OpenSSL-Avahi-ALSA-shiri-timed2-soxr-metadata`
format as a regression. All 84 focused runtime checks and the matching full
Mac/Ubuntu suites passed before retry. All early-run cleanup checks passed.

Group25 ran at 23:22:39–23:24:09 UTC using the same frozen backend9 binaries and
223-file candidate. Relative final digital alignment was 0 ms on the 1 ms grid,
correlation 0.999595, with 0 ms early/late drift. Independently corrected
presentation errors were 0 ms and -1 ms within predeclared bounds. All four
speech transitions passed: audible voice with .2 music ducking, silent-input
restoration, resumed voice, and closed-session restoration. Both sampled
playheads advanced from 290 ms to 45,530/45,540 ms without captured program gaps
or wrong-zone voice. This establishes the exercised one-session digital
music/TTS interval, not twenty-session, stock-phone or physical acceptance.

The complete test failed after explicitly retiring A's initial observer and
requesting source takeover. Its health RPC returned `runtime_unavailable`;
exact diagnostics show both workers still alive and A's successor producer
streaming under epoch2. No native timing fault was recorded, and B's original
sticky PCM observer remained continuous. A timeout, connection or permission
failure cannot yet be distinguished because the manual receipt omitted the
RPC cause. Safe bounded cause metadata is being added before retry; no deadline
or playback gate is widened. All resource cleanup checks passed. Reports:
`/tmp/shiri-v2-native-group-run-review25-result.json` and
`/tmp/shiri-native-group-review25-diagnostics.json`; diagnostic SHA-256
`c739103055e6bc16dd8543f7c82d0289a17a64af6f9a8244e8c9e0ccca4ac87f`,
387902 bytes and fifteen exact unit receipts.

The takeover health failure has since been reproduced with the actual local
NativeController, SourceActor, private Unix native sockets and production RPC
server. After successor admission, the closed old native socket still had a
pending thirty-second receive registered in the event loop. The first health
request timed out while subsequent requests completed normally and the
successor stayed healthy. Five repetitions reproduced this behavior. The stale
reader can also retain old connections until timeout and consume the eight-
connection admission budget. The independently reviewed repair tracks the exact
receive task, cancels/joins it before normal socket closure, and removes its
owned still-open registration before synchronous abort. Real-socket regressions
passed first-request production health RPC after normal quiescence, synchronous
abort and watchdog abort; twelve takeovers beyond the eight-connection limit;
stale PCM rejection; untouched second-zone PCM; canceled cleanup; and shutdown.
The exact old implementation fails the first-health regression with its receive
timeout. Independent focused review passed 32 cases with two Linux-only skips
on macOS. The frozen full Mac suite passed 1,462 cases with 50 skips; the same
240-file Ubuntu snapshot passed 1,509 cases with three skips. Full grouped
playback subsequently passed in group26, as recorded below. The RPC deadline and
timing gates remain unchanged.


Group26 completed successfully at 23:57:11–23:58:44 UTC on the frozen 240-file
baseline and backend9. Final digital alignment was 0 ms on the 1 ms analysis
grid, correlation 0.999339, with 0 ms measured early/late drift. Independently
corrected presentation errors were 2 ms and 1 ms within the predeclared
26.30/26.92 ms bounds. All four music/speech transitions passed. The previously
failing source takeover also passed: A's new 660 Hz program appeared in final
PCM and its retired 440 Hz program was absent, stale source events left the
successor intact, and ending A preserved B's exact original owner. B's sticky
observer checked 2,515 music blocks without a failed block. All fifteen inner
cleanup checks and both supervisor checks passed, including empty ownership,
closed Loopback slot, removed namespaces and preserved original host/legacy.

This is a complete exercised synthetic-source/kernel-digital baseline, not a
stock-phone grouping, physical-speaker, native Cast input, repeated twenty-
session or abrupt-failure acceptance claim. Source archive SHA-256
`368bb50c821c73f9b0fe58b6f8256b4fd7969e122e9079d2d2fb7ab1079a54f9`;
report `/tmp/shiri-v2-native-group-run-review26-result.json`. The exact reader
repair is `91a047512674936839808b69259947f20ba86d6dddc8b23561db0fd1e981e72e`.

Current final PCM also exposes a responsiveness limit: speech onset is about
4.4 seconds after request/media startup, and silent restoration about 4.7 seconds.
The explicit common relay horizon is four seconds; OwnTone's 2250 ms buffer is
subtracted at input and restored at output rather than added again. Reducing
that horizon requires measured startup/source-lead qualification and all
current timing/continuity gates. A fixed shared three-second experiment then passed the complete group27
baseline at 00:07:11–00:08:39 UTC on October 1. Relative digital alignment and
measured drift remained 0 ms on the 1 ms grid, correlation 0.999630; all speech,
takeover, END and cleanup gates passed. Speech onset was 3.385887 seconds,
resumed speech 3.357028 seconds, silent restoration 3.759734 seconds and closed
restoration 3.425677 seconds. Only `RELAY_DELAY_NS` changed in the separate
240-file experimental snapshot; source archive SHA-256
`dcccb7934db3592d1226f5fbffb086866847c947072033533b73700e1c29ffb9`,
report `/tmp/shiri-v2-native-group-run-review27-result.json`. This qualifies
that synthetic active-music baseline only. Cold/warm idle speech, final-output
offset extremes and actual source-lead behavior remain required before adoption;
the working production candidate retains the four-second policy. It does not
establish a universal safe minimum. The receiver currently
does not advertise the extra relay horizon to a phone's video timing path;
stock-phone video timing and grouping with direct native speakers remain
unverified. These limits do not remove the required receiver/grouping behavior.


The first explicit twenty-session-per-zone attempt, stress28, ran at
00:19:34–00:21:18 UTC on October 1 and failed during the first simultaneous
speech onset. Zero sessions were accepted. The strict peer-marker check rejected
B's final PCM buffer 2896; actual adjacent buffers and their exact timestamps
are retained for signal analysis. All fourteen inner cleanup checks and both
supervisor checks passed, with no cleanup errors. Full report:
`/tmp/shiri-v2-native-speech-stress-run-review28-result.json`. Its fifteen-unit
diagnostics are 419394 bytes, SHA-256
`293ea9cddea609942c6d8c9cb6a59ea86dbb54ea78f8f1cd4e1defc2a9c5fd63`.

Independent review also found two fail-path harness cleanup defects: failed
HTTP teardown could skip local peer disposal, and failed concurrent offers
could leave sibling negotiations running. Local repairs and cancellation
regressions now pass; the corrected full stress run remains pending. A
separate phase-dependent short-tone vector escaped midpoint-only estimation;
full-buffer voice-envelope detection is being reviewed without raising the
fixed four-count peer-marker limit. Independent actual-buffer analysis then reproduced B2895 from A's pure music
using the unchanged piecewise ducking law, within 0.174 integer-sample RMS and
maximum two counts, without any speech. The polynomial detector nevertheless
reported foreign-marker energy, proving that legal gain corner can cause its
false positive. B2896 onset remains separately qualified. A controlled emitted-
Opus decoder reference is being prototyped to distinguish own codec onset from
foreign speech; no leak threshold or production gain is relaxed. This run does
not establish a production routing fault or prove twenty-session acceptance.

The exact-payload reference prototype then passed 134 independently executed
local checks on a frozen private source. It uses one immutable source mapping,
the unchanged native gain law, and whole-buffer residual/foreign limits of four
counts. Checks include actual retained stress28 buffers, public AVPacket/RTC
decoded-prefix equality, genuine short foreign-speech injections, gap/drop and
cancel/bound cases. Review corrected the snapshot's writable-array backing to
immutable bytes. Storage plus the one snapshot is bounded to 16 MiB per peer,
excluding native codec/object overhead. Helper SHA-256
`323e1a1cd33fd91dc61740b4fe55ffe13a117ffab3b9d84542531b01e600cd35`.
This verifies the reference prototype, not its still-pending stress integration
or a new twenty-session VM run. Historical stress28 payloads were not retained;
its replay is diagnostic rather than exact-payload admission.

The separately frozen reference integration then passed independent review:
365 focused checks passed with one Linux-only skip in 13.17 seconds, followed
by the full Mac suite above. Its live observer continues while bounded analysis
runs in a thread; cancellation checks actual worker retirement separately from
the asyncio future. Both zones' complete waveform, restored tail and public
sender packet-count evidence must pass before either receives a session count.
The 750 ms restore observation covers the unchanged EOF lease and native gain
restoration; fixed residual/foreign limits and work bounds are retained.
Reference model SHA-256
`2d64f3049f6b965ff03350353a7b31acbd584cba9c599d1f1003abf53c345df4`;
stress helper
`84effddafcbd2eb84bf403eb0bfc0b61c5a9e7206849222ec0ac62d2a8505473`.
Independent receipt
`/tmp/shiri-native-speech-integration-independent-review-result.json`.
The actual twenty-session-per-zone Linux run remains pending. No production
mixing or routing behavior changed in this test-reference refinement.


The separate abrupt-failure run, fault29, passed at 00:37:52–00:39:53 UTC on
October 1 using the frozen 250-file snapshot and backend9. After exact owned
OwnTone and audio-worker SIGKILLs, Shiri's production monitor rebuilt A in
approximately 8.5 and 8.7 seconds. Manifest reservations remained intact, held
cgroups were thawed and owned descriptors closed. A's saved intent was restored;
its original sender connection ended and a fresh synthetic receiver/producer
was explicitly connected. This does not claim native-phone automatic resume.

B retained its original capture, source, unit/shared-sender identities, output
selection, volume, item and advancing NPT. Its sticky observer checked 3,947
music blocks, with 506 retained control samples and no failed block. Both fresh
A programs reached final PCM; the later source takeover and END checks passed.
Initial grouped alignment and measured drift remained 0 ms on the 1 ms grid,
correlation 0.999761. All fifteen inner and both supervisor cleanup checks
passed with no errors, preserving the legacy and original host baseline.
Report `/tmp/shiri-v2-native-zone-faults-run-review29-result.json`; archive
SHA-256 `15e4a757cd36f0e44d01612305bb7be17ff1a92c5db69c57d9f687e808ad13ad`.

A separate failed-reconnect review reproduced a cleanup edge outside fault29's
successful reconnection path: a retired synthetic producer handle could forget
the canonical manifest reservation of a newly recovered Shairport receiver.
The narrow harness repair snapshots the old unit's complete identity, stops
only that unit, requires it dead, then compares the current reservation after
the await before forgetting it. Fresh state handles remain untouched. Eleven
local cleanup checks cover successors appearing before/during stop, changed
metadata, stop errors, cancellation and a unit that remains alive. Independent
review passed on an exact private snapshot: 156 focused checks passed with one
Linux-only skip and clean lint. Harness SHA-256
`1b2b28004c0767faa681cbfbc37e836fed73695b8116f03ba3a6978fb76562a8`;
cleanup-test SHA-256
`93e560880451a012acef45b2c5355faff5188e314f7379832f81df8dc926fb86`.
A coherent 250-file local overlay changes only those two files from fault29.
The subsequent explicit 253-file latency overlay passed the full Mac suite,
1,644 tests with 51 platform skips in 54.08 seconds; its Linux suite and next
VM scenario remain pending. The historical fault29 source and proof remain
unchanged. The exact source mapping is retained in
`/tmp/shiri-v2-group30-latency-freeze-result.json`.

The next latency matrix preserves B's original program and sticky observer.
Each A row retains its configured offset and the independently measured
kernel-capture calendar bound. The initial grouped receipt retains its 2 ms
relative gate; the later constant B carrier cannot establish a new relative
alignment for each matrix row. The 1 ms analysis grid is not a claim of 2 ms
absolute timing accuracy. Four-second control and three-second experimental
snapshots remain separate.

Independent review of the frozen latency helper and grouped-harness hook
passed 196 focused checks with clean lint. Post-stop analysis may skip only the
current callback-age check after capture shutdown is proven; recorded gaps,
sequence, caps, content and offset failures remain mandatory. Restored A's
capture receipt must be retained before takeover publication. The helper and
hook remain frozen while the separate speech-reference integration proceeds;
no new Linux latency-matrix result is claimed.

The same snapshot passed actual offline Python3.10 package-building checks
using seven official hash-verified universal wheels, including refusal of
missing/tampered backends and source-only runtime dependencies. This proves
those isolated Python packaging paths, not an unmodified full installation on
a clean Ubuntu OS. The clean-OS and supported native Cast receiver gates remain
open. The current main timing policy remains four seconds.

Preparation for a genuinely fresh Ubuntu installation is separate from the
existing candidate VM. Canonical's frozen Jammy ARM64 release image was verified
against its signed checksum using the installed Ubuntu cloud-image keyring and
full signing fingerprint `D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81`.
The 705,299,456-byte qcow2 SHA-256 is
`04f2ca6af841918df1fa1aaba485f2b5f3d451f90459e48c066bbd70ebed174f`.
The image is downloaded on the Mac. A separate headless VM subsequently booted
using a new disk, unique VM/MAC, fresh UEFI state, no shares/USB/sound/bridge,
restricted user-mode networking and only loopback SSH. Offline installation
artifacts and full install/reboot/rollback remain pending. The existing stopped Ubuntu bundle
shares the live VM's bridged MAC and must not be booted unchanged.
[Official frozen image release](https://cloud-images.ubuntu.com/releases/jammy/release-20260926/)

The separate Linux ARM64/Python 3.10 wheelhouse completed at 01:48:52 UTC on
October 1: 38 runtime/audio and seven build wheels, 70,836,975 wheel bytes.
Every version and digest was checked directly against the original locks.
Offline dependency resolution and target wheel metadata closure passed;
a corrupted-copy check refused the invalid wheel without downloading files.
Transfer archive SHA-256
`a6bab8ba551d4296e33ce9bad0a25baeb9f1995c9e4886a3fed4ead7aa5dd0f5`;
receipt `/tmp/shiri-arm64-py310-wheelhouse-nk2kp_dn/proof.json`.
No packages were installed and no ARM64 imports or system dependencies were
tested by that preparation. APT/native-source closure and the actual clean-OS
installation remain pending.

The first headless attempt failed before guest authentication because the
fixture declared the bundled QCOW2 UEFI variable template as raw. Its failed
receipt and source were retained. The corrected explicit format booted at
02:09:27 UTC and completed at 02:10:06 UTC on October 1: Ubuntu 22.04.5 ARM64,
kernel `5.15.0-194-generic`, unique guest boot ID
`78e3974f-dcd0-417e-87e7-fe21f42c8b9d`. Cloud-init completed without errors;
the fresh pinned SSH host key authenticated, and the independent guest shut
down with QEMU exit code zero. The original signed image remained unchanged,
the owned process was reaped, and the existing VM's host process identity
remained unchanged. This does not establish the old guest's current internal
state or a successful Shiri installation.
Receipt `/tmp/shiri-clean-os-first-boot-result.json`; retained initial failure
`/tmp/shiri-clean-os-first-boot-attempt1-result.json`.

The fresh guest has no `/opt/shiri` installation and its kernel lacks an
available `snd-aloop` module. The clean installation must verify and satisfy
that kernel dependency before the required final module load can pass.
No application install or new kernel/device acceptance is claimed by this
first-boot observation.

At this point UTM's read-only control requests time out, and SSH to the guest's
last verified address is unreachable. The host still shows the exact existing
QEMU process, but that does not establish the guest's current state. No app or
VM restart, termination or duplicate test run was attempted. Software review
and offline preparation continue while this access limitation is investigated;
it must not be interpreted as a successful run or a stopped VM.

## Historical measurable release-gate snapshot

The following combine the required product gates with measurable robustness
criteria. A pending gate must acquire an actual result, environment and artifact
before it can be marked passed. Backend limitations do not remove a requirement.

| Gate | Acceptance criterion | Status at this historical checkpoint |
| --- | --- | --- |
| Automated regression | Full Python and web/browser suites pass for the candidate; lint and diff checks clean | Coherent348-file Mac candidate passed3146 tests/81 platform skips with Ruff and shell checks. Actual70's one-file cancellation-test repair passed the complete Ubuntu Python3.10 suite:3223 passes/four skips. Source/package/native/dependency admission and reboot passed. Later integrations require a fresh coherent suite |
| Pinned runtime | Builder pins revisions and patch hashes; preflight requires exact reviewed fifteen-layer OwnTone build, Shairport `shiri-timed2` AirPlay2/SMI with bounded clock sampling, compatible NQPTP shared memory, verified daemon identities, confinement helpers and GI/plugins | Actual68 built and installed the fifteen-layer OwnTone backend and exact50-file package; actual70 preserved their identities through full regression and fresh reboot. Native credential/source sanitizer checks and installed broker preflight passed. Final route qualification remains open |
| Installed services | Rootless authenticated API persists revisions; installed broker restarts after owned SIGKILL with host-visible resource identity; disable removes owned resources and leaves host baseline unchanged | Nine earlier-profile real Ubuntu candidate checks passed. Current separated-worker repeated lifecycle, installed-service replay and fresh-host installation remain open |
| Least-privilege daemons | Root broker owns setup/cleanup; backend/audio workers run under separate nonroot room credentials with minimal capabilities, read-only configuration/executables and narrowly scoped writable state; hook identities permit only bounded signals for their exact room | Distinct per-room nonroot identities and actual kernel capability/device/bind checks passed; workers retain zero capabilities, with the private PTP daemon limited to its required bind capability. Combined group26 playback and fault29 per-zone recovery passed in the exercised cases; broader lifecycle/failure validation remains open |
| Native AirPlay 2 input | Stock iPhone discovers each enabled zone and uses its existing controls to play through assigned outputs without a Shiri phone app | Candidate receiver implemented; stock-phone whole-path test pending |
| Native Chromecast input | Future stock-phone-compatible receiver with authenticated audio/control | Explicitly deferred by the user on October1; does not block the current software release |
| Competing source ownership | Repeated competing AirPlay sessions follow an explicit policy; stale volume/disconnect events never alter the successor | Source actors, native AirPlay hooks, framed PCM ownership and stale-event fencing implemented and tested. Group26 source takeover and stale-event checks passed on the kernel-digital path; repeated takeovers remain open. Future Cast adapters use the same boundary |
| Native iPhone grouping | Multiple zone receivers are selected using native controls; measured final outputs preserve supported sync through startup, regrouping and long playback | Earlier group26 completed the synthetic digital path with0ms relative alignment/drift on the1ms grid. Current low-buffer MUSIC69 retains both complete waveforms but fails the unchanged2ms group limit with4ms relative offset and zero drift. Minimum defaults remain unpromoted. Regrouping/long digital playback remain open; stock-phone and acoustic timing are deferred |
| Stable local hardware identity | Admitted physical device stays stable across restart/reordering; missing or changed identity rejects playback rather than selecting a successor | Opaque root-owned enrollment/fingerprint resolution, held sysfs/node identity and actual-open PCM guards implemented; direct/plug Loopback opens and wrong-identity rejection passed. Physical USB unplug/reordering remains deferred |
| Real input/output path | AirPlay source → Shairport private timed PCM → mixer → OwnTone → assigned output produces uninterrupted program audio for 30 minutes | Current-profile group26 completed the short synthetic full digital path; fault29 preserved B through A's two process crashes. Thirty-minute digital soak remains software work; stock-phone and physical 30-minute playback are deferred |
| Targeted speech | Two enabled zones; 20 sessions per zone including music overlap, conflict and cancellation; zero wrong-zone output; music advances without pause/seek/restart/reconnection or phone disconnect; gain restores smoothly | Earlier foundation rootless API/broker/worker final-PCM proof passed. Current hardened group25 passed all four speech transitions with advancing program and untouched-zone continuity, then failed in separate takeover validation. Group26 completed the grouped baseline; 20 audible sessions per zone remain software VM work; physical/phone continuity remains deferred |
| Device matrix | Actual AirPlay 1, AirPlay 2, Chromecast and configured paired Bluetooth/ALSA devices tested individually and in supported mixed groups | Pending; device availability required |
| Constant timing correction | Manual offset acknowledgment, stopped/playing application, retention across restart and measured sign/effect at fixed geometry | API application/restart/readback tests and all 4,001 ±2000 ms values in actual composed scheduling passed under GCC sanitizers; final-PCM offset sweep remains software VM work, acoustic sign/effect deferred |
| Calibration quality | At least 20 valid bursts across three restarts; before/after lag distribution, rejected attempts, jitter and 30-minute drift reported | Measurement analysis, recorded history, fresh verification and apply/rollback implemented and tested; physical capture gate pending |
| Room lifecycle | 50 enable/disable cycles and overlapping start/stop requests; no leaked owned namespace/process/slot; healthy room continues during another room failure | Fifty earlier-profile basic real Ubuntu cycles passed. Fault29 preserved the original B program through A's OwnTone/audio crashes, checking 3,947 music blocks and 506 control samples. Current separated-worker repeated/overlapping lifecycle and broader fault isolation remain software work |
| Crash recovery | Terminate broker at multiple startup/teardown stages; restart recovers owned resources without affecting unrelated host namespaces/processes or leases | Earlier two VM SIGKILL and installed-service restart proofs plus current launch gated/policy-attached/released SIGKILL stages passed. Real-room startup/running/teardown and durable-intent replay remain open; whole-broker restart intentionally interrupts/relaunches all owned rooms |
| DHCP/VM bridge | Verify lease renew/release, secondary-MAC reachability, mDNS, gateway/ARP behavior and exact host lease preservation | Actual kernel address/prefix transitions and isolated-server release/empty lease-table checks passed. House-router renewal/secondary-MAC reachability remain deployment gates; an earlier bridged-VM ARP failure remains recorded |
| Outage behavior | Speaker disappearance, backend exit, FIFO reader loss, broker unavailable and constrained storage remain bounded and visible; saved intent survives | Fault29 exact per-zone OwnTone/audio SIGKILL autoheal and unaffected-zone continuity passed on Linux; broker outage, storage and real-speaker disappearance remain open |
| Rollback | Restore the recorded backup and legacy checkpoint in a rehearsal; capture exact restoration steps and recovered state | Backup/checkpoint preserved; rehearsal pending |

No fixed mixed-protocol sync tolerance is claimed before measurement. Record
per-device and per-condition observed error, then choose a supported product
claim based on those distributions. OwnTone itself warns that Chromecast cannot
be precisely aligned with other outputs. [OwnTone Chromecast documentation](https://owntone.github.io/owntone-server/audio-outputs/chromecast/)

That output limitation must inform backend fixes or architecture changes.
Native iPhone grouping remains required; Cast input was explicitly deferred
by the user on October1. A synthetic OwnTone-to-Shairport input check is useful component evidence,
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

Run the independently reviewed twenty-session speech integration and the
cold/warm/native-source latency and offset matrix on Linux. Extend the exercised
source/crash transitions to the remaining lifecycle cases, finish actual
broker/OwnTone/BlueALSA routing, and exercise installed recovery and rollback.
The fresh-OS full installation, missing-module provision and installed API/runtime
startup after reboot now passed their recorded isolated scenarios. Short two-zone digital grouping, source takeover,
targeted speech and the two exact per-zone crash recoveries already passed
their recorded scenarios; those passes do not cover the remaining matrices.
The bind-policy/control-device corrections and separate bridge-publication and
receiver-readiness paths passed their real-kernel checks. Stock-iPhone,
router lease-table and physical-speaker checks belong to the later acceptance
phase, as requested by the user. Cast input is deferred for this release;
future receiver access does not by itself qualify an integration.

The staged calibration workflow and retained within-zone/cross-zone history
are implemented. Later recordings across AirPlay, Chromecast and paired
Bluetooth will determine whether a constant correction suffices or a backend
change is needed. A future Sendspin experiment must use the same device, load,
recovery and measurement criteria and keep the product domain/API intact.

Production replacement requires native AirPlay 2 receivers, safe input
arbitration, verified native grouping, and the outstanding Linux/physical
checks. The least-privilege daemon boundary has passed dedicated real-kernel
checks and the exercised grouped playback/per-zone recovery scenarios; broader
validation remains open. Component and
browser tests provide supporting evidence.
