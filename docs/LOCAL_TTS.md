# Local streaming speech

**October 3 startup qualification:** initial metadata, natural-EOF drain and
cold-music input repairs are installed. Cold idle speech still loses its opening
words with the Sonos output's automatic PTP timing. A controlled, temporary NTP
comparison preserved the entire phrase according to the user's listening test;
normal PTP configuration was restored afterward. The `outputclock1` build and
saved per-speaker timing controls are pending installation. See
[the diagnosis and evidence boundaries](SPEECH_STARTUP_DIAGNOSIS.md) and
[the installed-versus-staged handoff](LIVE_TEST_HANDOFF.md).

Shiri can generate speech locally from text and route it to one exact room. The
room's music continues while the mixer fades its gain down for speech and back
up afterwards. Nobly can use the same text API through an external room binding;
Nobly itself is not required to try speech in the web interface.

The input is a complete text request. The output is streamed audio where the
selected model supports it; incremental incoming text is not implemented.

## Architecture

The optional native macOS worker runs MLX on Apple Silicon. The Linux Shiri
router, including the Ubuntu VM, handles room selection, speaker preparation,
playback pacing and mixing. A VM does not provide the worker access to Metal.

For direct PCM, the router establishes its sample calendar once from the room
AudioWorker's first admitted-frame receipt over private local RPC. Both use the
same Linux monotonic clock. The receipt's exact marker and integer timestamp
must fit the first-send/reply interval. Initial dispatch time precedes the
receiver's stream; reply delay after admission and later stalls still count as
lateness. The Mac's model clock never sets this playback calendar.

The worker keeps one model loaded in a separate spawned process and warms it
without playing the warmup audio. Generated mono audio passes through one
stateful resampler per utterance to become 48 kHz signed 16-bit PCM. The worker
streams PCM records over its authenticated private HTTP connection; the router
delivers them in frames of at most 20 ms through the existing room audio runtime.
The text path does not add the WebRTC speech input's jitter prefetch buffer.

Room backend preparation starts alongside generation. Shiri forwards speech
once both audio and the room backend are ready. First generated audio therefore
does not establish when a speaker makes sound. Output protocol buffering, a
sleeping speaker, the generated waveform's leading quiet and network conditions
can all contribute to the time a person hears speech.

The October 3 cold NTP comparison verified incremental delivery: first room PCM
dispatch occurred at 1,036 ms, and 179 ms of speech had already been delivered
by 1,217 ms while synthesis took 2,715 ms. The complete 4.64-second phrase was
delivered with no reported speech drops. This establishes streaming before
generation finishes, together with one successful human prefix observation; it
does not measure acoustic onset or multi-speaker alignment. The corresponding
cold PTP capture showed the first receiver timing probe at 2,991 ms, later than
the sender's intended first speech presentation. Receiver clock lock remains
unmeasured.

## Models and controls

| Model ID | Default voice / language | Controls | Audio delivery in MLX Audio 0.5.7 |
| --- | --- | --- | --- |
| `qwen3-0.6b-customvoice` | `ryan` / `English` | Nine voices, ten languages | Incremental codec audio; 80 ms generation interval by default |
| `kokoro-82m` | `af_heart` / `a` | 54 voice presets; speed 0.5–2.0 | Completed text segments; first segment can require the whole short phrase |
| `soprano-1.1-80m` | `default` / `en` | One English voice | Completed phrase |

Kokoro is the default for predictable phrase duration and its voice/speed
controls. English replies are synthesized at complete sentence boundaries with
one continuous output resampler. The first sentence can begin while later
sentences are still being generated; no words are cut to force a smaller chunk.

The first tested Qwen/MLX inference configuration produced stretched vowels or
extra tails, including natural completion after 12–40 seconds for a short input.
Fixing the seed reproduced that problem; reducing temperature did not fix it.
Those trials describe that checkpoint and inference configuration, rather than
the Qwen model family. Source comparison subsequently identified a difference
between MLX's text input preparation and Qwen's official CustomVoice default.
The compatibility repair and its measurements are recorded below. Soprano is
another compact English option.

