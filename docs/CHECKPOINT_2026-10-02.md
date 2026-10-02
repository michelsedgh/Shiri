# Rebuild checkpoint — October 2, 2026

This is a work-in-progress recovery checkpoint, not a production release. Continue the active rebuild goal after creating it. Do not infer current production readiness from the large historical unit-test counts.

## Product contract

Each zone exposes one native AirPlay 2 receiver. The user selects or groups zones through the phone's normal AirPlay controls and assigns physical speakers to each zone through Shiri. OwnTone schedules the outputs; Shiri retains the source presentation timeline, route timing and exact source/session ownership. Use one room master volume with persistent setup attenuation per physical speaker. Target speech to an exact zone and duck advancing music without pausing or flushing it.

Bluetooth speaker groups are one paired primary device; synchronization of that device's followers belongs to the speaker vendor. Bluetooth input is excluded. Chromecast input is deferred at the user's request; Cast outputs remain approximate and require physical measurement. Nobly integration is a room-addressed API contract until that application exists.

## Saved work

The checkpoint includes the runtime, process/network ownership and recovery work; room and speaker storage; native timing and speech patches; Bluetooth speaker-output adapter; UI, calibration and measurement tools; installation and daemon isolation; and meaningful native/Python/browser regression tests. It also includes the reviewed room master/speaker balance changes and the receiver startup/clock repairs.

The actual live iPhone → Sonos test now starts and resumes normally according to the user's test after fresh connection and a requested 40-second pause. See [the live review](LIVE_PLAYBACK_REVIEW.md) for the failures, fixes and measured scope. The independent Linux native-reader tests exercised an actual 31-second admitted pause and exact socket retirement. A timer-wait race in the test was corrected without changing production code.

The live diagnostic backend binaries and the default build recipe are not yet the final combined release. The additive live OwnTone source-transition repairs are retained separately while the bounded atomic transition is integrated. Do not deploy this checkpoint over a working house installation as a qualified release.

## Remaining work

- Integrate and verify bounded atomic speaker preparation/FLUSH/arming, delayed output release after END, and recovery after failed setup.
- Complete bounded AirPlay 2 receiver-to-phone volume feedback, including stale-session and echo handling; verify the phone's displayed slider.
- Build and verify the saved speaker-balance backend on Linux and test gain changes without transport changes.
- Complete speech on a retained paused phone source using OwnTone's existing output timer, without advancing or corrupting the input timeline.
- Exercise the complete encrypted native receiver → network output chain, repeated starts, pause/resume, teardown and failures. Preserve the distinction between OwnTone's real-time AirPlay sender and an iPhone's buffered stream.
- Finish current-source speech, Bluetooth, recovery, network lifecycle, install/reboot and rollback checks. Earlier local synthetic soaks retain their original scope and cannot qualify new code.
- Complete the durable handoff and release documentation. Acoustic alignment, real mixed speakers and household acceptance require later physical measurements.

The active goal remains open. Stop the goal only after the agreed software work and its required validation are complete.
