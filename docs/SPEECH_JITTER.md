The optional `jitter1` backend layer delays consumption of the room's late
speech queue until 20 ms after its first valid PCM admission. The next player
tick consumes the original samples in order. Music keeps its existing player
clock, native presentation calendar, output buffering and transport sessions.
The 40 ms duck and 250 ms restoration ramps are unchanged.

This reserve repairs the reproduced case in which a valid second 20 ms packet
arrives at 30 ms: the immediate-consumption preimage inserts 480 silent voice
frames at the 20 ms tick, while the new queue preserves the prefix and tail.
It adds 20 ms plus at most one player tick before a newly primed voice run;
it does not establish a physical speech latency guarantee. A longer stall
still produces missing voice and increments underflow diagnostics.

Every queued sample keeps its original packet deadline of emission plus
250 ms. Priming never refreshes that deadline. Packets already too old, or
ones that expire during priming, are refused or discarded rather than played.
The queue remains limited to 12,000 mono samples. Overflow is atomic refusal,
not partial admission. Quiet PCM cannot extend or clear an audible lease.
Normal EOF ends the lease and drains nonexpired queued voice, including tiny
utterances whose EOF precedes their first mix.

`GET /api/player/shiri-speech-status` uses the existing authenticated OwnTone
JSON API and copies state on the player thread. Its room and launch IDs, and
current source incarnation, session, epoch, generation and operation generation,
identify the snapshot. Counters are explicitly **room-launch lifetime totals**,
not counters for that source or an individual utterance. The read does not
expire or consume media, prime playback, refresh a lease, or change a source.
Clock fields are player dispatch/admission observations, not speaker render
times. `max_pcm_interarrival_ns` includes gaps between separate runs.

The status records admitted/refused packets, admitted/mixed/expired frames,
priming, empty queue and audible-lease underflow counts, admission age, packet
interarrival and the latest underflow/resume dispatch clocks. Internally the
counters saturate at `UINT64_MAX`; JSON integers saturate at `INT64_MAX`.
The current run's first PCM admission is labeled separately. Readiness ACK-v3
and its current source-operation checks are unchanged and remain required.

The fourteen-layer `jitter1` preimage uses an 80-byte room/launch/sequence wire.
It cannot retire an exact canceled voice: already-admitted A samples can play
with A's gain after B owns the Python session. Its immutable patch and proofs
remain the reproduced preimage for the additive `owner1` repair.

`owner1` admits only version2, 96-byte packets. The appended16byte voice ID is
the existing readiness `speech_id`, retained through silence and gain control
for the one public RTC session. Authenticated `begin`, `finish` and `cancel`
actions keep the original nine-field request and five-field readiness echo.
BEGIN validates the exact current source and fresh common mix on the player
thread before binding the sole queue owner. Exact finish closes PCM admission
and the lease while retaining every nonexpired natural-EOF sample. A successor
is refused until that tail drains or expires and may retry within its bounded
setup budget. Exact cancel removes the admitted voice queue, lease and reserve,
then echoes player-thread retirement. The original gain restoration ramp
continues on the unchanged music clock. It issues no music pause, source flush,
output reconnection or transport command. Stale-ID PCM/control/finish/cancel
cannot mutate a successor, and a terminal same-ID BEGIN cannot resurrect voice.
The retirement echo covers the remaining player queue, lease and reserve.
Voice already mixed and dispatched downstream can still play within the
existing output buffering and presentation schedule; cancel cannot retract
that physical tail without also flushing the shared music output.

Python retains one independently owned bounded BEGIN task per session. It
retries only after an exact not-ready reply, while ownership and the original
setup budget remain valid; an uncertain exchange is never repeated. Offer
cancellation returns while owned disposal joins the definitive BEGIN outcome,
then requests exact cancellation. A timeout, transport or schema uncertainty
retains cleanup failure and refuses successors even when CANCEL is echoed;
its terminal ID fence is not overwritten. Natural EOF is recorded before peer
close callbacks and uses exact finish, so teardown preserves the real resampler
and admitted backend tail. Cleanup uses the admitted request even if music
changes source afterward; it cannot clear the new music owner. Existing module
deinitialization and clock failure still clear the bounded queue.

