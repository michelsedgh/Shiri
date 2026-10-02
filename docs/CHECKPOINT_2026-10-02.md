# Rebuild checkpoint — October 2, 2026

This is a work-in-progress recovery checkpoint, not a production release. Continue the active rebuild goal after creating it. Do not infer current production readiness from the large historical unit-test counts.

## Product contract

Each zone exposes one native AirPlay 2 receiver. The user selects or groups zones through the phone's normal AirPlay controls and assigns physical speakers to each zone through Shiri. OwnTone schedules the outputs; Shiri retains the source presentation timeline, route timing and exact source/session ownership. Use one room master volume with persistent setup attenuation per physical speaker. Target speech to an exact zone and duck advancing music without pausing or flushing it.

Bluetooth speaker groups are one paired primary device; synchronization of that device's followers belongs to the speaker vendor. Bluetooth input is excluded. Chromecast input is deferred at the user's request; Cast outputs remain approximate and require physical measurement. Nobly integration is a room-addressed API contract until that application exists.

## Saved work

The checkpoint includes the runtime, process/network ownership and recovery work; room and speaker storage; native timing and speech patches; Bluetooth speaker-output adapter; UI, calibration and measurement tools; installation and daemon isolation; and meaningful native/Python/browser regression tests. It also includes the reviewed room master/speaker balance changes and the receiver startup/clock repairs.

The actual live iPhone → Sonos test now starts and resumes normally according to the user's test after fresh connection and a requested 40-second pause. See [the live review](LIVE_PLAYBACK_REVIEW.md) for the failures, fixes and measured scope. The independent Linux native-reader tests exercised an actual 31-second admitted pause and exact socket retirement. A timer-wait race in the test was corrected without changing production code.

The live diagnostic backend binaries and the default build recipe are not yet the final combined release. The original checkpoint retained the additive live OwnTone source-transition repairs separately; the subsequent source now integrates the bounded atomic transition. Do not deploy this checkpoint over a working house installation as a qualified release.

## Checkpoint validation follow-up

The full host checkpoint run completed with 3,558 passes, 81 explicit platform skips and one failing cancellation regression. That fixture still expected volume to be applied after selection; the saved-balance implementation deliberately stages exact gains under the speaker lease before selection can start sound. The fixture now verifies that order and still requires cancellation to preserve saved intent and prevent any later operation. The focused cancellation/balance follow-up passed all 22 cases; production code was unchanged. Ruff and the 31 frontend tests passed. Linux and browser results retain their separately stated scopes.

## Remaining work

Follow-up source integration now includes the bounded atomic native transition, separate receiver volume feedback and paused-source speech. The combined OwnTone backend compiled on Ubuntu with the exact `balance1-transition1-bed1` marker. The receiver compiled with the exact `startup1-volume1` marker, retaining its real pinned Git origin. These private builds have not replaced the working live diagnostic receiver.

The combined OwnTone source passed strict composed-layer guards, 44,364 actual-C paused-speech assertions, 28 real JSON parser cases and 20 real FFmpeg converter cases under sanitizers on Ubuntu. Two test-fixture portability repairs were needed for GCC's warning checks; production code was unchanged. These are native function/converter checks with controlled hardware seams, not complete encrypted network playback or acoustic acceptance. A focused actual Python 3.10 run passed 83 cases, including the real credential/packet listener path. The complete network gate remains a separate requirement.

Further review repaired asynchronous volume/assignment intent races, notification loss across quick administrative edits, and a receiver startup race. Saved gains and the complete speaker assignment are now acknowledged before receiver advertisement. Failed startup uses the existing cleanup/backoff; released leases wake waiting sibling rooms without causing the failed room to bypass its own backoff. Enabled empty zones retain discovery, while the native output readiness guard refuses music until an output is assigned. Focused broker/feedback tests passed 161 cases with one macOS platform skip.

The subsequent full host suite reached 3,612 passes, 82 platform skips and one obsolete test expecting the earlier backend marker. Its assertion now requires the exact combined marker; all nine focused native-layer tests passed. The new network observer passed 26 regressions, including accepted-descriptor and sibling-reader cleanup. These results remain separate from the pending actual encrypted protocol run.

- Verify bounded atomic speaker preparation/FLUSH/arming, delayed output release after END and failed-setup recovery through the complete network path.
- Verify the phone's displayed slider and rapid alternating edits with the bounded receiver-to-phone feedback implementation.
- Verify saved speaker balance through actual output readback and gain changes without transport changes.
- Verify speech on a retained paused phone source through the complete network path, preserving the input timeline.
- Exercise the complete encrypted native receiver → network output chain, repeated starts, pause/resume, teardown and failures. Preserve the distinction between OwnTone's real-time AirPlay sender and an iPhone's buffered stream.
- Finish current-source speech, Bluetooth, recovery, network lifecycle, install/reboot and rollback checks. Earlier local synthetic soaks retain their original scope and cannot qualify new code.
- Complete the durable handoff and release documentation. Acoustic alignment, real mixed speakers and household acceptance require later physical measurements.

The active goal remains open. Stop the goal only after the agreed software work and its required validation are complete.
