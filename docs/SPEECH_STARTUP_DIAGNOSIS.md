# Speech startup diagnosis — 3 October 2026

The investigation below records the earlier uninstalled speech checkpoint.
The later [live rollout](CHECKPOINT_COLD_PLAYBACK_2026-10-03.md) installs these
repairs with reproduced cold-music and AirPlay gap-timing fixes. Physical
confirmation of the idle speech opening remains pending.

The reported missing opening words are a real playback failure. Silent captures
show that generation and the room audio pipeline preserve the complete opening
through AirPlay encoding. They also show incremental delivery before generation
ends. Receiver startup remains the boundary requiring qualification: a local
PCM admission receipt cannot establish when a physical speaker becomes audible.

This investigation used saved live state, isolated sockets, generated audio
files and native capture programs. It did not play test audio through the house
or activate a speaker while the user was sleeping. Physical acceptance of the
new startup repair is **pending**.

## Reported failure and measured timing

The request was `Hello. This is Shiri speaking in your room.` The user heard
only the ending, `your room`, and perceived that playback waited for the whole
waveform. The saved job `6fb0a0e05969e2fc6343fac70bf770d5` completed with these
metrics:

| Measurement | Saved value | What it establishes |
| --- | --- | --- |
| First generated normalized PCM | 157.975 ms | First model output available to the worker |
| First non-silent generated chunk | 157.975 ms | That chunk contains non-quiet audio; its first sample need not be speech |
| Leading generated silence | 11.458 ms | Quiet waveform prefix, far shorter than the reported missing words |
| Speaker backend ready receipt | 620.750 ms | Existing local startup contract completed |
| First PCM dispatch | 620.768 ms | Router began sending to the room |
| First PCM room admission | 622.565 ms | Room runtime accepted the first frame |
| Generation duration | 2440.063 ms | Time spent producing the complete waveform |
| Generated and delivered duration | 4.640 s each | Router submitted the complete waveform |
| Generation time / audio duration | 0.525876× | Generation was faster than real time for this request |
| Total job time | 5522.342 ms | Includes room preparation, paced playback submission and retirement |

Model timings use the Mac worker's generation origin; router/backend timings
use the Linux job's origin. They cannot be subtracted to produce an exact
generation-to-room interval. The independent gated stream tests below establish
incremental delivery before generation completion. Total job time includes
sending a 4.64-second waveform at playback speed; it is not a 5.52-second delay
before sound starts. These measurements alone do not establish acoustic onset.
Earlier 54–69 ms warm native-generation results
used different controlled requests and excluded room transport and receiver
startup; they are not a promise of that delay at a house speaker.

The saved native status has `expired_frames=0`. Its last underflow predates the
current run's first admitted PCM. These counters are scoped to the room launch,
so accumulated admitted/mixed frame totals must not be subtracted and described
as loss in this particular job. The failed job had a 500 ms configured output
buffer. Neither its metrics nor the quiet captures justify adding a universal
2250 ms output buffer or a fixed multi-second preroll.

The saved room volume was 15. A separate arithmetic preview applies its cubic
gain, `(15 / 100)^3 = 0.003375`, about −49.435 dB, to the generated waveform.
That is substantial attenuation, but it is not a captured native output or a
measurement of this room's background noise. It cannot establish whether soft
opening words were below audibility. No volume, speech gain or normalization
was changed during these silent probes.

## Evidence through each boundary

Two quiet model probes used the exact user text with Qwen 0.6B CustomVoice,
Ryan/English, seed 42, temperature 0.9, full-text conditioning and 80 ms model
chunks. Both produced 233 HTTP PCM records containing **222,720 mono 48 kHz
samples**, or 4.64 seconds, and ended naturally at 58 tokens under the 512-token
cap. Their complete WAVs were byte-identical, with SHA-256
`eeb06b41760dcfc09701b8897d1c7185671f97f5d4f7a8d774d0cc2a682a5f6e`.

| Quiet native-worker probe | First HTTP PCM | HTTP end record |
| --- | --- | --- |
| Exact text, first probe | 460.947 ms | 2702.339 ms |
| Exact text, repeated probe | 444.616 ms | 2707.694 ms |

