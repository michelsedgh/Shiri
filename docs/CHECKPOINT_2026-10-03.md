# Streaming speech checkpoint — October 3, 2026

The later [speech startup investigation](CHECKPOINT_SPEECH_STARTUP_2026-10-03.md)
records a physical playback failure reported after this checkpoint. The route
passes below establish software delivery; they do not establish complete
audible speech at the receiver. The new repair candidate is tracked separately.

This checkpoint adds optional local text-to-speech generation to Shiri and
repairs idle speech at the OwnTone boundary. The working system remains one
AirPlay receiver per zone, with selected physical outputs, one phone/web room
master, saved speaker balancing and targeted speech over advancing music.
Nobly is a future client. Chromecast input, Bluetooth input and speaker standby
policies remain outside this release.

## Delivered behavior

- An exact-room text request generates speech on the native Apple Silicon worker
  and streams normalized PCM to the Linux room runtime. An external Nobly room
  binding is resolved once under admission rather than reinterpreted midstream.
- The interface loads/selects voices, measures generation without opening
  speakers, previews completed quiet results, speaks to an enabled room and
  cancels an exact request. Model capabilities control the available settings.
- One model and one generation job run at a time. Model/credential/registry
  bounds, finite audio, producer ownership, source changes and stale frames
  have explicit failure and cleanup behavior. Complete text is the input;
  streaming incoming text and voice cloning are not implemented.
- Speech uses OwnTone's existing late mixer with smooth music duck/restore. The
  direct PCM path adds no WebRTC prefetch. Backend preparation runs alongside
  generation; playback still waits for actual output readiness and protocol
  buffering.
- A bounded read-only music-onset trace separates backend preparation, source
  admission, incoming PCM and actual FIFO delivery. It preserves the original
  music calendar and makes no phone-button-to-sound measurement claim.

## Qwen inference repair

The initial MLX CustomVoice trials produced stretched phrases or extra tails.
The official API defaults to full-text conditioning; MLX Audio 0.5.7 prepared
simulated streaming text instead. Shiri's instance-local adapter uses the
correct full-text input layout and keeps incremental audio decoding. It pins
package version and the exact upstream inference source hash; it does not edit
installed MLX files or global model classes.

The guarded loader generated seven diverse clips and two repeats. All ended
naturally below their caps and exactly matched the corresponding content-checked
prototype waveforms. Short first PCM was 54–56 ms; the longer replies were about
70 ms on the M3 Mac. Ryan's short reply lasted 2.72 seconds, with about 2.6 ms of
leading quiet. Aiden examples had 212–256 ms of leading quiet despite quick PCM
emission. Generation cancellation discarded the tail and its successor matched
the clean reference. These are model/software measurements, not heard latency
or subjective quality scores. Other texts, voices, languages and physical outputs
still need qualification.

Kokoro remains the default, with natural English sentence delivery, voice
presets and speed control. Soprano is another compact English option. The Mac
has the pinned assets cached and an authenticated offline worker LaunchAgent.
[Local speech documentation](LOCAL_TTS.md) records model revisions, dependency
pins, the official source comparison, measurements and API examples.

## Idle OwnTone repair

The real VM text route exposed a five-second startup timeout after the previous
speech ended. The original pipe input had suspended and waited for a six-second
refill before resuming. A realtime silent bed could not satisfy that deadline;
increasing the deadline would preserve the latency and age its presentation.

The additive `idle1` patch arms the existing output-only mixer for the idle
source. Shiri suppresses its competing authenticated idle pipe bed. Prepared
speech expiry also restores exact idle output cleanup when no speech was mixed.
Music continues to win on genuine queued input and keeps its original clock.
No music buffer, output protocol delay or speech preparation deadline changed.

The strict checker reproduces the old idle failure on the exact `event1`
preimage, then validates the patched source with address/undefined-behavior
sanitizers. It passed 44,433 lifecycle assertions including retained paused
phone ownership, advancing music, cold/warm idle speech, EOF/cancel and finite
expiry. Inverse validation privately removes only the additive patch before
calling the unchanged historical source validators.

An isolated, network-disabled systemd build in the actual Ubuntu guest passed
those checks, compiled the backend and ran the real libav framed-resampler,
transition and paused-speech fixture before installation. Source archive SHA:
`8243fa17632acdbbb8c524da79f0ad23f6ebf5c4e3e0bf0494215c41398054b2`.
Final player source SHA:
`ac30806a6bab2032b2ece07fdfbbe36d5b2ecdc7dd4b1376a341cf5b74be202e`.
Patch SHA:
`19161c472ceb6ab80d88d3e22202ea16c37a7829ed5d44db027cdcd3b73ac29c`.
Native binary SHA:
`0be833a0780a55b5a4ea53fea2aaf4ed2a9bbfa9c885ebf1708d20a12e4cef47`.

## Live installation and boundaries

[The live handoff](LIVE_TEST_HANDOFF.md) records the current wheel, unchanged
Shairport binary, room, service startup and test instructions. The coherent
native/package deployment saved verified rollback artifacts, retired the exact
recorded actors through normal shutdown, verified empty owned process/network
records, then restarted with the same installation identity. Saved room master
15, revision76 and speaker assignment were preserved. No manual ledger, lease
or interface edits were used.

