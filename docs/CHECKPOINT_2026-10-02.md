# Rebuild checkpoint — October 2, 2026

This is a recovery checkpoint while release qualification continues. It is not a production release. The current speaker-test VM has not received the final combined build.

## Product contract

Each zone exposes one native AirPlay 2 receiver. Users select or group zones through the phone’s normal AirPlay controls and assign physical speakers through Shiri. OwnTone schedules outputs; Shiri preserves presentation timing and exact source/session ownership. One room master is shared by phone and web controls, with persistent setup attenuation for each physical speaker. Speech targets an exact zone and ducks continuously advancing music without pausing or flushing it.

A Bluetooth speaker group is one paired primary device; the vendor manages its followers. Bluetooth input is excluded. Chromecast input is deferred at the user’s request. Cast output and mixed transports require physical measurement; a stable offset correction cannot remove changing transport jitter. Nobly currently has a zone-addressed speech API contract because that application does not yet exist.

## Current qualified artifacts

The final combined build is installed in an isolated Ubuntu rehearsal clone. Its frozen source contains 445 files; its wheel contains 52 package files. The receiver reports `timed3-startup1-volume2`; OwnTone reports the combined `balance1-transition1-bed1-event1` contract. The original pinned Git origins remain intact. The 50-account daemon identity map, dependencies and helper binaries are preserved.

Actual native checks passed the retained startup, clock, event, real pairing-crypto, bounded receiver callback and paused-speech cases, including sanitizer checks. Both native builds compiled on Ubuntu. The final Python 3.10 suite passed 3,918 tests with 14 platform or explicit opt-in skips. The earlier run in Linux’s world-writable `/tmp` failed 53 mock policy-file tests because the production guard correctly refused that ancestor. The successful run used a protected root-owned `--basetemp`; no runtime policy was weakened. All original failed outcomes remain preserved.

The installed package, native binaries, manifest and original rollback chain passed strict idle preflight and independent read-only admission. These checks establish the artifact and installation contract; functional, reboot and rollback checks remain separate requirements.

## Actual encrypted playback and volume

The current combined build passed cold decoded playback, advancing music under speech, ducking/restoration, incoming AirPlay volume and encrypted reverse-event acknowledgement in the isolated network run. Every zone, terminal, source and shared-sender actor retained its invocation/cgroup identity through a real systemd manager reload, with namespace masks still denying all namespace creation. No physical audio device was granted.

The OwnTone test sender acknowledges reverse device-volume events but does not apply them to its own displayed master. That run proves delivery and acknowledgement, not the iPhone slider. An actual iPhone check remains required.

The first current run stopped before playback because its test Unix socket path was too long; cleanup passed. Its successor reached long pause, where the test incorrectly required the OwnTone sender’s owner to remain present for 31 seconds. The pinned sender deliberately sends TEARDOWN after its ten-second paused-output timeout. Shiri retired that owner correctly. The corrected test requires actual sender TEARDOWN, no music PCM during pause, unchanged zone services, exact owner retirement, and a fresh music owner on resume. The failed run remains failed until a new actual run passes. This source fixture is AirPlay 2 type96/PTP; it cannot establish the iPhone’s type103 pause behavior.

## Live phone-volume failure

The user previously confirmed normal fresh playback and resume after a requested 40-second iPhone pause on the diagnostic build. A subsequent unapplied phone-volume report exposed a separate real ownership failure: systemd249 serialized the transient namespace restriction as an empty value and restored it as an all-allowed mask after manager reload. The old strict guard refused those changed actors, leaving reserved resources and a closed worker routing lane. Reordered DeviceAllow observations also caused an exact-order comparison failure.

The current build persists an invocation-bound restrictive companion policy and verifies its exact bytes/inodes as well as the actual typed namespace mask. It compares DeviceAllow as the exact path/rights multiset, retaining duplicates and every other ownership check. The actual current encrypted run verified reload survival and incoming volume with all transport actors live. The live diagnostic VM still needs exact legacy-actor retirement and coherent rollout; policy restoration alone cannot reopen its closed worker.

## Earlier functional evidence

On the preceding combined installation, the actual two-zone minimum-buffer gate, 20 speech sessions per zone, worker crash isolation, 50 enable/disable cycles and both broker SIGKILL recovery phases passed with exact process/output/network cleanup. The corrected finite-speech gate subsequently passed idle and native-music prefix, body, tail and quiet completion; the earlier failed subtraction oracle is preserved.

The Bluetooth run passed audio, speech, volume and takeover but its final oracle incorrectly equated an intentionally retained socket with continuing music after END. Actual source retirement, DropSync, stopped frame counters and over eight seconds without RTP were retained. The corrected END oracle requires exact retirement and stable counters independently of socket lifetime. Current-build reruns remain required; historical passes do not qualify the newer namespace/native changes.

## Remaining before software acceptance

- Complete current encrypted healthy and delayed-GRANT failure/recovery checks with exact cleanup.
- Rerun grouping, repeated and finite speech, Bluetooth END, worker faults and lifecycle/recovery against the current installed build.
- Verify normal guest reboot, fresh boot admission and encrypted bootstrap, then exact two-stage rollback to the preceding installation and original preimage.
- Retire the exact live legacy actors under restored full ownership authority, deploy the complete qualified package/native pair, and preserve room names, assignments, speaker trims and master volume.
- Confirm first playback, long pause/resume, phone-to-room master and web-to-phone slider behavior on the actual iPhone.
- Finish the durable handoff, release notes and final Git checkpoint. Mark the goal complete and stop only after these software requirements pass.

The private validation artifacts are retained under `/Users/homr/Documents/Shiri-Validation/release-2026-10-02`. Acoustic alignment, real mixed speakers and household acceptance remain the later physical measurements agreed with the user.