The complete clip's independent, offline CPU ASR recognized all eight expected
words; its first two-second crop began `Hello, this is` and then partial `sh`
tokens. The tiny recognizer's punctuation and partial-word repetitions are not
evidence of repetitions in generated audio. This checks gross text retention,
not subjective voice quality or exact acoustic word boundaries.

The worker's authenticated HTTP stream emits generated PCM records before
end-of-stream. The regression in
[`test_real_http_delivers_incremental_child_pcm_before_eos_is_released`](../tests/test_tts_worker.py)
holds the child generator's completion gate closed until the test has received
its first PCM over real HTTP. A separate silent coordinator proof receives the
first 20 ms at 624.305 ms after a controlled 620 ms preparation gate, while the
generator is still blocked before completion, and preserves all 0.2 seconds
of that synthetic stream. Coordinator progress tests separately distinguish
audio received from the worker and audio accepted by the room; receipt alone
cannot be presented as playback.

[`test_pcm_speech_capture.py`](../tests/test_pcm_speech_capture.py) captures
real framed local RPC and authenticated Unix PCM datagrams through AudioWorker,
PcmSpeech, NativeMixer, SpeechPreparation and SpeechOutput. Both warm and
620 ms cold-admission cases preserve all **220,800 mono 48 kHz samples**, as
230 uniquely identifiable 20 ms frames over 4.6 seconds. The first captured
frame arrives before later frames are submitted or EOF is sent. There are no
dropped sender frames or writes to the music FIFO. Readiness is controlled in
this fixture; the assertion ends at the datagram receiver.

The native drain capture compiles the actual OwnTone player admission, command,
timer, speech mix and output-bed bodies. Cold and warm 4.6-second cases preserve
every unique sample through `outputs_write`. The separate native packetizer
capture runs the real output resampler, ALAC encoder, AirPlay packetizer and
FFmpeg decoder, with local substitutes only at device/socket send boundaries.
Across four captures, 1168 ALAC packets yield 1,644,544 decoded stereo PCM
bytes, including output-bed silence and packet padding. Its opening and ending
sample assertions survive that conversion. These tests are silent and establish
encoded sample retention, not receiver rendering.

The revised host and isolated Linux player fixtures pass 486,512 checks,
including the original 44,364 lifecycle checks. The finite EOF silence budget is 480 frames total;
short timer intervals can consume that budget over several callbacks. This
retains fractional-callback tails without reserving an entire large silence
block in one short output interval. The final capture's ALAC decode matches
the encoder's input byte for byte after 48 kHz to 44.1 kHz conversion:

| Case | Input mono 48 kHz speech frames | Decoded stereo 44.1 kHz frames | Unpacketized zero-only frames |
| --- | --- | --- | --- |
| Short natural EOF | 480 | 1408 | 340 |
| Cold 4.6-second utterance | 220,800 | 204,160 | 7 |
| Warm 4.6-second utterance | 220,800 | 204,160 | 7 |
| Short EOF with a 1 ms terminal callback | 480 | 1408 | 340 |

The decoded totals include output-bed silence and packet padding. All remaining
unpacketized frames are zero-only; they contain no missing speech tail. The
fourth case independently covers the fractional timer interval that defeated
the earlier one-callback padding assumption. The final captured player corpus
has SHA-256
`73b52cce02a9b638f16e4b750a27109293da24bcfc1a193615fdebc2f79f5bb8`.

The model, ASR and HTTP receipts are saved in the local investigation directory
`/tmp/shiri-tts-streaming-proof-20261003`: `live-user-text-http.json`,
`live-user-text-warm-repeat-http.json`, `live-user-text-asr.json` and
`synthetic-stream-progress-proof.json`. Failed-job timing and read-only native
status are in `/tmp/shiri-speech-prefix-20261003/readonly-job-host.json` and
`readonly-summary.json`. These local raw receipts are separate from the
portable regression sources.

The completed evidence is also preserved in additive `batch-0004` under
`/Users/homr/Documents/Shiri-Validation/release-2026-10-03`. Its `INDEX.json`
maps durable files to their original paths and verifies sizes and SHA-256
digests. Earlier candidates and unsuccessful runs are retained separately.

