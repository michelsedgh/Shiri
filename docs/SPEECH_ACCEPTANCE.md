# Software speech acceptance

These limits were declared before the next qualifying runs on October 1.
They apply to the measured software route. Phone, radio and acoustic acceptance
remain a later phase. Historical failed or pending measurements stay unchanged.

| Measurement | Required limit |
| --- | --- |
| Encoder first source frame to final decoded reference origin | The entire corrected latency interval must stay within the actual selected OwnTone buffer plus selected output correction and 200 ms additional delay |
| Independently calibrated timestamp uncertainty | At most 32 ms for the complete origin bracket; this is the only permitted negative measurement allowance |
| Cold API offer to authenticated readiness response | At most 1,000 ms |
| Warm utterance request to its first encoded source frame | At most 100 ms on the same running peer, source and output incarnation |

The additional-delay allowance covers 20 ms Opus frames, the pinned aiortc
receiver's four-frame prefetch, the native 20 ms speech reserve, OwnTone's
10 ms mix tick, and conversion, dispatch and capture margin. It is an acceptance
ceiling, not a buffer setting or a reason to add delay. OwnTone's selected
output buffer and correction must be read from the admitted route; a common
relay horizon cannot substitute for that measurement.

Qualification also requires the complete emitted speech prefix, body, codec
tail and quiet interval; continuously advancing music; an untouched other
zone; exact request/source ownership; and complete cleanup. Cold and warm
idle/native routes and admitted negative, zero and positive corrections must
pass. Capture clocks must be calibrated before the offer. Waveform matching
must not fit a timing correction into the reported latency.

Report offer, ICE, emission, ducking, final speech onset and restoration
separately. Waiting deliberately before a warm utterance must not be counted
as its onset latency. Reusing a peer alone does not establish a warm output.

The current immutable declaration is
`/tmp/shiri-speech-release-budget-v2.json`, SHA-256
`a83b02d384edc1c2aa36a0e2fcf2637e0884fd51a4c62a78cb34dc0dceea646a`.
Before any qualifying replay, it corrects the original uncertainty limit:
20 ms describes one capture period, while the full independently measured
bracket also includes queue-origin spread, query uncertainty and analysis
grid margin. The 32 ms ceiling applies to that full bracket. The entire
latency interval must meet the unchanged 200 ms additional-delay limit.

The earlier, unqualified immutable declaration is retained at
`/tmp/shiri-speech-release-budget-v1.json`, SHA-256
`35a489b9ad71f6d6c12a727fc5127c5fde8774dc2f005f836d6717aa0267cfb3`.
The pinned aiortc 1.15.0 receiver was checked locally against its source;
audio uses `JitterBuffer(capacity=16, prefetch=4)`.

On October 2, the separately declared204 matrix passed cold/warm idle/native
cases at −2000, zero and +2000 ms correction on one admitted boot/profile.
The unchanged frozen evaluator qualified all 12 rows, including full independent
latency brackets, waveform completion and cleanup. The immutable declarations
and original finite reports remain unchanged. Earlier200 and202 results were
not substituted into this matrix. This establishes software speech performance;
smaller default buffers, actual-phone behavior and physical output remain
separate requirements.

After returning to the stock generic kernel, fresh candidate182 admission205
and encrypted playback/speech/volume/reload regression199 passed with complete
cleanup. Both exact installation rollbacks and normal VM shutdown also passed.
The204 latency matrix remains bound to its original boot/profile; live rollout,
actual iPhone volume and final handoff are still pending.
