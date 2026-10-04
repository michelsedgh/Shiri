# TTS readiness checkpoint — October 3, 2026

This checkpoint keeps the selected room speakers connected independently of
music and speech ownership, starts a smooth text-speech duck during room
preparation, and streams Qwen PCM as it arrives. It preserves the existing
AirPlay input, saved per-speaker clock selection and common presentation plan.

## Installed release

The live VM and Mac worker use the same 61-file wheel:
`d11563a487786772c957139b32501dad4b8c7d1f3211a555d1ea9583f369d3e9`.
OwnTone ends `outputclock1-duck1-warm1`, binary SHA-256
`3d31ad79d937a0661222f32a10945bccc958f4f8318e7a7f7a46fdd57c2b7ba4`.
The backend manifest SHA-256 is
`b22311c39e3a5f3cae2e0aa7d2fd68dddd74a0a5cf24a870efe365e4d3d5d387`.
Shairport remains `timed3-startup1-volume2`, binary SHA-256
`4e051902f8ea7095385367ca4260372bda4813b8e6a84bd50abe08b8eb2fb5d6`.

The coordinated VM rollout retired the previous actors and owned networks,
verified every installed package file, and kept schema4 unchanged. Living Room
retains revision 84, master volume 26, Sonos endpoint 92539824408726, balance 100,
offset 0 and requested/native NTP. Normal VM services remain enabled for boot.
The Mac LaunchAgent now explicitly preloads `qwen3-0.6b-customvoice`; previously
its default startup loaded Kokoro despite Qwen having been selected in the UI.
The first worker rollout detected that model mismatch and restored its prior
wheel. The second installed the qualified wheel and explicit Qwen startup.

Private VM recovery files are in
`/var/lib/shiri-tts-ready-checkpoint-20261003-03`; recovery retains the current
schema4 database rather than restoring older user intent. The Mac checkpoint
is `~/Library/Application Support/Shiri/TTS/checkpoint-tts-ready-20261003-03`.
No credentials or database copies belong in the public evidence bundle.

## Readiness and speech behavior

The default `adaptive` policy prepares enabled rooms at startup and retains
connections for five minutes after actual use. `ready` renews continuously;
`on_demand` uses playback and explicit hints. Internal 60-second guards renew
at 30 seconds, with bounded retry backoff. An external lease accepts 5–300 seconds
and can be renewed indefinitely while Shiri is healthy. Routing changes and
room shutdown revoke the old exact hold. Volume and balance preserve it.

An optional Nobly `presence` hint retains the connection without inference.
An `interaction` hint can also prime the shared loaded model's first chunk,
discard it and join a bounded decoder reset. Real speech takes priority, and a
slow optional hint HTTP response cannot hold its admission lock. There is no
default discarded-inference loop or continuous silent PCM keepalive.

Text speech starts its owned duck before its first PCM arrives. Configured
attack/release durations are 300/600 ms; errors and cancellation retire the exact
voice and restore its envelope. These are smooth software envelopes, not
measured speaker fade/onset values. The existing program timeline keeps
advancing. Quiet preparation alone does not lower music.

The final refresh changes only the optional model-prime timeout to a Python
3.10-compatible same-task deadline. Its preflight-only first attempt stopped
on a missing rollout-wrapper import before stopping or installing anything;
the corrected retry verified the same native binary and preserved current room
settings. Quiet endpoint verification on the final wheel received first PCM in
139.66 ms and completed reset in 185.25 ms, sent no room audio, and acknowledged
release. The recovery checkpoint above belongs to that corrected refresh.

## Live software measurements

The listening and streaming observations below belong to the preceding
readiness wheel
`50bd36241ad6dbcab880ca1c397f6cee77896cb49eb696fab51d7075af9722ec`.
The final wheel keeps its native transport and speech generation code unchanged;
its new quiet model-prime endpoint check is recorded separately above.

A controlled connection-only hold stayed connected for 35.69 seconds, beyond
the AirPlay feedback interval. The passive header-only capture observed 40
packets directly between the shared sender and Sonos, with 2156 transport
payload bytes total (60.4 bytes/s, excluding network headers). Separate speaker
broadcast discovery was excluded. No music or speech producer was admitted;
no packet payload or audio capture was retained. This is one device/window,
not an all-speaker network or power estimate.

The subsequent explicit quiet Qwen prime received its first PCM in 133.8 ms and
finished reset in 181.2 ms total. Its discarded 20 ms chunk never went to the room.
The following request said “Hello. This is Shiri speaking in your room.”:

| Software boundary | Observed time (job clock unless noted) |
| --- | ---: |
| Room preparation requested |0.77ms |
| Owned music duck requested |26.31ms |
| Backend preparation acknowledged |26.98ms |
| Model's first generated PCM (model clock) |59.73ms |
| First PCM received by VM |74.34ms |
| First PCM admitted to room |79.15ms |

All 4.64 seconds were delivered, with zero dropped speech frames and confirmed
worker cleanup. At the first progress observation, 40 ms had already reached the
room while model completion metrics were still absent. Model generation took
2319.96 ms (RTF 0.50); total delivery and cleanup took 4976.68 ms. Total duration
includes realtime utterance delivery and does not represent initial latency.

These observations do not measure first acoustic sound. The saved AirPlay
route still has B 500 ms/H 600 ms software lead; holding the connection does not
remove that lead or the receiver's internal buffering. The user confirmed the complete greeting, including “Hello,” on that
readiness wheel after the quiet hold. That listening result does not measure exact onset.
Power, amplifier standby, long-duration/network-fault behavior, Bluetooth
speaker-group behavior and whole-house microphone sync remain hardware
qualification work. Previous phone pause/volume acceptance is historical;
this update has no new human iPhone regression result yet.

## Validation

The final source passed 4529 Mac Python tests with 85 explicit/platform skips;
focused compatibility tests passed 190 with one skip on both Python 3.10 and
3.12. The unchanged frontend passed 67 tests without skips; Ruff and whitespace
checks passed. The initial Linux CI exposed Python 3.10's missing
`asyncio.timeout`; the portable deadline and production-equivalent fixture
readiness barrier fix that failure without increasing production timeouts.
The exact old deadline reproduces two meaningful failures in the corrected
Python 3.10 tests. Both the failed observation and final CI receipt belong in
the additive evidence bundle. The isolated Linux
build applied the exact duck/warm patches over the qualified outputclock1
base. Address/undefined sanitizers checked owned duck envelopes, legacy PCM
identity, fresh music prefixes,225 warm lifecycle and 20 actual JSON-parser
cases. The real FFmpeg/ALAC packetizer decoded 1183 packets/1665664 bytes exactly,
including long original-PTS gaps and preserved prefix/tail.

See [room readiness](ROOM_READINESS.md), [local TTS](LOCAL_TTS.md) and the
[live handoff](LIVE_TEST_HANDOFF.md). Additive evidence batch 0006 records the
exact source, binaries, rollout, quiet model results and live observations;
prior sealed batches remain unchanged.