The capture programs are
[`check_speech_drain.py`](../tests/native/check_speech_drain.py) and
[`check_speech_packetizer.py`](../tests/native/check_speech_packetizer.py).
They use reviewed native source bodies and sanitizers. Startup-metadata
acceptance has separately passed the host and isolated Linux checks below.

## Concrete corrections

Natural EOF had a separate receive-queue race: the player command could seal
speech admission before a previously sent final datagram was drained from the
socket. The additive
[`speech-drain` patch](../install/patches/owntone-29.3-speech-drain.patch)
drains the bounded pending receive burst before FINISH seals admission. The
preimage fixture reproduces lost final PCM, and the corrected path preserves
it. Cancel and foreign-generation guards remain enforced. This is a tail
correctness defect; it does not explain missing opening words.

Idle speech exposed a concrete AirPlay startup omission. OwnTone normally
prepares DMAP track metadata from a queue item. The output-only speech bed has
no queue item, and `airplay_metadata_startup_send` returns without sending when
the current global metadata is absent. Consequently a cold speech session can
complete control setup and receive PCM without any initial track metadata.

The additive
[`startup-metadata` patch](../install/patches/owntone-29.3-startup-metadata.patch)
sends a small DMAP placeholder on the explicit Shiri framed-output path when
the receiver requests text metadata and no real program metadata exists. It
uses the already initialized RTP session position for `RTP-Info`, rather than
calculating progress from the not-yet-initialized output timestamp. The request
is part of START_PLAYBACK, before the final volume step, so startup completes
only after the receiver accepts it. Real program metadata takes precedence;
the placeholder never becomes global track metadata or creates a fake queue
item. A durable acknowledgement log records the accepted initial metadata and
its RTP position for later physical correlation.

[`check_startup_metadata.py`](../tests/native/check_startup_metadata.py)
passes 208 ASan/UBSan checks on the host and isolated Linux target.
Its exact preimage fails because CONNECTED can be reached without initial DMAP.
The fixture captures the 71-byte metadata body and exact initial uint32 RTP
position, requires a 200 acknowledgement before final volume and CONNECTED,
rejects 400/disconnect/stale-incarnation cases, and preserves the real music
metadata skip. Independent review identified and reproduced another error
side effect: the generic startup failure path could discard a healthy saved
pairing key after encrypted setup had already succeeded. The final repair
selects the existing session-failure callback specifically for the initial
metadata step, preserving those keys while failing startup. It restores the
existing upstream failure policy for the following final-volume request.
The fixture compiles the actual upstream pairing-clearing handler and verifies
preservation after metadata rejection/disconnect and local request, header,
RTP-header, body and send failures. It also verifies the existing final-volume
failure policy is restored.

The final revision04 Linux build and repeated ALAC capture pass. The compiled
native candidate has SHA-256
`b0f1f77d2b01d9a86dc1b8b931a81adc2bfdf0c30081ca0710b324c6e0f6e67e`.
At that checkpoint the patched candidate had **not been deployed** to the live
receiver. See the later rollout for the installed artifacts; physical
confirmation of complete first playback has not been performed.

The web job now reports received and delivered audio progress while streaming.
These counters expose the difference between model generation, room admission
and completion. A successful local job remains a delivery result, not a claim
that a physical microphone confirmed playback.

## Primary receiver research and its limits

The physical output is a Sonos SYMFONISK Tablelamp advertising AirPlay 2 PTP and
buffered-audio support. The sender uses the persistent shared OwnTone libairptp
daemon. Its receiver clock startup is therefore relevant, but the exact first
probe and acoustic onset during the failed job were not captured.