The earlier actual iPhone/Sonos acceptance remains historical and unchanged.
Today's evidence does not establish mixed-protocol acoustic sync, native phone
grouping, distributed microphone clock quality or new physical first-sound
latency. The reported slower first sound after connecting still needs the later
phone trace; it must not be conflated with the proven idle speech refill defect.

## Retained failures

Earlier failed inference configurations, token exhaustion and their complete
WAVs remain preserved beside the repaired results. An initial deployment used
invalid wheel basenames; no new package was installed, and valid packaging then
completed the rollout. The first actual Qwen room request failed at the old
five-second preparation deadline; its successful quiet generation is separate
from that playback failure.

The first idle1 live regression reached successful natural speech, successors
and idle-after-natural/idle-after-cancel cases, then exposed a cancellation
completion race: a Qwen cancel returned while its private decoder was still
cleaning up. Terminal room state alone was insufficient evidence that another
model request could begin. This failure is retained separately from subsequent
corrected readiness checks.

The coordinator now closes the exact room stream promptly and retains its
generation slot until bounded read-only worker status confirms decoder
retirement. An independently owned cleanup task survives Stop during natural
completion and a disconnected caller. There is no generation retry or remote
cancel of an unrelated worker operation. Timeout or control failure produces
cleanup-unconfirmed diagnostics and finite retirement.

The final cancellation regression deliberately delays actual ModelWorker
cleanup acknowledgements behind an independent HTTP producer. It covers
cancelled and natural completion, interrupted/repeated Stop, warm successors,
finite cleanup uncertainty and a failed status client. The 119-test coordinator
and worker suites passed; root independently reviewed final cleanup ownership.

The first full portable suite after the native change reported one stale
fixture expecting the suppressed authenticated idle FIFO. The revised test
checks actual speech-channel PCM/control and absence of a competing program
FIFO, with existing legacy no-output FIFO behavior still covered. These
unsuccessful results are preserved rather than rewritten as passing evidence.

A concurrent live run rejected a stream after one frame for missing the sender
deadline; cleanup confirmed and the room remained ready. The same package
subsequently passed all 14 live cases without the host regression suite running.
The original receipt cannot distinguish first-dispatch latency from a genuine
later producer stall, so concurrent load is not asserted as its proven cause.

Independent investigation reproduced a separate initial pacing defect using
the actual coordinator and PcmSpeech decoder: delaying only first dispatch by
180 ms made the sender reject the next frame even though the receiver accepted
it within its unchanged bounds. Pacing now starts at the room's actual first
admitted-frame timestamp, verified against the local send/reply interval and
clock marker. It never starts at acknowledgement time or a Mac generation
timestamp. Later deadline, leading-audio and stall limits remain unchanged;
new dispatch/RPC metrics distinguish initial delivery from later timing.

## Final software validation

| Check | Result and boundary |
| --- | --- |
| Complete portable Python regression | 4,197 passed, 85 explicit/platform skips |
| Browser and client checks | 58 passed, including Chromium controls and speech flows |
| Coordinator, native worker and direct PCM regression | 155 passed, including delayed first dispatch/reply and cancellation ownership |
| Independent pacing review | 105 relevant PCM/coordinator tests passed; no findings |
| Actual Linux native build | Old failure reproduced, sanitized idle lifecycle and linked libav framed-resampler checks passed before installation |
| Live room route | 14 cases passed on final package: natural speech, cancellation, immediate successors, model switch, quiet preview and 16-second idle reuse with Kokoro/Qwen |

The final 60-file wheel SHA is
`064fd36922f49f5ae735013206ea0b88856ab13a8edbcde1de2b3522bf29f3f9`.
Mac and VM installations verified the same full package manifest and the
checkout's source hashes. Final coordinator SHA is
`e0cd7097f33b127d5f02c8893c7a482b3b7cccf150c4415fbd3a555ea75eec0d`.
Later API package updates kept the runtime PID, native binaries, service units,
configuration and room actors unchanged.

Warm Qwen short replies reached actual room PCM admission in 64–108 ms in the
retained final cases, with backend preparation around 19–23 ms. Idle transport
reactivation raised admission to 645–673 ms. These remain software observations;
physical first sound and mixed-room sync still need microphones and phone tests.

Push-triggered [Linux Checks](https://github.com/michelsedgh/Shiri/actions/workflows/checks.yml)
run the full GStreamer/PCM regression, native parser checks and browser suite.
The exact code commit and CI result are retained with the release evidence;
portable skips are not rewritten as Linux or hardware passes.

## Evidence preservation

Raw measurements, model/inference comparisons, complete WAVs, failed trials,
native source/build logs, deployment receipts, final packages and live checks
are preserved outside Git at
`/Users/homr/Documents/Shiri-Validation/release-2026-10-03`.
Each additive batch has an SHA-256/byte-count index and read-only file modes.
Credentials, private environment copies, model caches and full guest rollback
backups are excluded. The protected guest keeps those rollback artifacts.

`batch-0001` contains the historical generation/native build and failed live
records: 267 files, 99,952,163 bytes, index SHA
`d57e9f99910953ebba966473c1890f26f035b69ee61e9dae8987d4dd1cdaee2c`.
Independent audit verified every payload, all 441 archived source members,
source/patch/build-log chains, absence of the current worker credential and
retention of unsuccessful outcomes. Later passing validation and CI are
separate additive batches; earlier evidence was not replaced.
