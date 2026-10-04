# Native group timing and latency policy

This document describes the current timing contract. Retired H750/H1000 and
three/four-second experiment implementations have been removed; their research
history remains in Git. There is one policy in
[`runtime/latency.py`](../shiri/runtime/latency.py), shared by production and the
maintained qualification harnesses.

## Route buffers and common presentation

| Selected route, zero offset | Output buffer B | Single-room relay horizon H |
| --- | ---: | ---: |
| Local ALSA / private framed Bluetooth A2DP | 40 ms | 140 ms |
| Cast / Pulse | 250 ms | 350 ms |
| AirPlay 1 / 2 | 500 ms | 600 ms |

For each room, B is the largest of 40 ms and each selected output's required
lead minus its negative offset. Every enabled room shares
`H = max(enabled room B) + 100 ms`. Disabled rooms do not enlarge the plan.
A negative offset advances one output relative to the others, so B grows to
retain the required lead; positive offsets never reduce a route's floor.
Local offset −2000 ms uses B2040/H2140; AirPlay uses B2500/H2600.
The complete validated plan is frozen for a program incarnation. Discovery
outages cannot shrink it, and speech never changes it.

For the phone's native presentation time P, the relay carries `P + H`.
OwnTone's framed input subtracts B before arming its existing player timer;
output buffering adds B back. Independently arriving receivers from the same
iPhone group therefore retain the same final program deadline. A fresh FIFO
read timestamp or independent player startup would lose that relationship.
An initial `P + H − B` anchor that is already late is rejected even if the final
presentation time remains in the future. Shiri does not rewrite a late anchor
to arrival time.

B and H are software settings, not measured acoustic latency. AirPlay's 500 ms
floor preserves the actual pinned sender/receiver arithmetic: the receiver
subtracts 250 ms protocol latency and 150 ms backend buffering, leaving 100 ms.
An indiscriminate 250 ms replacement can become negative and wrap into an
unsigned sample count. Extracted native tests exercise the packet encoding and
consumer across all admitted offsets. AirPlay 1 uses the same conservative
floor; physical devices may require more. Cast jitter, Bluetooth radio/device
buffering and speaker DSP delay are not eliminated by these calculations.

## Preserving clock provenance

The patched Shairport backend provides the actual first-sample presentation
anchor after resampling and partial-frame skipping, together with original RTP
frame position, a fresh connection identity and native flush generation.
It supplies paired `CLOCK_MONOTONIC_RAW` / `CLOCK_MONOTONIC` observations so the
Linux audio worker can map P without assuming a constant offset between clocks.

A disrupted clock triple is retried at most four times within a total 5 ms
sampling deadline. The receiver admits at most 1 ms mapping uncertainty. OS
preemption may exceed the deadline, in which case the sample fails. Exhaustion
closes that exact producer without sending an invalid mapping or advancing its
timed-frame counters. The payload and native presentation anchor are unchanged
by a retry. Optional Shairport progress metadata is not the timing source.

Source transitions serialize output quiescence, generation changes and grants.
Each final music write checks current source ownership. Old callback epochs,
flush generations, repeated presentations and stale connections cannot supply
successor music. A paused phone keeps its accepted ownership without an
arbitrary media-idle timeout; its next real source event determines retirement.

## Speech takes a separate final path

The audio worker sends admitted mono speech to OwnTone's authenticated late
mix socket. OwnTone mixes it immediately before output conversion, outside the
music ingress horizon H. Protocol/device buffering B still applies, as does
cold transport setup when connections are unavailable. Maintaining enabled
rooms in `ready` mode removes avoidable reconnection work while idle.

The native speech queue has a 250 ms age/queue bound and a 20 ms jitter reserve.
Finite EOF drains valid samples; explicit cancellation retires only the exact
voice. Music continues advancing during its duck and restore envelope. Text's
300/600 ms envelope is applied while audio plays, not as a fixed preroll.
Delivered speaker audio can have a bounded tail after cancellation; Shiri
never flushes the music transport to shorten it.

The Mac supplies generated audio, not speaker timestamps. The Linux worker
paces its persistent PCM stream using a monotonic sample calendar. Model time
to first PCM, route preparation, local frame acknowledgment and first audible
sound have different meanings; [local TTS metrics](LOCAL_TTS.md) distinguish them.

## Calibration and output limits

Offsets range from −2000 to +2000 ms and are stored by exact speaker identity.
They correct measured constant delay, not drift or changing jitter. OwnTone
applies offsets when opening the output session; administrative changes during
playback may require a controlled restart. Speech admission and cancellation
cannot trigger that operation.

A Bluetooth speaker-managed group is one endpoint through its paired primary;
its internal delay and synchronization require measuring the complete group.
OwnTone's Cast output does not establish precise mixed-protocol synchronization.
Stock-phone grouping, acoustic onset, long-run drift and regrouping require
real device measurements under [calibration](CALIBRATION.md) and
[product acceptance](PRODUCT_REQUIREMENTS.md).

## Maintained verification

- Portable Python tests validate one route policy, selected-offset arithmetic,
  frozen common horizons, clock mappings and exact source/voice ownership.
- Native checkers compile extracted production callbacks under sanitizers:
  receiver clock handling, framed input, both player timer variants, output
  packet timestamps, speech content/drain and transport cleanup.
- The current Linux music-minimum/grouping harnesses use the same latency plan
  as production. Observers compare independent arrivals against the common
  presentation timeline and reject truncated or retimed output.
- The finite-speech and latency qualification matrices retain exact route,
  epoch, content and cleanup observations. Actual output capture is evidence
  beyond control readback; synthetic corruption fixtures test the observers
  themselves and do not claim production audio.

The dedicated Linux harnesses require their documented isolated lab setup;
ordinary pytest and an unprivileged container do not exercise a deployed
systemd/network/audio installation. Historical digital and phone observations
remain in [the live handoff](LIVE_TEST_HANDOFF.md) and dated checkpoints.
Current refactor verification is recorded in
[the October 4 review](REPO_REVIEW_2026-10-04.md).