Qwen's adapter emits audio before completing a phrase.
Its registered voices are `ryan`, `aiden`, `serena`, `vivian`, `uncle_fu`,
`ono_anna`, `sohee`, `eric` and `dylan`. Languages are English, Chinese,
Japanese, Korean, German, French, Russian, Portuguese, Spanish and Italian.
The 0.6B entry does not advertise instruction or speed control. The upstream
capability table qualifies instruction control for the 1.7B CustomVoice model.
[Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS)

### Qwen input compatibility

Qwen's official CustomVoice API defaults to conditioning on the complete text
before generating codec tokens. MLX Audio 0.5.7 instead uses its simulated
streaming text preparation. Shiri receives complete text requests, so its
adapter now uses the official full-text input layout while retaining MLX's
incremental audio decoder. This does not wait for the entire waveform.
[Official inference default](https://github.com/QwenLM/Qwen3-TTS/blob/022e286b98fbec7e1e916cb940cdf532cd9f488e/qwen_tts/inference/qwen3_tts_model.py),
[official input layout](https://github.com/QwenLM/Qwen3-TTS/blob/022e286b98fbec7e1e916cb940cdf532cd9f488e/qwen_tts/core/models/modeling_qwen3_tts.py),
[MLX input preparation](https://github.com/Blaizzy/mlx-audio/blob/94c7716212b2228f178d2f9c7619a591fd1b0b78/mlx_audio/tts/models/qwen3_tts/qwen3_tts.py)

The compatibility helper adapts only the loaded CustomVoice instance. It checks
`mlx-audio==0.5.7` and SHA-256
`0d9437e4f08680d7bf8cb7bf3b44c8e3de37ad9edf025f50dba37dface2e6902`
of its Qwen inference source before adapting it. An incompatible installation
fails clearly; installed library files and global model classes are untouched.
The talker uses the checkpoint's temperature 0.9, and each utterance resets
sampling to seed 42. MLX applies temperature 0.9 to both talker and codec
predictor sampling; other sampling options retain the upstream defaults. These
are reproducible adapter defaults, not public text API controls.

A controlled comparison used the same cached checkpoint, seed, temperature and
texts on both paths. Full-text preparation reduced Ryan's living-room phrase
from 4.32 to 2.72 seconds and a longer reply from 31.92 to 16.72 seconds;
Aiden's long reply reduced from 20.48 to 14.48 seconds. All seven repaired-path
clips ended naturally below their token caps. First normalized PCM was
54–69 ms except one new-shape cold case at 113 ms. Content prefix/tail checks
used a small ASR model and retained the complete WAVs; they establish gross
content rather than subjective voice quality. Other voices/languages and
physical speaker onset remain to be qualified. Qwen stays selectable with an
experimental notice; Kokoro remains the default.

The guarded production loader subsequently generated nine clips, including
two repeated living-room requests. All completed naturally and matched the
corresponding content-checked prototype WAVs byte for byte. Short replies
emitted normalized PCM in 54–56 ms; 40-word replies in 70 ms. Direct native-backend cancellation
after a 1.039-second prefix discarded its pending tail, and the following
utterance again matched the clean reference. These are native generation
measurements; the room route adds its own preparation and delivery timing.

Kokoro language codes are `a` (American English), `b` (British English),
`e` (Spanish), `f` (French), `h` (Hindi), `i` (Italian), `p` (Brazilian Portuguese),
`j` (Japanese) and `z` (Mandarin). Some require additional language dependencies;
the setup below provisions English. Voice and language selections are validated
against the registry, and the catalog supplies the available voice names.
The current Kokoro and Soprano adapters produce a segment after synthesizing it;
passing a streaming option does not turn their current MLX implementations into
incremental generators.
[Kokoro adapter](https://github.com/Blaizzy/mlx-audio/blob/main/mlx_audio/tts/models/kokoro/kokoro.py),
[Soprano adapter](https://github.com/Blaizzy/mlx-audio/blob/main/mlx_audio/tts/models/soprano/soprano.py)

The built-in checkpoints are pinned:

| ID | Hugging Face repository | Revision |
| --- | --- | --- |
| `qwen3-0.6b-customvoice` | `mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-4bit` | `08c72cad5e2fd0f41730c8bd1f28149585e46361` |
| `kokoro-82m` | `mlx-community/Kokoro-82M-bf16` | `a71e4d38b236d968966a2002c4c895dbd12b1c3c` |
| `soprano-1.1-80m` | `mlx-community/Soprano-1.1-80M-bf16` | `745350c27f356c3910eebcb49e29760dcf6a643c` |

The registry records Apache-2.0 for these models. Soprano's license is confirmed
by its original model card and upstream license, even though the conversion's
card does not repeat all license metadata.
[Soprano model card](https://huggingface.co/ekwek/Soprano-1.1-80M),
[upstream license](https://github.com/ekwek1/soprano/blob/main/LICENSE)

Voice cloning, reference-audio uploads and arbitrary remote model loading are
not implemented. OmniVoice was researched but is not an installed or qualified
backend in this release.

## Native worker setup

Use a separate Apple Silicon Python 3.12 environment. The durable location on
the current Mac is `/Users/homr/Library/Application Support/Shiri/TTS`; use
`$HOME/Library/Application Support/Shiri/TTS` for another account. From a Shiri
checkout:

```sh
TTS_DIR="$HOME/Library/Application Support/Shiri/TTS"
mkdir -p "$TTS_DIR"
python3.12 -m venv "$TTS_DIR/venv"
"$TTS_DIR/venv/bin/python" -m pip install '.[tts,tts-kokoro]'
```

The optional dependency set pins `mlx-audio==0.5.7`, MLX `0.32.3`, Transformers
`5.18.0` and PyAV `16.1.0`, with NumPy `>=1.26.4,<3`.
The generated [worker dependency lock](../install/tts_requirements.lock) records
the resolved dependencies and hashes separately from Linux audio installation.
Actual model measurements used MLX/MLX Metal
0.32.3, NumPy 2.5.3 and PyAV 16.1.0. Keep the model environment separate from the
Linux router's environment.

Kokoro needs English phonemization dependencies and a preinstalled spaCy asset:

```sh
"$TTS_DIR/venv/bin/python" -m pip install 'misaki[en]==0.9.4' \
  'en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl#sha256=1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85'
```

Shiri checks that the English asset is installed before using Kokoro. Install it
during setup rather than allowing the phonemizer to fetch packages during the
first real speech request.

Create a random worker credential once. Keep its file private, owned by the
worker account, with mode `0600`; the worker requires at least 32 characters and
rejects symlinks and whitespace in the credential. For example:

```sh
"$TTS_DIR/venv/bin/python" -c 'import os, pathlib, secrets; p = pathlib.Path(os.environ["HOME"]) / "Library/Application Support/Shiri/TTS/worker-token"; fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.write(fd, (secrets.token_hex(32) + "\n").encode()); os.close(fd)'
```

Run the worker, initially allowing only the registered pinned model downloads:

```sh
"$TTS_DIR/venv/bin/shiri" tts-worker \
  --host 0.0.0.0 --port 8091 \
  --token-file "$TTS_DIR/worker-token" \
  --cache-dir "$TTS_DIR/cache/hub" \
  --preload kokoro-82m --allow-download
```

`--cache-dir` is the Hugging Face **hub** directory, not its parent. Once the
desired models are cached, run without `--allow-download`; normal loads then
require the pinned local assets. The Qwen assets are about 1.7 GB, Kokoro about
0.33 GB plus voice assets, and Soprano about 0.22 GB. The worker only holds one
model at a time and applies a 3 GiB MLX allocation limit.

Configure the router with the Mac's reachable address and a private copy of the
same worker credential:

```sh
SHIRI_TTS_WORKER_URL=http://MAC_LAN_ADDRESS:8091
SHIRI_TTS_WORKER_TOKEN_FILE=/etc/shiri/tts-worker-token
```

These are configuration values: replace `MAC_LAN_ADDRESS` with the actual Mac
address. The VM's `localhost` is the VM, not the native Mac worker. Keep port 8091
on the trusted local network. The worker always requires its separate bearer
credential, including when the router's temporary LAN administration token
requirement is disabled. HTTP provides no transport encryption; use HTTPS or a
protected network when that is required.

An optional macOS LaunchAgent can keep the native worker running after login.
That startup is separate from the VM's Shiri service. A booted VM still needs
the Mac worker to be running and its address reachable. Preload completes
asynchronously: wait until the catalog reports `ready` for the selected model.
The current Mac has that LaunchAgent installed as `org.shiri.tts`, with persistent
assets under `/Users/homr/Library/Application Support/Shiri/TTS`. The VM reaches
it through `Homrs-MacBook-Air.local:8091`, so a normal Mac IP change does not
require editing a numeric worker address. This was checked from the real VM.

## Trying speech

In the web interface, open **Speech voices**, choose a model and load it. Run
**Quiet model measurement** first; it generates audio without
opening a room's speakers. A completed measurement has an explicit browser
preview for listening on the browser's audio device. Use a room's **Speak** button when ready to hear the
result. The selected room must be enabled and have assigned speakers. **Stop**
cancels the active job.

Stopping first retires the exact room speech stream, then waits for the private
worker's decoder cleanup before releasing the generation slot. Closing its HTTP
connection alone is insufficient. A bounded read-only readiness check confirms
worker retirement; it never retries generation or cancels another model job.
The same owned cleanup survives Stop arriving during natural completion or a
disconnected cancelling caller. Unconfirmed cleanup is reported explicitly.

Switching models performs a new load and warmup. Speech and quiet measurements
share one active job slot; changing models during an active job is refused.

## Public text API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/v1/tts/models` | Model capabilities and worker state |
| POST | `/api/v1/tts/models/load` | Load `{ "model_id": "kokoro-82m" }`; returns 202 |
| POST | `/api/v1/tts/benchmark` | Quiet generation measurement; returns a job with 202 |
| POST | `/api/v1/rooms/{room_uuid}/tts` | Speak in one exact room; returns a job with 202 |
| POST | `/api/v1/nobly/rooms/{external_id}/tts` | Resolve one Nobly binding, then retain that room UUID for the job |
| GET | `/api/v1/tts/jobs/{request_id}` | Read job state, metrics and error |
| DELETE | `/api/v1/tts/jobs/{request_id}` | Cancel the job |
| GET | `/api/v1/tts/jobs/{request_id}/sample.wav` | Explicit preview of a retained completed quiet measurement |

Use a fresh 32-character lowercase hexadecimal `request_id` for each intended
utterance. A caller should generate it before sending the request:

```json
{
  "request_id": "00112233445566778899aabbccddeeff",
  "text": "The timer is ready.",
  "model_id": "kokoro-82m",
  "voice": "af_heart",
  "language": "a"
}
```

Omitted model, voice and language use the documented defaults. `speed` defaults
to 1.0 and may vary only for a model advertising speed support. Built-in models
reject instruction control. Clients select registry IDs, never model URLs,
weight files or local paths. Encode external room IDs as URL path components.

Job states are `queued`, `generating`, `playing`, `completed`, `cancelled` and
`failed`. A benchmark never enters room playback. Job completion means the
software delivery finished; it does not certify that a microphone heard it.

If the submission response is lost, query the same `request_id`. Repeating the
same request with the same ID returns the existing job, while changing its text,
target or options conflicts. Do not immediately send a new ID after a timeout:
the original utterance may already be playing. Receipts are the latest 32 jobs
in memory, so this recovery guarantee ends after eviction or a router restart.
An intentional repeat of a completed utterance needs a new ID.

## Bounds and cancellation

Text is limited to 2,000 characters; generated audio to 60 seconds; generation
to 180 seconds; model load and prewarm to 120 seconds. The backend defaults to
512 tokens and permits 32–750 tokens for trusted internal generation requests.
The public text API does not expose that token override or the generation
interval. Reaching a model's token cap is an explicit failure rather than a
successful truncated utterance. A model may fail on a long request before any
character or duration bound is reached.

There is one active generation across rooms and benchmarks. Requests are not
silently queued behind another utterance. Normal cancellation retains the model
only after the child closes its generator, discards resampler history, resets
the decoder and acknowledges retirement. The parent keeps one owned pipe reader
and drains queued old PCM before accepting a successor. An uncertain error or
missing acknowledgment within two seconds terminates the process; select
**Load model** again after that fallback. Normal completion also keeps it warm.
Stopping drops unsent audio and restores speech ownership, but already delivered
speaker or protocol buffers can still contain audio briefly.

Completed quiet benchmarks expose an explicit browser audio preview. Preview
audio never autoplays or selects room speakers. At most 12 MiB of completed
samples are retained in API memory, plus the one bounded active generation;
old samples expire while their job status remains available. Failed, partial,
cancelled and room-speech jobs do not expose a preview. Restarting the API
clears this ephemeral history. `GET /api/v1/tts/jobs/{id}/sample.wav` uses the
same API authentication policy as the job itself.

## Measured latency and its meaning

Private, quiet measurements ran on this Mac's M3 with 8 CPU cores and 16 GiB
memory while the existing four-vCPU Ubuntu VM was running. Models ran
sequentially. No speakers or microphones were used for these measurements.
The first table describes the initial inference trials, before the Qwen
compatibility repair above and Kokoro's natural sentence delivery. Those
initial favorable samples did not expose Qwen's later phrase-length failures.

| Model / mode | Warm first native PCM | Warm generation RTF | Peak measured process RSS |
| --- | --- | --- | --- |
| Qwen, 80 ms incremental chunks | 50–53 ms over five repeated samples | 0.49–0.50 | about 1.9 GiB |
| Kokoro, completed phrase | 189–342 ms depending on phrase | 0.08–0.11 | about 780 MiB |
| Soprano, completed phrase | 119–201 ms depending on phrase | 0.05–0.08 | about 691 MiB |

RTF is generation time divided by audio duration; below 1 means faster than
playback. Phrase engines can have a lower overall RTF while waiting longer to
emit their first audio. RSS is process memory, not a measurement of the entire
Mac's incremental unified memory use.

Actual Shiri backend checks, including the stateful 48 kHz resampler, measured
first PCM at **50.5 ms for Qwen**, **280.8 ms for Kokoro**, and **146.4 ms for
Soprano** on their respective sample utterances. Their completed audio durations
were 3.600, 3.525 and 2.688 seconds. Resampler tails were preserved once; Qwen
completed naturally with 45 tokens, below its cap. These are local backend
measurements, not HTTP, room-admission or acoustic latency measurements.

After sentence delivery was added, a three-sentence American-English Kokoro
reply emitted first normalized PCM in 178 ms while later sentences continued
generating, with complete ordered content and one final resampler tail. First
use of the British-English pipeline took 1,089 ms on a different voice; model
and language warmup matter. Short real-HTTP Kokoro requests took 156–330 ms to
first PCM. The system's quiet measurement uses the selected model and voice so
the operator can inspect that route rather than assume every voice is warmed.

Cold starts were much slower. Qwen's first model load took 13.7 seconds and its
first generation about 2.1 seconds to PCM. Soprano's initial load took 2.6 seconds
and first generation about 0.97 seconds. Kokoro's first generation took about
20.5 seconds while it initialized its text pipeline and acquired its English
asset. Explicit asset installation and silent prewarm keep those steps out of
normal user speech. File caches, compilation and contention change cold timings.

The following metrics answer different questions:

- `first_pcm_ms`: generation start to the first normalized PCM emitted.
- `first_non_silent_pcm_ms`: generation start to emission of the first chunk
  containing samples above the measurement threshold.
- `leading_silence_ms`: quiet samples at the beginning of the generated waveform,
  using a threshold of −60 dB relative to full scale.
- `backend_ready_ms`: router job start to confirmation that the room speech
  backend is connected and its local startup contract completed. This is not
  a receiver clock-lock or acoustic-readiness measurement.
- `first_worker_pcm_received_ms`: router job start to its first received worker
  PCM record, before waiting for the room. Available while generation continues.
- `received_audio_s`: duration of validated PCM consumed from the worker so far.
- `delivered_audio_s`: duration accepted by the room so far, or retained in the
  sample for a quiet benchmark. A refused frame does not advance it.
- `room_admission_ms`: router job start to acceptance of its first room PCM frame.
- `first_pcm_dispatch_ms`: router job start to sending its first room PCM RPC.
- `first_pcm_rpc_ms`: elapsed first-frame RPC time, including dispatch and reply.
- `worker_cleanup_confirmed`: whether bounded worker retirement was observed
  before the job became terminal and its generation slot was released.
- `total_ms`: the full delivery and cleanup duration. Room speech is submitted
  at playback speed, so a 4.64-second reply takes roughly that long to send
  after preparation. This field does not measure startup latency.

In the initial inference trials, a Qwen short sample had 17.5 ms of leading quiet. A longer paragraph emitted its
first PCM after about 51 ms but contained about 432 ms of leading quiet audio.
The Kokoro and Soprano adapter samples had about 304 ms and 63 ms respectively.
Returning a chunk quickly does not remove quiet generated by the model.

After the Qwen input repair, Ryan's short and longer examples had about 2.6 and
8.3 ms of leading quiet. Aiden's examples had about 212 and 256 ms. These are
waveform observations for the retained texts and voices, not a guarantee that
every reply is audible at the first generated chunk.

Generation and router timings have different starting clocks; do not subtract
or add them as if they were consecutive stages. Generation timing can include
consumer backpressure during actual paced playback. Use quiet measurements for
model comparisons and room metrics for delivery diagnosis. An acoustic test
with room microphones remains necessary to establish heard latency and sync.

The final live VM sequence completed 14 software cases with Kokoro and repaired
Qwen: natural replies, cancellation, immediate successors, model switching,
quiet preview generation and replies after 16 seconds of idle following both
EOF and cancellation. Every case confirmed worker cleanup and preserved the
same actors, saved room volume and speaker assignment. Warm Qwen first room
admission was 64–108 ms in those retained short-reply cases; after idle transport
release it was 645–673 ms. Initial Kokoro cold transport preparation took about
1.07 seconds. These are bounded route observations on one configured AirPlay
speaker, with no microphone or human sound-onset observation. Neither a 55 ms
native model result nor a 64 ms room admission establishes audible latency.

## Idle-room backend repair

An actual room request exposed a separate startup defect after earlier speech
finished. OwnTone had suspended the original pipe input and was waiting for a
six-second refill before resuming it. Synthesizing a realtime silent pipe bed
could not meet Shiri's five-second speech preparation deadline. Increasing that
deadline would retain the refill latency and age the pipe's presentation anchor.

The additive `idle1` backend patch instead uses OwnTone's existing output-only
speech clock for an idle source, as it already does for a retained paused phone
source. The Python mixer emits speech through the authenticated speech output
and suppresses the competing idle pipe bed. Genuine queued music retains
priority and its original presentation. Finite expiry restores the existing
idle output cleanup even if speech was prepared but never mixed.

The patch changes no music buffer, setup deadline or speaker transport delay.
It requires the matching rebuilt OwnTone and Shiri package; runtime preflight
rejects the old `event1` binary. The native checker reproduces the failure on the
exact old source, then validates the repaired source with address/undefined
behavior sanitizers while preserving the strict historical source validators.
See [the native patch notes](../install/patches/README.md) and
[the current live checkpoint](CHECKPOINT_2026-10-03.md) for build and route proof.

## Adding a qualified model

`--registry /absolute/path/models.json` accepts a trusted local JSON file:
`{ "version": 1, "models": [...] }`. Each entry uses `ModelSpec.to_dict()`'s
schema, names one of the implemented backend families and supplies an immutable
40-character revision. The file must be owned by the current account or root
and must not be group or world writable. Built-in IDs cannot be replaced.

Adding an entry does not qualify a new architecture: verify its actual voice,
language, token-completion and streaming behavior, memory demand, license and
dependencies first. A larger instruction-capable CustomVoice model can be a
separate qualified entry; arbitrary inference code and reference-audio cloning
are outside the supported adapters. Capability metadata must match the loaded
checkpoint rather than the name of the model family.

The focused registry and backend tests cover trusted pins and capabilities,
finite mono PCM, stateful resampling and tail preservation, token exhaustion,
cancellation cleanup and leading-quiet measurements. Retain model revisions,
dependency versions, WAV samples and raw timing receipts when comparing a new
backend; listening quality and real speaker behavior need separate validation.