The actual C checker runs the unchanged preimage, the exact patched media
module and the extracted player/HTTP functions under ASan/UBSan. It verifies
ordered streams, tiny EOF, original expiry, overflow, quiet/gain behavior,
fresh launch refusal, clipping, visible prolonged underflow, and a read-only
typed source-bound snapshot. Linux credential and full transport tests remain
separate requirements; the portable checks make no new kernel or radio claim.

The disposable Bluetooth transport fixture records optional authenticated
snapshots for both exact zones before TTS, during audible TTS, after restoration,
and at the first playback assertion failure before cleanup. It adds no periodic
poller. A snapshot is verified only when ready worker health observations before
and after the GET agree with all seven current binding fields. Unavailable,
malformed, stale or wrong-scope diagnostics are recorded separately; they do not
replace a playback rejection or relax its fixed SBC spectral floor. External
cancellation still cancels the observer and retires its owned diagnostic task.

Each response is limited to 4,096 bytes and each room observation to 250 ms,
including the worker identity checks. The GET has a 150 ms total deadline and
each worker RPC a 100 ms deadline. The two-room fixture retains at most twelve
observations. It uses the existing private authenticated client and records no
credentials, response headers, undeclared fields, speech SDP or PCM payload.

The endpoint's exact version 1 JSON schema is:

| Fields | Type and meaning |
| --- | --- |
| `version`, `reserve_ns` | Exact integers `1` and `20000000`. |
| `scope` | Exact string `room_launch_lifetime; current_source_at_read_only_snapshot`. |
| `room_id`, `launch_generation`, `incarnation` | 32 lowercase hexadecimal UUID characters. |
| `session_id` | Current music source UUID in the same format, or JSON null for idle. This is not a speech identity. |
| `epoch`, `generation`, `operation_generation` | Nonnegative signed 64-bit JSON integers identifying the current source snapshot. |
| `configured`, `source_initialized`, `source_faulted` | JSON booleans identifying media and source admission state. |
| `active`, `running`, `priming` | JSON booleans describing the speech queue at the read. |
| `observed_monotonic_ns`, `sequence`, `queued_frames` | Nonnegative signed 64-bit dispatch clock, latest datagram sequence and queue length. The queue bound remains 12,000 frames. |
| `admitted_packets`, `refused_packets`, `expired_frames`, `pcm_packets`, `admitted_frames`, `mixed_frames` | Saturating lifetime packet/frame counters. |
| `priming_events`, `priming_frames`, `empty_frames`, `underflow_events`, `underflow_frames` | Saturating lifetime queue and audible-lease counters. |
| `priming_until_monotonic_ns`, `current_run_first_pcm_admitted_monotonic_ns` | Current priming deadline and the current run's first admission clock; zero when no observation is available. |
| `last_pcm_admitted_monotonic_ns`, `last_pcm_emitted_monotonic_ns`, `last_mix_monotonic_ns`, `last_underflow_monotonic_ns`, `last_resume_monotonic_ns` | Latest admission, emission or dispatch clocks; zero when unobserved. |
| `max_pcm_admission_age_ns`, `max_pcm_interarrival_ns` | Lifetime maximum admission age and gap between PCM admissions, including gaps between runs. |

A new source binding never resets these lifetime counters. The fixture retains
raw bounded values without labeling a delta as one utterance's contribution.
In the fourteen-layer preimage, `CONTROL(false)` marks natural EOF with queue
draining and provides no per-utterance cancellation guarantee. The `owner1`
layer uses exact authenticated FINISH and CANCEL as described above. The
optional read-only observations retain this same 39-field schema.
