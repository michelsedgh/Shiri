# Speech startup repair checkpoint — October 3, 2026

This records the earlier **uninstalled** checkpoint at `2aa45cd`. Its live-state
statements describe that point in time. The later installed music/speech repair
is recorded in [the cold playback rollout](CHECKPOINT_COLD_PLAYBACK_2026-10-03.md).

This candidate addresses the report that an idle Living Room announcement lost
the beginning of `Hello. This is Shiri speaking in your room.` The investigation
kept the running house system unchanged and used saved logs, private sockets,
generated files and isolated native captures. No test audio was played through
the house. The candidate is **not installed**, and acoustic acceptance remains
pending.

[The diagnosis](SPEECH_STARTUP_DIAGNOSIS.md) records the original failure,
measurements, source research and each evidence boundary. This checkpoint
supersedes any interpretation of the earlier software route passes as proof
that the physical speaker rendered a complete announcement.

## Resulting behavior

The direct text route already delivered incremental PCM. It did not wait for
the whole waveform. New regressions hold generation completion closed until
the first PCM arrives over real HTTP and until room delivery has begun. Quiet
Qwen captures retain all opening words. Native capture additionally verifies
the actual output resampler, AirPlay packetizer and ALAC encode/decode path.

Cold idle speech had no initial track metadata because OwnTone's output-only
bed has no database queue item. The additive `startupmeta1` layer sends a small
DMAP placeholder on Shiri's framed path when text metadata is requested and
real program metadata is absent. The receiver must acknowledge it before the
final volume step and CONNECTED receipt. The request uses the initialized RTP
position; it neither creates a fake queue item nor replaces real metadata.
An acknowledgement log supports correlation with a later microphone capture.

A separate natural-EOF defect could seal admission before the final PCM
datagram was polled. The additive `drain1` layer drains the bounded readable
burst before FINISH and flushes the converter/partial packet with bounded
past-interval silence. Cancellation still discards pending media, exact
ownership guards remain enforced and genuine queued music takes precedence.
This corrects a tail defect and does not establish the cause of missing
opening words.

The API and interface now expose audio received from the worker and audio
accepted by the room while generation is in progress. Output connection is
labelled separately from playback. Delivery and cleanup duration includes
paced transmission of the reply; it is not time to first sound. A 4.64-second
waveform takes about 4.64 seconds to submit at playback speed even if generation
is faster than real time.

## Timing interpretation

The failed job generated its first PCM at 157.975 ms and completed generation
in 2440.063 ms for 4.640 seconds of audio, a 0.525876 real-time factor. Room
admission was 622.565 ms and total job time was 5522.342 ms. Model and room
measurements use different origins; their counters cannot be subtracted into
an exact end-to-end interval.

Two quiet authenticated Qwen HTTP captures of the exact user text delivered
their first PCM at 460.947 and 444.616 ms. Both full WAVs were byte-identical,
and independent offline CPU ASR retained the opening phrase. Earlier 54–69 ms
controlled model trials used different requests and excluded receiver startup.
They are not a guarantee of that latency through the current room route.

No universal startup delay, enlarged output buffer, gain boost or replacement
audio scheduler was added. Receiver clock lock and acoustic onset are still
unmeasured. If complete first playback still fails after the metadata repair,
the diagnosis specifies bounded PTP telemetry and microphone correlation as
the next evidence to collect.

## Software validation and retained evidence

| Check | Result and boundary |
| --- | --- |
| Complete portable Python suite | 4,211 passed, 85 explicit/platform skips; live-device opt-ins disabled |
| Real HTTP generation and room socket capture | First PCM delivered before gated EOS; all synthetic and captured room samples retained |
| Native player lifecycle | 486,512 ASan/UBSan checks on host and isolated Linux, including natural EOF, fractional timer intervals, cancellation and music priority |
| Native metadata startup | 208 ASan/UBSan checks on host and isolated Linux, including ACK ordering, callback ownership, rejection and saved pairing preservation |
| Actual resampler, AirPlay packetizer and ALAC round trip | Four cases, 1,168 packets, 1,644,544 decoded stereo bytes matching encoder input; no remaining nonzero tail |
| Complete isolated native build | Compile and link passed; no installation or live output |
| Frontend | 57 unchanged checks passed in the full run; the corrected room progress check passed separately; push CI runs the complete frontend suite |
| Static checks | Ruff, shell syntax, JavaScript syntax and whitespace passed |
| Candidate package | Wheel built; all 60 package files match the checkout byte for byte; not installed |

The final candidate native binary SHA-256 is
`b0f1f77d2b01d9a86dc1b8b931a81adc2bfdf0c30081ca0710b324c6e0f6e67e`.
The uninstalled candidate wheel SHA-256 is
`c568d3cf8f889366cde0aaf598a162f30abcd521479272302cccd46ac4e043b3`.
The final drain and startup-metadata patch digests are recorded with their
strict source guards in [the native patch documentation](../install/patches/README.md).

The final native fixture unit attests its loaded isolation settings while
active: private network, devices and temporary directory, inaccessible live
house runtime paths, finite CPU/memory/time limits and a separate network
namespace. The post-exit observer's unloaded-unit defaults were not treated as
evidence of those active settings; both observer outcomes are preserved.

Raw receipts, WAVs, failed proposals, completed test logs, native build inputs
and the candidate package are retained in the additive `batch-0004` under
`/Users/homr/Documents/Shiri-Validation/release-2026-10-03` with an indexed
byte-count/SHA-256 manifest. Earlier batches remain immutable. The exact pushed
commit and [Linux CI](https://github.com/michelsedgh/Shiri/actions/workflows/checks.yml)
outcome are recorded alongside the checkpoint evidence.

Retained unsuccessful runs include stale frontend label assertions, a launcher
that bypassed the virtual environment, and a full portable run whose temporary
directory under `/Users` failed the strict ancestor ownership policy. The final
run used the verified protected macOS temporary directory. No policy was
weakened. Native regressions separately retain the pending-datagram failure,
the too-short initial EOF flush proposal and the metadata error's saved-pairing
side effect before their corrections.

## Running system and later acceptance

At this checkpoint the live VM remained on the previous `idle1` backend, binary SHA-256
`0be833a0780a55b5a4ea53fea2aaf4ed2a9bbfa9c885ebf1708d20a12e4cef47`,
and the previous wheel SHA-256
`064fd36922f49f5ae735013206ea0b88856ab13a8edbcde1de2b3522bf29f3f9`.
Its saved room volume, speaker assignment, service units and native actors
were not changed by this investigation. The Mac model worker was not upgraded.

The candidate requires the complete `idle1-drain1-startupmeta1` native marker;
runtime preflight rejects partial or misleading markers before launch. Native
patches remain additive and invert only their own layer before invoking the
unchanged historical source validators. A coherent package/backend installation
is required when the candidate is deployed.

Later physical acceptance must cover complete cold and warm announcements,
speaker standby, cancellation, music ducking/restoration, real program metadata
and the intended output protocols. A microphone should correlate metadata ACK,
PCM admission and audible onset. The software checks do not certify acoustic
latency, whole-house synchronization or a Sonos clock-lock state.