Music Assistant's primary AirPlay implementation was reviewed at commit
`8e79242996b7ef52352ee49d390e6db434bf88a6` (2 October 2026). It sends initial
placeholder DMAP metadata unless real metadata has already been published,
and includes `RTP-Info`: its documented Sonos observations include withholding
audio without metadata and rejecting metadata without that header. This
supports correcting OwnTone's missing operation. It does not, by itself, prove
that the omission accounts for every lost opening word on this SYMFONISK.
[Pinned design](https://github.com/music-assistant/airplay-cli/blob/8e79242996b7ef52352ee49d390e6db434bf88a6/DESIGN.md),
[native metadata sender](https://github.com/music-assistant/airplay-cli/blob/8e79242996b7ef52352ee49d390e6db434bf88a6/src/ap2_client.c#L4865)

The same implementation observes each receiver's continuous Delay_Req/Pdelay_Req
probe streak and reports clock startup independently of RTSP connection. Its
documented measurements include a Samsung first probe 1.078 seconds after
CONNECT and a Sonos Era 100 settling window of 1.7–2.3 seconds from the first
probe. This is a plausible additional explanation for a cold receiver becoming
audible after speech has begun. It remains an inference for the actual
SYMFONISK, whose settling window has not been measured here.
[Pinned README](https://github.com/music-assistant/airplay-cli/blob/8e79242996b7ef52352ee49d390e6db434bf88a6/README.md),
[probe-streak tracker](https://github.com/music-assistant/airplay-cli/blob/8e79242996b7ef52352ee49d390e6db434bf88a6/src/ap2_ptp.c#L1052)

A probe streak is not a receiver lock acknowledgement. That implementation's
2300 ms projection is a measured device policy. Its hardware-tested late-join
repair distinguishes joins to an existing timeline from fresh origin starts:
current code observes an origin shortfall without shifting that anchor because
moving one member can desynchronize the group. Therefore this investigation
does not add that fixed bound to every Shiri startup or copy its content-cut
behavior into a speech announcement.
[Primary PR 36](https://github.com/music-assistant/airplay-cli/pull/36),
[current origin handling](https://github.com/music-assistant/airplay-cli/blob/8e79242996b7ef52352ee49d390e6db434bf88a6/src/ap2_client.c#L4384)

Shiri pins libairptp at `7e2252e0258525b3480b54b1906038fee230e981`.
Its peer-add API returns after a local UDP control send, and exposes no live
per-peer probe snapshot. Its `last_seen` field updates on any incoming peer
packet, not specifically on a successful clock exchange. Receive/send packet
logging is compiled out; normal `airptpd -v` logs cannot retrospectively
establish the failed job's first Delay_Req. Peer registration and a clean RTSP
response therefore cannot close this clock evidence gap.
[Pinned libairptp API](https://github.com/owntone/libairptp/blob/7e2252e0258525b3480b54b1906038fee230e981/airptp.h),
[peer-add implementation](https://github.com/owntone/libairptp/blob/7e2252e0258525b3480b54b1906038fee230e981/src/airptp.c#L227),
[compiled logging switches](https://github.com/owntone/libairptp/blob/7e2252e0258525b3480b54b1906038fee230e981/src/ptp_msg_handle.c#L39)

## Bounded clock diagnostics if physical evidence requires them

OwnTone can gain clock diagnostics without replacing its output backend or
introducing another scheduler. An additive libairptp extension would record
monotonic first, last and third successful Delay_Req/Pdelay_Req exchanges,
response failures, peer identity and clock generation. It would accept only
registered, correctly sized/domain-matched messages and reset history on peer
retirement/re-add, clock replacement or a measured probe gap.

A bounded local query or versioned read-only snapshot would expose this state
to a worker, never block OwnTone's 10 ms audio thread, and report
unmeasured/cold/probing/stale rather than asserting acoustic readiness. NTP
outputs would explicitly report that this PTP observation is unavailable.
Repeated lost status queries must not turn a healthy established stream into a
false cold start. Tests would cover independent peers, malformed packets,
failed replies, first/third boundaries, gap reset, reconnect generations,
daemon-query loss and recovery, and the NTP case. This telemetry is a proposed
next measurement tool, **not implemented in this repair**.

## Physical acceptance still required

When the user is ready for house audio, test a cold idle receiver and a warm
repeat with the exact sentence. A microphone capture should correlate request,
first generated audio, metadata ACK, first PCM admission, first probe when
available, and the first audible sample. Confirm all opening and ending words,
incremental onset, music ducking/restoration and unchanged program metadata.
Repeat after speaker standby and across the intended output protocols. A
metadata ACK proves control acceptance; only that acoustic capture can prove
the receiver renders the complete prefix at the measured delay.

Until those checks pass, this work qualifies the local streaming and encoding
boundaries and repairs a known startup omission. It does not claim a completed
physical speaker latency or whole-house synchronization certification.
