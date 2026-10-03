# Initial music onset diagnostics

The reported issue is **fast AirPlay connection followed by slower first sound
than a later pause/resume**. It is not an observed delay in the connection
spinner. The cause remains unattributed until a fresh phone session is captured.

The audio worker exposes `music_startup` in its private `health` response.
The API also returns this bounded trace from
`GET /api/v1/rooms/{room_id}/diagnostics`, after checking that the same room
launch still owns the audio worker. An unavailable worker produces an explicit
diagnostics error; reading the trace does not start playback.
It retains at most eight native BEGIN/FLUSH generations. Records disappear when
that worker restarts; their clocks cannot be compared across worker or VM boots.
Record identities are worker-local sequence/epoch/generation counters. No audio,
phone address, source token or arbitrary exception text is retained.

| Observation | What it establishes |
| --- | --- |
| `requested_monotonic_ns` | The worker observed a valid native BEGIN or FLUSH; this is not the phone's Play-button time |
| `backend_prepare` | Time spent in the existing exact-source output preparation request, including its selected-output readiness barrier |
| `grant_ready_monotonic_ns` | Native source admission completed |
| `grant_sent_monotonic_ns` | The native socket GRANT was sent; null for a direct fixture or failed reply |
| `first_pcm` | First accepted native block, original clock/presentation, measured clock uncertainty, declared relay lead and original frame position |
| `first_nonzero_input_pcm_monotonic_ns` | First accepted input block with a nonzero sample; this still does not establish audible sound |
| `first_fifo_write` | First actual PCM bytes written to the timed FIFO, with that block's preserved output presentation |
| `fifo_dropped_bytes_before_first_write` | PCM refused by FIFO backpressure or absence of a reader before the first successful write |

A cold BEGIN can perform downstream transport setup. A later FLUSH can reuse
already-ready outputs. The trace separates that preparation interval from the
arrival of the first PCM and its intended presentation. Shairport also waits
for the source's presentation calendar before releasing native audio; an early
phone connection alone does not establish when its first sound was scheduled.
The pinned upstream describes source-negotiated latency and preserves source
timing. [Shairport timing documentation](https://github.com/mikebrady/shairport-sync/blob/7bad231c18368dbd26f298577f6210e36e4b0797/README.md#latency-stuffing-timing).

Compare a new session's BEGIN with its later FLUSH records. A FLUSH may happen
at the beginning of a pause, so a long FLUSH-to-PCM interval can contain the
user's intentional pause. A resume that sends PCM without a new FLUSH does not
create a separate take. These records therefore help attribute server stages;
they cannot alone measure phone Play-to-sound latency. Actual downstream
emission and microphone onset remain separate measurement points.

The automated fixture uses real private Unix sockets and a real named FIFO,
with controlled backend acknowledgements. It verifies cold versus warm
preparation accounting, first silent versus nonzero blocks, FIFO refusal,
immutable original presentation, bounded history and rejection of stale
callbacks. Controlled preparation times are test inputs, not measurements of a
real speaker. The observer sends no new backend requests and changes no audio
lead, volume, source-readiness barrier or timer scheduling.

For the later iPhone check, capture the trace from a fresh connect and first
play, then pause and resume on the same source without changing web volume.
Record the app used and whether the source stayed connected. A microphone or
separate recording of button action and sound is needed for acoustic onset.
There is no need to perform that phone check before the automated work.
