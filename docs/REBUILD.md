# Software verification and release gates

The October 4 workspace refactors room speech concurrency, persistent PCM
transport, model recovery, output readiness and configuration reads, and removes
retired audio implementations and latency experiments. It has not been deployed.
[The repository review](REPO_REVIEW_2026-10-04.md) records the root causes,
implementation and current verification results.

The last recorded live deployment is the October 3 `duck1-warm1` release.
[The live handoff](LIVE_TEST_HANDOFF.md) and its linked checkpoints identify
exact artifacts, installed state and rollback files. Earlier design proposals
and experiment logs are available in Git history; they do not define the
current architecture or buffer defaults.

## Current behavior

[Product requirements](PRODUCT_REQUIREMENTS.md) are authoritative. Each enabled
room exposes native AirPlay 2 input and sends its program to exact assigned
speakers. Text speech has independent room playback, one active reply plus two
queued texts per room, and explicit exact-job interruption. A shared selected
model stays loaded on the Mac and recovers after failure. Enabled Linux room
outputs default to continuous readiness. Music keeps its source and timeline
through speech ducking, completion and cancellation.

[Architecture](ARCHITECTURE.md) describes owners and process boundaries.
[Timing](TIMING_RESEARCH.md) defines the single route policy: B40/H140 for local
or framed Bluetooth, B250/H350 for Cast/Pulse, B500/H600 for AirPlay at zero
offset. All enabled rooms share the largest required horizon; corrections may
increase it. These settings do not claim physical acoustic latency. Cast input
is deferred; Bluetooth input is excluded.

## Verification layers

| Layer | What it establishes | What it does not establish |
| --- | --- | --- |
| Python unit/integration suite | Configuration, routing, queue limits, cleanup, recovery, authentic socket behavior and native callback regressions | Complete running deployment or heard latency |
| Node and Chromium tests | UI state, independent room jobs, focus/drafts, polling, conflict and cancellation behavior | Physical audio |
| Extracted native C and sanitizer checks | Actual pinned callback arithmetic, sample content, clocks, lifecycle and failure cases | A complete backend build or a physical device |
| Isolated Linux suite | Python 3.10 compatibility, Linux-specific sockets/native libraries and portable integration | Host systemd, privileged namespace lifecycle or real speakers |
| Dedicated lab harnesses | Their actual staged services, kernel boundaries, final PCM/capture and exact cleanup | More than the explicitly declared route and fixture |
| Stock phones and microphones | Interoperability, audible prefix/tail, timing, drift and grouping | Untested models, speakers or network configurations |

The maintained harness instructions are in [tests/linux](../tests/linux/README.md).
The installer composes pinned native patches and runs their source/checker
contracts; patch provenance remains in [native patch notes](../install/patches/README.md).
Source pins, digest guards and before/after native regressions remain necessary
because the production backends are built from those exact patches. Retired
standalone Loopback routing demos and duplicate latency-policy implementations
have been removed. Loopback remains an optional measurement instrument for
current final-output qualification, not a production routing dependency.

## Recorded deployment evidence

The October 2 LIVE182 deployment passed its software qualification, lifecycle,
reboot and rollback checks. The user confirmed fresh iPhone playback without
moving web volume, phone/web volume feedback and resume after a 40-second pause.
These observations belong to that recorded release and device.

The October 3 updates added local model controls, repaired idle speech, saved
AirPlay NTP compatibility for the tested Sonos output, and introduced quiet
output retention. A held-connection Qwen request admitted its first PCM in
79.15 ms and delivered 4.64 seconds without reported drops; the user heard the
complete greeting. That is one software admission measurement and a listening
observation, not microphone-based onset or whole-house synchronization.

Use these immutable records for exact scope and artifact identity:

- [October 2 deployment](CHECKPOINT_2026-10-02.md)
- [October 3 TTS/idle repair](CHECKPOINT_2026-10-03.md)
- [Speech startup diagnosis](CHECKPOINT_SPEECH_STARTUP_2026-10-03.md)
- [Cold playback repair](CHECKPOINT_COLD_PLAYBACK_2026-10-03.md)
- [Quiet readiness release](CHECKPOINT_TTS_READINESS_2026-10-03.md)

They are evidence records, not commands to reintroduce their retired designs.
No October 4 live deployment, model-performance benchmark or acoustic test is
claimed by the workspace refactor.

## Remaining acceptance

At the user's request, physical acceptance follows implementation and automated
software review. It is not a reason to leave implementable software unfinished.
Before describing a new build as physically qualified, record:

1. Stock iPhone discovery, first play, competing sources, pause/resume and both
   volume directions on the installed build.
2. Native selection of multiple Shiri receivers, final output alignment during
   startup/regrouping, and drift over extended playback.
3. Independent texts in two rooms, per-room queue order, explicit replacement,
   cancellation and audible full prefix/tail over idle and active music.
4. The selected real model's cold recovery and warm time to audible speech,
   with request, software-stage and microphone clocks identified.
5. Mixed wired, AirPlay, Cast and paired Bluetooth routes, including any
   speaker-managed Bluetooth group, with measured corrections and limits.
6. Installed service startup/recovery, network outage behavior, exact cleanup,
   state migration and coherent package/backend rollback.

[Speech acceptance](SPEECH_ACCEPTANCE.md), [digital soak](DIGITAL_SOAK.md),
[calibration](CALIBRATION.md), [daemon privileges](DAEMON_PRIVILEGES.md), and
[Bluetooth output](BLUETOOTH_OUTPUT.md) define the relevant contracts. Saved
configuration or healthy control responses never substitute for those measured
results.
