# Recorded speaker timing calibration

Shiri implements deterministic probe generation, bounded stereo PCM import,
relative-arrival analysis, exact-room measurement sessions, reviewed offset saves,
post-change verification and rollback. The admin UI exposes the workflow under
**Speakers → Measure speaker timing**. Synthetic fixtures and HTTP/browser tests
exercise it; physical microphone, speaker and phone measurements remain pending.
The reference speaker may belong to the target zone or another configured zone,
including the intended Bluetooth-zone versus Wi-Fi-zone comparison within a
native iPhone group. The session endpoint always identifies the exact target
room; cross-zone endpoints are `(room UUID, output ID)` pairs, so two rooms'
local OwnTone output `0` remains unambiguous.

Calibration measures recorded acoustic arrival at the declared setup. It cannot
replace native receiver clock synchronization, align unrelated source programs,
or make an approximate Cast transport phase accurate. Native iPhone multi-zone
playback and its synchronized final outputs remain requirements. See
[TIMING_RESEARCH.md](TIMING_RESEARCH.md).

## Capture and review

1. Assign the measured outputs to their rooms. Choose the target speaker, a
   reference zone and its reference speaker. The same zone remains supported;
   different zones may each have just one output. Name the capture device and
   describe microphone placement, processing,
   network conditions and playback restarts. The room UUID, speaker identities,
   local device, saved offsets and configuration of both rooms are bound to this
   session. For cross-zone work, describe the common playback source and native
   phone grouping context. This declaration is exported, never reported as an
   observed or certified phone group.
2. Download the session's quiet, 48 kHz mono WAV probe. Stop normal music and play
   that file through the measured room using its normal music source. For a
   cross-zone test, select both Shiri zones together through the phone's native
   AirPlay grouping controls and play this exact probe from one source. No Shiri
   sender application is required. It contains eight
   different 120 ms band-limited noise markers over 5.6 seconds. Shiri does not
   start a room player, select a different output, or inject this probe as speech.
3. Record each speaker on a separate channel of one stereo capture device. Both
   microphones must share one ADC clock. Keep gain, processing, position and
   orientation fixed, preferably near their corresponding speakers with matched
   distances. Disable automatic gain, echo cancellation and noise suppression
   where the capture device supports it. A microphone hearing both speakers at
   once produces an acoustic sum whose peaks cannot reliably identify the output;
   two separately started phone recordings have no validated common timebase.
4. Import uncompressed signed 16-bit stereo WAV, channel 1 reference and channel
   2 target, 16–96 kHz, 3–16 seconds and at most 2 MiB. A normal full recording fits
   comfortably at 48 kHz; trim unused leading/trailing silence or resample before
   importing high-rate files. Include complete markers and their arrivals. The
   default search bound is ±500 ms and can be set to at most ±2000 ms.
5. Import at least three fresh takes across playback restarts. The first marker
   in each take is retained as startup evidence and excluded from the fixed-delay
   estimate. At least twenty accepted steady-state markers are needed for a
   candidate. PCM-content hashes prevent the same take, even with edited WAV
   metadata, from counting again. The server cannot certify that an imported file
   came from a microphone, when it was recorded, or that declared restarts and
   microphone identities are accurate; those facts belong to the operator's
   evidence.
6. Review lag, dispersion, correlation, short-window drift and rejected markers.
   Turn the target room off explicitly before saving a candidate. This disconnects its
   receiver and stops room playback. The save changes the target's retained offset
   through the same Store operation as a manual timing edit. The reference
   room/profile remains unchanged. Both configurations are rechecked atomically
   with current target/reference revisions. The save does not
   claim that OwnTone has applied the setting or that physical alignment improved.
7. Re-enable the target room and ensure the reference room is enabled. For
   cross-zone work, recreate the same native phone group after the target's
   receiver reconnects. Verify both outputs are selected in their own rooms with exact OwnTone offset
   readback, then make at least three fresh captures under the same conditions.
   Import these as post-change verification. Twenty accepted steady-state markers
   with stable median residual lag within 1 ms produce **measured at this setup**,
   qualified by the recording geometry, import provenance and backend evidence.
   A synthetic or simulated run remains a software check, not physical proof.
   Stable fresh recordings with more than 1 ms residual lag instead show
   **correction did not align recorded arrivals**, with rollback and investigation
   available. Verification never suggests or automatically applies a second delay.
