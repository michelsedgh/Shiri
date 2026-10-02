# Live playback review — October 2, 2026

The live iPhone → Shiri receiver → Sonos Table lamp test exposed real regressions that earlier local PCM tests did not cover. This document records the failures and the required lifecycle contract. The rebuild is not production-qualified yet.

## What failed

1. A source FLUSH sealed the native input while OwnTone’s existing playback timer continued to tick. The tick rejected the sealed/new-generation input and aborted playback, replacing the pending speaker FLUSH callback. The source control request then timed out, and Shiri rebuilt the zone, disconnecting the phone.
2. The first source BEGIN granted PCM while selected network speakers had only been probed. OwnTone began the full encrypted speaker connection after timed PCM arrived. In the live test, the first anchor was already 934 ms late; another attempt was 474 ms late. Increasing the audio buffer would conceal the ordering problem.
3. The timed Shairport hook emitted initial volume from `conn->own_airplay_volume` before checking `own_airplay_volume_set`. The zero-initialized field maps to 100%, although the normal suggested volume had not yet been resolved.
4. A phone-volume acknowledgement committed a new SQLite room revision without refreshing the worker’s control revision. Fresh subsequent phone volume events could be rejected as stale until a web mutation refreshed the worker.
5. OwnTone treated an accepted FLUSH with no new PCM as a failed five-second initial anchor. The Python native reader separately disconnected any admitted source after 30 seconds without packets. Both conditions incorrectly retired a normally paused phone. Initial admission remains bounded; admitted pause ends through EOF, exact revocation or shutdown.
6. Existing OwnTone output volume is an instantaneous absolute level, not a durable independent speaker balance. Re-selection recalculates master/relative volumes. Calling selection for ordinary room-volume changes also couples an otherwise simple gain change to transport control.

## Required contract

A receiver session must prepare its assigned output transports before returning successful stream SETUP to the phone. Use the existing source BEGIN/GRANT admission rather than a second independent preparatory owner. Selected output IDs, session identities and source generation stay fenced. Preparation sends no PCM and does not start a music timer. Admission fails explicitly if the outputs cannot become ready within the existing bounded setup operation.

After successful admission, the receiver may emit PCM with the phone’s original presentation timestamps. OwnTone alone schedules outputs. Keep the measured per-route B/H policy; do not replace late timestamps with arrival time or introduce another scheduler. Reuse the already prepared speaker sessions when readable native PCM starts playback.

A valid source transition suspends the existing timer before sealing input and awaiting output FLUSH. Rearm only after the same owner/generation is successfully armed. End-of-source retires the native playback session instead of waiting for nonexistent PCM. Failure must quiesce the timer and fence input, without allowing an old callback to consume a newer transition.

Initial volume comes from Shairport’s resolved suggested volume, with the saved room master supplied as its fallback. Fresh phone control updates the room immediately and commits durably; acknowledging the commit refreshes worker revision without reselecting or reconnecting speakers. Web-to-phone feedback requires the real sender control path and must not be claimed from backend readback alone.

Each physical speaker has a persistent attenuation from 0 to 100%, independent of the room master. The applied absolute level is `floor(master × balance / 100)`. Saving balance or moving room volume must preserve the current music timeline and transport session. Preserve balances through restart, mute/unmute, removal and reassignment. Normal listening has one room volume; speaker balance is a setup adjustment.

## Evidence and validation limits

The isolated live VM retained old binaries, SQLite intent and logs before installing the first timer/phone-ACK fixes. Its OwnTone build SHA is `9516b49d9e8666eda5e61cea83abae08f251a37f8979b864f9cdf8352aadd19e`; the isolated broker SHA is `a4fafcdd8fe690190612a181c1b9722a89b6b1c69d20b364f63e388ee9cb9a0e`. These are diagnostic repair builds, not a released version.

The original/patched exact C regression reproduces lost FLUSH completion and verifies normal transition ordering under sanitizers. Five Python regressions exercise repeated phone volume commits without web changes. Neither proves actual phone playback. After deployment, the user reported first-connect audio still required web volume changes, while pause/play began working; new instrumentation identified late startup anchors. The subsequent repair performs selected-output preparation before admission, resolves initial volume before emitting it, and retains an accepted paused source without either idle deadline. The user then confirmed a fresh connect and Play without web volume changes, followed by a requested 40-second pause/resume, worked normally. The before/after observations retained the same six daemon PIDs. The subsequent worker capture reported 4,806 accepted native blocks, zero dropped bytes, no ingress fault, and matching worker/room revision 52 and volume 12. This is one actual iPhone → Sonos test, not an acoustic synchronization or broad reliability qualification.

The earlier 30-minute local PCM soak used synthetic producers and ALSA loopback. It demonstrated timing consistency for that setup but did not exercise real iPhone buffered AirPlay SETUP, Sonos connection latency or sender volume feedback. Preserve those results with their original scope.

Before release, verify the complete native receiver/network-output chain, repeated cold starts, pause/resume, teardown/reconnect, music and speech volume, speaker balance persistence, and failures during setup. Run the remaining recovery, Bluetooth, installation/reboot and rollback gates against the final coherent source and native builds. Physical acoustic alignment and mixed-speaker acceptance remain separate measured tests.

## Current live repair identity

The installed diagnostic OwnTone binary SHA-256 is `7d6287b7df35a5b438bca519a706b405bab41384457c893182e40d8e24a7f39c`. Its source includes the first timer repair, selected-output preparation, and pause/END cleanup. The installed Shairport binary SHA-256 is `c79ac38d768acc4b102513a7bdf70ab22ca05ca7d61e6e5fce777d728c1e1d95`; its actual Git origin is pinned `7bad231c18368dbd26f298577f6210e36e4b0797` with the timed, clock recovery, synchronous startup and retry-cleanup changes.

The first startup build omitted Git metadata and truthfully reported the pinned source's package version 5.5.1. Runtime preflight rejected it. Rebuilding the same source with its genuine pinned Git metadata produced `7bad231-dirty`; no compatibility check was weakened and no version was fabricated.

Local records, private logs, binaries, rollback files and the user's test report remain outside Git under `~/Documents/Shiri-Live-Test`. The checkpoint records source and evidence summaries; it does not publish credentials or private runtime data.

## Primary references

OwnTone explicitly supports Shairport pipe input for multiroom routing: https://owntone.github.io/owntone-server/library/#pipes-for-eg-multiroom-with-shairport-sync . Its documented multiple-instance setup is https://owntone.github.io/owntone-server/advanced/multiple-instances/ .

Shairport describes source presentation timing, NQPTP and the limitations of pipe output in https://github.com/mikebrady/shairport-sync/blob/master/README.md#synchronised-audio . Its stable README currently identifies AirPlay 2 remote controls as experimental development-branch functionality; reverse sender volume needs a reviewed implementation and phone verification.
