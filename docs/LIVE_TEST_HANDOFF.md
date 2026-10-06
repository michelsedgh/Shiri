# Live test handoff — October 5, 2026

## Phone music alignment

The permanent `phone1` release is installed and running in the Ubuntu VM.
The paired deployment completed at 2026-10-06 01:21 UTC (October 5 local time),
preserving the enabled room, revision 93 and volume 19. The speaker reconnected
and both services passed readiness. The temporary general-offset trial is retired.

Open **http://shiri-speaker-test.local:8080/**. Select **Shiri Test Living Room**
in the iPhone's AirPlay controls for music, or use **Speak** for a text reply.
The room retains its existing Sonos AirPlay 2 / ALAC assignment, NTP timing,
zero speaker offset and 100% balance. Volume and room revisions follow current
user edits; deployment never restores an earlier database snapshot.

Buffered Apple Music previously acquired an additional 600 ms relay delay in
the standard AirPlay room. The receiver now hands that music onward earlier,
using `A = min(common H, 600 ms)`. All receivers in a group use the same A.
The 500 ms speaker buffer remains intact. A larger correction plan retains
its residual `H − A`; realtime AirPlay, AirPlay 1 and TTS retain their existing
paths. See [the timing contract](TIMING_RESEARCH.md) for the exact arithmetic.

The general Shairport latency offset stays zero. Its upstream realtime guard
can reset that process-global setting, losing alignment for later music.
The new immutable `shiri.buffered_audio_advance_ms` setting applies only to
buffered AirPlay 2 through Shiri and survives those source changes. There is
no new public tuning knob, scheduler, watchdog or automatic room restart.

## Evidence and limits

The user compared iPhone built-in playback with Sonos, then reported that the
live trial sounded better without noticeable startup issues. A 45-second
passive observation captured buffered AirPlay 2 music in 36 samples: zero
FIFO drops, rejected writes, worker/source errors or relevant journal warnings.
The permanent receiver then passed another 45-second observation, with music
active in 38 samples and the same zero-error counters. Its private advance was
600 ms and its general offset zero. The user confirmed it still sounded good
with a clean start. Final room readiness and the warm Qwen worker were healthy;
the user had advanced the room to revision 97 while volume remained 19.
This is a listening result and software-delivery evidence, not an independently
measured screen-to-speaker offset. Direct Sonos had subjectively felt similar
to Shiri before the trial, so the original perceived sub-200 ms discrepancy
cannot be attributed precisely to the relay from that comparison alone.

The final code passed 4,347 portable tests with 77 platform-specific skips on
the Mac. Linux GCC and Mac Clang sanitizer checks exercise the exact native
clock functions: 10,818 mapping cases per build mode, both sample rates, RTP
wraparound, integer configuration bounds and 200 realtime-to-buffered session
transitions. Existing receiver event, startup, clock and PCM checks remain in
the build. The full Linux receiver built successfully; its existing publisher (27 cases),
startup (14 cases), clock (50 cases) and encrypted event checks also passed.
The exact-source fixture additionally checks compilation without the private
mapping enabled; it is not a complete second backend build.

Startup traces showed positive first-packet lead and the user heard no clipped
opening. Upstream may discard expired samples before Shiri's counters; current
telemetry does not prove preservation of every original song-prefix sample.
Physical grouping, different speakers and exact acoustic alignment still need
measurement.

## Artifacts and rollback

Python wheel SHA-256:
`29105b1b3dc095e9a62bd6789baffaef1995702c86116a7863eca820689e1a5b`.
It contains 61 package files; only the broker, backend configuration and latency
policy differ from the preceding stable wheel.

Shairport remains pinned to upstream
`7bad231c18368dbd26f298577f6210e36e4b0797` and adds `-phone1` to the existing
`timed3-startup1-volume2` marker. Its new patch SHA-256 is
`0bbd200c49f57df9890c92521e664de42ab21515b623106d460a6520c42d763e`.
Installed receiver SHA-256:
`fb4e5931a2b96e6ca7126df5ca2f5358656a32f25341157dc96b6fcfc0165e90`.
Installed backend manifest SHA-256:
`6adde9fec5dc126b63c822da8cde2c2a6651a8ba34f5a2a7a8d8c61eb49e9441`.
The runtime requires the matching receiver; deploy Python and native artifacts
together. OwnTone and the Mac TTS worker are unchanged by this release.

The private paired checkpoint is
`/var/lib/shiri-phone1-checkpoint-20261005` in the VM. Its reviewed helper retires
the exact owned processes/networks before replacement, verifies all package and
native bytes, and waits for connected room readiness. Recovery restores stable
wheel `a2d46a…` plus the exact preceding Shairport binary and manifest, preserving
current room intent. The successful experimental wheel is not the rollback
release. Host evidence is under
`/Users/homr/Documents/Shiri-Live-Test/management/phone-alignment-20261005`.

## Startup and speech

The Mac model worker and its loopback SSH link start at desktop login. After a
Mac reboot, sign in and start **Shiri Speaker Test** in UTM; both Linux services
start with the VM. Qwen remains loaded and warm. The VM reaches the authenticated
worker at `127.0.0.1:8091` through `org.shiri.tts-link`; do not restore the faulty
Mac/VM bridged TTS route.

Enabled rooms keep speaker connections ready. Each room supports one active
reply and two queued texts; interruption requires an explicit request. The
bounded speech-delivery headroom and selected model's startup warmup remain in
place. See [local TTS](LOCAL_TTS.md) and the
[October 4 deployment checkpoint](CHECKPOINT_REFACTOR_2026-10-04.md).
Earlier builds and listening observations remain in their dated checkpoints
and Git history rather than being repeated as current instructions here.
