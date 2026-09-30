# Speaker timing calibration

The rebuild stores and applies per-speaker OwnTone offsets. Automated acoustic
measurement is the next stage; it is not implemented or proven by the offset
API. Calibration must distinguish a repeatable constant delay from startup
variation, ongoing jitter, clock drift and the time sound takes to reach a
microphone.

## What an offset changes

The setting is an integer number of milliseconds from `-2000` to `2000`.
Positive values delay a speaker. Profiles follow the stable network output ID,
or the explicitly configured physical local audio device, and survive room
deselection. Optimistic room revisions protect profile edits. The OwnTone
adapter requires an acknowledgment and readback, then a playback-session
restart when necessary before claiming the backend has applied the change.
This is an explicit administrative calibration operation. TTS must never apply
that pause/restart sequence: speech changes only music mix gain while playback
and the phone's connection continue.
[OwnTone output API](https://github.com/owntone/owntone-server/blob/29.3/docs/json-api.md#change-an-output),
[OwnTone offset application](https://github.com/owntone/owntone-server/blob/29.3/src/player.c#L2743-L2771)

Suppose two speakers have measured arrivals of 120 ms and 170 ms under the
same conditions. Delaying the earlier speaker by 50 ms is a candidate
correction. Verify the sign with a synthetic delayed waveform and then a real
playback experiment. The candidate becomes a measured result only after a new
recording confirms its effect. Negative offsets are allowed by the API, but
the available buffering and individual backend impose practical limits; a
stored negative value does not prove that audio can arrive earlier in every
transport.

One constant offset cannot remove changing network delay or mismatched sample
clocks. OwnTone specifically does not promise precise Chromecast/AirPlay
alignment. A mixed group can be measured and improved without relabeling it as
phase-accurate or permanently synchronized. [OwnTone Chromecast limitations](https://owntone.github.io/owntone-server/audio-outputs/chromecast/)

## Stage 1: deterministic signal and analysis tests

Start with reproducible PCM fixtures rather than a microphone:

1. Generate a low-level band-limited chirp or pseudorandom probe with a recorded
   sample rate, sample count, seed and hash. Include silence between bursts and
   unique burst markers so a one-second mistake cannot look like a valid peak.
2. Produce delayed/noisy copies with known offsets, inverted polarity,
   clipping, dropouts, competing echoes and sample-rate drift.
3. Estimate lag with normalized cross-correlation in an explicitly bounded
   search window. Report the lag in samples, then convert using the actual
   capture rate. Record the sign convention; use the lag array matching the
   correlation order. [SciPy cross-correlation](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.correlate.html),
   [SciPy lag indices](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.correlation_lags.html)
4. Reject clipped or incomplete windows, low signal-to-noise recordings and
   ambiguous peaks. A high peak can be a reflection or a repeating waveform;
   compare peak separation, normalized peak value and agreement across bursts.
5. Confirm known integer delays are recovered within one capture sample when
   the fixture has no drift/noise. Under degraded fixtures, report uncertainty
   or reject the estimate instead of inventing confidence.

No single universal confidence threshold is assumed. Tune thresholds against
accepted and rejected fixtures and retain that validation data with the
analysis version. The production offset granularity remains 1 ms even if the
estimator has finer sample resolution.

## Stage 2: Linux routing and backend application

Use the real room mixer and FIFO reader before adding network speakers. Check
format, complete stereo frames, dropout counters, bounded buffers and shutdown
with the reader connected, missing and reconnecting. Then route a known probe
through the pinned OwnTone backend and a physical output.

Test an offset while stopped and while playing. Record requested offset,
readback, pause/restart/resume acknowledgment and acoustic result separately.
If OwnTone, the source or a device fails during this sequence, keep the saved
intent and report application pending or failed. Repeatedly pressing Save must
not silently reset the retained profile to zero.

## Stage 3: microphone measurement

The preferred setup records outputs simultaneously on channels sharing one ADC
clock, with a microphone near each speaker and documented equal or corrected
speaker-to-microphone distances. Cross-correlate corresponding copies of the
same probe. One capture clock makes relative lag meaningful without guessing
the relationship between a browser clock and a Linux monotonic clock.

A single phone microphone is a constrained workflow. Keep its position,
orientation, gain and capture processing fixed throughout a run. Use explicit
permission, visible recording state and a bounded recording lifetime. Document
whether echo cancellation, automatic gain or noise suppression could be
disabled. Keep raw capture local by default and provide an explicit export
action for reproducible diagnosis.

Do not compare sequential recordings against separate browser timestamps and
call the result speaker delay. Independent start/negotiation latency can
dominate that estimate. Sequential probes require a validated common capture
and emission time reference, repetitions and an uncertainty budget for any
clock mapping. Label restart-based measurements as startup-relative until
steady-state alignment is independently verified.

When two speakers emit the same probe into one microphone at once, the result
is their acoustic sum. Peaks cannot generally be assigned to speaker IDs from
that recording alone. Use isolated routes plus a proven reference, a shared
multichannel capture, or a backend that can deliver distinguishable probes to
individual outputs. OwnTone's common PCM room program does not by itself
provide independent per-speaker probe content. Reject ambiguous measurements.

The microphone measures arrival at its location. Changed distance and room
reflections change that measurement even if digital timing stays identical.
Describe results as alignment at the recorded geometry; do not generalize
them to every listening position or confuse them with receiver clock accuracy.

## Stage 4: repetition, jitter and drift

For each speaker pair and transport combination, capture at least 20 valid
bursts across three playback restarts. Repeat on an unloaded LAN and under a
documented representative Wi-Fi/load condition. Keep invalid attempts in the
record with rejection reasons; never report only the best take.

Report median relative lag, dispersion (including median absolute deviation),
5th/95th percentiles, accepted/rejected counts, correlation confidence and
clipping/dropout evidence. Separate the first burst after startup from
steady-state bursts. A candidate constant offset follows a stable central
estimate; inconsistent runs require investigation rather than a larger offset.

For drift, record repeated markers for at least 30 minutes and estimate the
trend of relative lag against elapsed capture time. Report ms/minute with
residual jitter and reconnect/underrun events. A growing trend is evidence for
clock or resampling correction work in the backend, not a reason to keep
adjusting a room's fixed delay automatically.

After applying an offset, rerun the same conditions and compare distributions.
An improvement that disappears after a reconnect is a failed calibration
acceptance test. A change of speaker firmware, transport, audio device, capture
geometry or backend build invalidates the claim until rechecked.

## Proposed measurement record and product states

The next measurement module should retain:

| Field group | Required evidence |
| --- | --- |
| Identity | Room UUID, speaker identities, protocol, local device where relevant |
| Environment | Backend/build versions, network condition, capture device/format, geometry |
| Probe | Signal hash/seed, sample rate, marker positions, emitted sample reference |
| Analysis | Algorithm/version, lag sign, search window, uncertainty, rejection rules |
| Results | Repetitions, raw relative lags, confidence, jitter, drift and dropouts |
| Correction | Previous/requested/readback offsets, session restart state, post-change results |

Raw microphone recordings need a retention limit and an explicit export
policy. Profiles and measurement evidence must be separate: a manual offset
can exist without any verified acoustic measurement.

Product states should read **manual offset**, **measurement in progress**,
**insufficient evidence**, **candidate correction**, **backend applied**, and
**measured at this setup**. Only the last state includes a fresh post-change
recording. API success, a native protocol label, a green health check or a
strong correlation peak alone never authorizes a physical-sync assertion.

## Backend decisions after measurement

First isolate whether an issue arises before OwnTone, in its output adapter,
in the network or in a device's audio buffering. Capture a minimal reproduction
and pin any backend fix. Assess an alternative backend using the same probes,
devices, load and failure tests. Sendspin defines timestamp scheduling plus
continuous offset/drift estimation, which is useful for comparison, but it
requires compatible endpoints and measured implementations.
[Sendspin timing specification](https://www.sendspin-audio.com/build/spec/#clock-synchronization)

Native iPhone multi-zone playback is required; its final-output synchronization
still needs whole-path verification and any necessary shared-timeline redesign.
Calibration cannot turn independently started room players into one program timeline.