8. Export evidence that must outlive the bounded history. The history picker
   reopens retained measurements after browser or API restart; starting another
   measurement preserves the previous receipt. To restore the previous offset,
   turn the room off and use **Restore previous delay**. An intervening speaker,
   profile, local-device or room-configuration change in either room invalidates an old
   candidate and its rollback. Shiri never overwrites a newer manual adjustment.

A target arriving 23 ms later than the reference has a positive measured lag.
Its candidate offset is `previous_target_offset_ms − round(lag_ms)`: for an old
100 ms setting, the candidate is 77 ms. Positive OwnTone offsets add delay.
Negative settings are bounded by the backend's available buffering; saved intent
and reported numbers alone do not demonstrate earlier acoustic arrival.
[OwnTone output API](https://github.com/owntone/owntone-server/blob/29.3/docs/json-api.md#change-an-output)

A declared geometry correction is **target travel time minus reference travel
time**, in milliseconds, and is subtracted from the observed lag. Zero assumes
matched acoustic distances. This correction is operator supplied, not inferred
from the audio. Reflections, microphone crosstalk and changing placement can
invalidate a result even when the digital transport timing has not changed.

## Implemented analysis and conservative rejection

`shiri/calibration.py` uses normalized FFT matched correlation against each
session-specific, nonperiodic marker. Capture-rate conversion uses the same
48 kHz source waveform; unrelated random patterns are never regenerated at a
new sample rate. Absolute correlation accepts inverted polarity, while retaining
both polarities in the result. Positive lag is target arrival minus reference
arrival, measured in the same capture samples.

The versioned `shared-adc-probe-v1` policy rejects:

- A marker with normalized correlation below 0.65, a competing peak outside
  ±1 ms above 78% of its main peak, clipping, missing samples or incomplete framing.
- A fixed correction with fewer than three distinct recordings/twenty accepted
  steady-state markers, more than 20% rejected steady markers, median absolute
  deviation above 0.35 ms or a 5th–95th percentile range above 1 ms.
- A within-recording lag trend larger than its stated uncertainty and accumulating
  more than 0.5 ms over the observed span, or a candidate outside −2000…2000 ms.

These explicit thresholds are tested acceptance rules, not universal acoustic
confidence guarantees. Normalized correlation is a waveform-match score, not a
probability that a peak belongs to the intended speaker. Observed uncertainty is
at least one capture sample and half the measured percentile spread; it does not
include undeclared microphone geometry, device processing or clock uncertainty.

Drift uses a least-squares lag-versus-capture-time slope for each take, reporting
ms/minute, residual dispersion, span and a finite-resolution/fit uncertainty.
Short five-second recordings can reveal substantial changing delay but cannot
validate 30-minute clock stability. Long continuous captures, reconnect tests,
shared-ADC final-output comparisons and representative network-load tests remain
physical acceptance work. One constant offset cannot remove variable jitter or
correct a sample-clock mismatch. OwnTone's mixed Chromecast/AirPlay timing stays
approximate. [OwnTone Chromecast limitations](https://owntone.github.io/owntone-server/audio-outputs/chromecast/)

## HTTP session contract and lifecycle

All routes require installation authentication and the normal same-origin write
checks. Sessions are addressed by the exact room UUID; Nobly bindings do not
retarget them. A session ID used with another room returns 404.
`reference_room_id` defaults to the target room. Cross-zone creation additionally
requires `expected_reference_revision` and a nonempty `playback_context`.

| Method and suffix under `/api/v1/rooms/{uuid}/calibration` | Behavior |
| --- | --- |
| `POST` | Create with expected room revision, target/reference IDs, optional reference room, capture device, geometry, playback context, optional max lag and geometry correction |
| `GET` | Resume this room's unexpired session evidence |
| `GET /{session}` | Read analysis, bound configuration and save/rollback receipt |
| `GET /{session}/probe.wav` | Download the unique quiet test signal |
| `POST /{session}/recordings` | Import binary `audio/wav`; `?verification=true` identifies fresh post-change captures |
| `POST /{session}/apply` | Save the reviewed target correction with `expected_revision`, `expected_generation`, and cross-zone `expected_reference_revision`; target room must be off |
| `POST /{session}/rollback` | Restore the retained target offset with current room/evidence/reference revisions and both unchanged configurations; target room must be off |
| `GET /{session}/export` | Download evidence JSON without raw audio, SDP or installation tokens |
| `DELETE /{session}` | Remove evidence; retained speaker offsets remain unchanged |

Sessions and correction receipts are durable in SQLite schema v2. The migration
from a validated v1 database adds only the evidence table; existing room,
profile, phone-receipt and event intent is preserved. Earlier same-room v2
evidence normalizes its absent reference fields from the target snapshot on
read, without rewriting the stored JSON. Unfinished sessions last one
hour and are limited to sixteen globally. Applying a correction retains that
session for up to 90 days; the global history holds at most 128 sessions, evicting
the oldest saved evidence before active sessions. Each summary is bounded to
256 KiB, twelve baseline takes and twelve verification takes. Expired evidence
is inaccessible and pruned during the next session save; expiry never changes
speaker offsets. Deleting evidence also deletes that session's rollback receipt;
deleting its room cascades its evidence. Export before deletion or retention
expiry if a longer history is needed. Raw WAV/PCM is discarded after bounded
analysis. Export provides reproducible measurement hashes,
per-marker evidence, declared setup, analysis version, reported offsets and
backend versions where available; raw recordings stay with the operator.
Deleting a separate reference room leaves the target's evidence available for
export, but blocks further import/apply/verification/rollback with an actionable
conflict. Each take records both rooms' current volume/revision/enabled state and
runtime versions at import. Backend verification records exact room/output
endpoint readbacks; duplicate local output IDs are never collapsed into one key.
Those observations do not certify the recording's emission time or origin.

Only one FFT analysis runs at a time; concurrent imports receive a conflict
instead of queueing more recording memory. Canceled analysis holds its resource
guard until its bounded worker finishes and cannot append late evidence.
Offset/profile changes, room revisions, events and rollback receipts commit in
one SQLite transaction. Receipt-write failure rolls the entire operation back.
Runtime reconciliation follows the commit: interruption or failure after it
leaves a durable receipt with an unconfirmed acknowledgment, which can still be
read, exported and rolled back after restart. Canceled saves finish durable
bookkeeping before releasing the mutation guard.
The browser reports unconfirmed writes as ambiguous and requires a refresh,
without automatically retrying them. Evidence generations prevent a new import
in another process from changing the candidate reviewed by an earlier browser.
Both room revisions plus exact configuration
fingerprints protect apply, verification and rollback from stale ownership or
profile edits. Volume and enabled-state changes are allowed only with current
revision checks; all measured identities and offsets must still match.

Calibration saves only with the room disabled. Runtime acceptance is separately
reported and remains pending after an outage. Verification additionally requires
both measured rooms enabled, both speakers selected/available in their own rooms
and exact integer offset readbacks. Imported recording chronology and native
phone grouping remain operator supplied. TTS never
uses this workflow or applies playback-restarting offset changes: speech only
changes music mix gain while its program and receiver connection continue.

NumPy is provided by Shiri's `audio` dependency extra. Without it, new analysis
and probe generation are unavailable, while retained evidence, exports and
guarded rollback remain usable in the API and admin UI. The retained analysis
version, deterministic probe hash and generator version identify the waveform;
a changed generated probe is rejected rather than silently reused.

## Software evidence and remaining physical acceptance

`tests/test_calibration.py` exercises integer delay recovery within one sample,
inverted polarity, independent 44.1 kHz conversion, known geometry corrections,
noise, clipping, silence/dropouts, ambiguous echoes, changing startup delays,
500 ppm drift, duplicate PCM, format/body limits, exact-room auth, stage guards,
optimistic conflicts, stale rollback, backend outage/readback and repeated
cancellation. `tests/test_calibration_history.py` independently exercises audited
schema migration, restart/export/rollback without NumPy, atomic SQL receipt
failure, cross-store generation conflicts, retained-profile protection, bounded
history/expiry, rejected corruption and room-deletion cleanup. Browser tests
exercise the binary upload, room-off requirement,
conflict retention, explicit fresh verification, safe text rendering and rollback
on a narrow screen, including reopening retained evidence from a fresh browser.
Cross-zone fixtures additionally prove duplicate local IDs, target-only profile
changes, both-room readbacks/configuration/revision guards, reference deletion,
legacy-record normalization and restart-safe cross-zone rollback. Audits
recompute drift from retained markers, so a cached zero trend cannot suppress a
measured drift rejection. No physical microphones, speakers or phone settings are
invoked by these tests.

Physical release evidence still needs recordings across representative AirPlay,
Cast and Bluetooth output combinations, three playback restarts, LAN load,
reconnects, firmware/backend changes and microphone geometry; longer captures
must separately establish drift and final native multi-zone alignment. The
implemented analysis/session/UI is ready for those controlled experiments. Its
software fixtures do not satisfy those hardware gates.
