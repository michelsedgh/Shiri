# OwnTone 29.3 room-local software volume

## Buffered phone music timing

`shairport-5.5.2-buffered-timing.patch` follows the bounded-events receiver
layer on the same pinned Shairport commit. It adds `-phone1` and the private
integer `shiri.buffered_audio_advance_ms` setting (0–600). The value is read
once and advances only the Shiri backend's buffered AirPlay 2 anchor mapping.
It leaves the source PTP clock, realtime/AP1 negotiation, speech, and output
buffers unchanged. The runtime uses the shared frozen `min(H, 600 ms)` value;
the standard AirPlay H600 plan therefore cancels its extra relay delay.

Using the upstream general latency offset instead would let a short realtime
session reset the correction for later music in that process. The private
field is independent of that fallback. The exact composed-source checker
`tests/native/check_shairport_buffered_timing.py` covers rate conversion, RTP
wrap, bounded setting parsing, source changes and timestamp mapping with the
private backend disabled under sanitizers. Build and deploy the matching Python/native pair;
the runtime rejects receivers without `phone1`. See
[timing and physical limits](../../docs/TIMING_RESEARCH.md).

## Software volume foundation

`owntone-29.3-software-volume.patch` applies only to reviewed upstream commit
`d6fb3edf5831de38134ebd92fcf09a730ddd37aa`. Its SHA256 is
`f9250ebec36873ea39fff78ca3bbc5c424b023ead267868331f985ad0da1fe15`.
The builder verifies that commit and digest, checks patch applicability,
applies it, runs the sanitizer checks, and records the patch filename and
digest in `share/shiri/backends.json`. The compiled version identifies itself
as `29.3-shiri-swvol1`; Shiri refuses an unpatched backend.

Pinned upstream always opens a hardware mixer before activating a local ALSA
device. A validated Loopback PCM has no volume element, so selection failed
and OwnTone deselected the intended output. The patch adds explicit
`software_volume = true` to the existing `audio` and per-device `alsa`
configuration sections. Its default is false, preserving the hardware mixer
path for existing configurations. Shiri enables it for its configured local
endpoint; it does not create global ALSA controls or fall back to a different
PCM/Master device.

Software mode skips hardware mixer open/set/close. It clamps the session's
volume to 0..100 and uses cubic gain `(percent / 100)^3`: 0 is mute, 50 is
one-eighth amplitude, and 100 is exact unity. Signed S16_LE, packed S24_3LE
and S32_LE use bounded int64 integer arithmetic, truncating toward zero.
Private reusable playback-session buffers are scaled only immediately before
each ALSA write. Shared output buffers and prebuffered samples remain raw;
queued and draining old-quality sessions use the current device-session gain.
Already submitted kernel-buffered frames retain their previous gain, so a
volume change is not an instantaneous replacement of that tail.

OwnTone retains its frame counts, timestamps, synchronization, latency
compensation, resampling and queue decisions. This first layer preserves the
upstream partial-write handling; the final ALSA queue layer below corrects it.
The adjacent unsigned start-offset guard is corrected:
an invalid negative offset retains the original delay and logs the error;
valid zero/positive/negative offsets retain their timing. Widening before
negation also handles `INT_MIN` without signed overflow. PCM writes and volume updates run on the pinned
player event loop; the patch introduces no separate scheduler or thread.
See the pinned [ALSA backend](https://github.com/owntone/owntone-server/blob/d6fb3edf5831de38134ebd92fcf09a730ddd37aa/src/outputs/alsa.c),
[player event loop](https://github.com/owntone/owntone-server/blob/d6fb3edf5831de38134ebd92fcf09a730ddd37aa/src/player.c#L3997-L4003)
and [output dispatcher](https://github.com/owntone/owntone-server/blob/d6fb3edf5831de38134ebd92fcf09a730ddd37aa/src/outputs.c#L1251-L1269).

Run portable tests, with no ALSA device or Linux root requirement:

```sh
python3 tests/native/check_owntone_patch.py
```

Add `--source /path/to/patched/pinned/owntone` to compile the actual control
functions against mocks and verify software mode never invokes the hardware
mixer while the default mode still does. The scaler checks cover 6,827,608
format/gain cases, immutable input and guard bytes, unaligned packed samples,
signed extrema, mute/unity, clamps and invalid arguments under ASan/UBSan.
The actual final-write seam tests current gain on queued/draining copies,
unchanged frame counts, allocation failures and invalid/overflowing lengths.
Control mocks also test start offsets -2000, -2001, `INT_MIN`, 0, 2000 and
`INT_MAX` against a 2000 ms buffer, including the zero-start boundary.
`--no-sanitizers` is an explicit fallback for an unsupported compiler.

Sanitizer checks and actual-function control mocks passed on the Mac and
Ubuntu. The final patched binary compiled and installed into the private
Ubuntu candidate prefix on September 30, 2026.

The real rootless API → broker → audio worker → OwnTone → final Loopback PCM
check passed in 33.4 seconds, from 10:07:05 to 10:07:39 UTC that day. Changing
volume 100 → 50 → 100 measured amplitude ratios 0.12493408 and 1.000000,
matching the cubic half-volume gain. Exact-zone TTS ducked music to 0.1999987;
silence restored 1.000000 and close restored 0.9999993. Reported queue and FIFO
drops were zero. The retained report is
`/tmp/shiri-v2-airplay-api-tts-result.json`. This used a synthetic AirPlay source
and a configured local Loopback output; it establishes the exercised API and
final-PCM route, rather than physical-speaker timing, stock-phone/group
interoperability, Cast input or production completion.

## ALSA queue and continuous playback

`owntone-29.3-alsa-partial-write.patch` follows the framed-output layer. Its
SHA256 is `07e8c811ac27220fc96993a5a887bddb758d7a21f5f0a2a5b8684a9e006f1e43`.
This layer ends in `-resample1-framed1-alsa1`; the late speech layer adds
`-speech1`; the private cold-start proposal below adds `-ready1` and requires that exact version. The builder verifies the digest, applies the
layer, runs the actual composed C seams before installation and records its
digest in `share/shiri/backends.json`.

The upstream direct and queued paths lose 384 frames when a device accepts
576 of a submitted 960. The repair peeks at the existing raw playback queue
and consumes only positively acknowledged frames. Short writes, zero,
EAGAIN and EINTR retain unwritten samples in order. Submission uses the current
gain; queued raw samples stay unchanged. Admission counts include both accepted
device frames and retained queue frames. Insufficient queue capacity returns
an explicit failure. Exact source/session cleanup frees pending tails before
a successor can play them.

A separate actual-source reproduction found startup buffering could restart
when the uint32 frame counter wraps, approximately every 24.85 hours at 48 kHz.
Startup completion now belongs to the playback session, independently of the
wrapping counter. Initial arithmetic widens before addition; existing modular
sync calculations remain intact. A fresh playback session starts with fresh
startup state.

```sh
python3 tests/native/check_alsa_partial_write.py
python3 tests/native/check_alsa_partial_write.py --source /path/to/composed/owntone \
  --preimage-source /path/to/preceding/framed/owntone
```

Actual-source Clang and GCC sanitizer checks passed for raw 16/24/32-bit samples,
gain changes, direct/queued/draining tails, retry results, wrapped queues,
capacity refusal, counter rollover, fresh startup and source cleanup. The
helper attempts at most eight transfers per callback; this bounds work count,
while upstream's blocking driver calls retain their existing timing behavior.
No application scheduler or output clock was added. The fresh Ubuntu candidate35
full build passed; combined low-buffer grouped playback remains required.

## Shairport native receiver startup

The timed receiver patch targets Shairport commit
`7bad231c18368dbd26f298577f6210e36e4b0797`; its current SHA256 is
`6f04b42c31b1e6612586349955b135356d7d844de1b1cf36701c3fd8eae74d36`.
The native backend accepts the standard `main` no-option initialization with
`argc=-1` or `0`. Rejecting `-1` previously terminated the real receiver during
initialization despite a briefly live service. The runtime now requires the
exact receiver MainPID to own its admitted IPv4 port-7000 LISTEN socket before
output restoration can mark a room running. Unit, PID birth, held namespace,
cgroup and socket-inode checks bound that readiness decision. A short protected
same-UID observer reads kernel proc evidence without adding `CAP_SYS_PTRACE`.
Dedicated real-kernel readiness checks passed seven vectors; these checks do
not establish stock-phone discovery or native group interoperability.

The `-shiri-timed2` backend checks the actual RAW-clock syscall and retries a
complete MONOTONIC/RAW/MONOTONIC sample at most four times. Accepted brackets
remain at most 1 ms and must complete before the cumulative 5 ms admission
deadline. OS preemption can exceed that duration; the sample then fails rather
than claiming a bounded wall-clock return time. Native presentation time, RTP,
source identity and PCM stay unchanged. Exhaustion closes only the exact route
without a timed sequence/frame increment or invalid transmission. The actual
callback sanitizer checks retain the exact previous backend as a checksummed
preimage and reproduce its invalid 1.347960 ms transmission before proving the
repair, deadline boundaries, syscall failures and existing gap/flush semantics.

## Offset and buffer arithmetic layer

`owntone-29.3-offset-arithmetic.patch` follows the software-volume, timed-input,
source-transition, PCM-identity and transport-control layers. Its feature suffix
is `-offset1`; the resulting version is
`29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1`.
The SHA256 is
`c5ca167859b70449a72d9b0e011af759aa36a5e56f360d7676873ed0f1364cda`.
Builder integration and a real combined backend build must pass before this
layer replaces a previously verified candidate binary.

Cast, FIFO and Pulse used an unsigned comparison that could never detect a
negative resulting delay. An offset of -2000 ms against a 1000 ms buffer
wrapped FIFO/Pulse to an enormous unsigned delay; Cast's additional 100 ms
still wrapped. The layer applies the existing ALSA boundary correction to
these outputs, uses signed comparisons for legacy configuration offsets, and
rejects negative buffer configuration before unsigned conversion. Invalid
negative offsets retain the configured base delay and log the failure.

AirPlay/RAOP sample offsets now multiply and remain in signed 64-bit values;
RTP positions retain their existing modulo-2^32 arithmetic. Buffer conversions
check multiplication and the actual destination width before assignment:
32-bit RTP sample counts, signed ALSA frame counts and `size_t` bytes, and
Pulse lengths including its doubled maximum. Valid timing, integer rounding,
PCM data and output scheduling remain unchanged.

RAOP request-header admission now rejects exhausted or invalid signed CSeq
counters before emitting headers or incrementing. It never wraps or reuses a
number within the same session; existing callers free the unsubmitted request
and follow their normal failure path. `INT_MAX-1` is the last admitted number.

Receiver-supplied RAOP discovery fields are bounded before encoder/resampler
allocation: rates 1..384000 Hz, bit depths 16/24/32, and 1..8 channels. Missing
legacy fields keep their defaults; malformed present values reject that
advertisement. These are candidate admission limits, not a claim that every
combination works on a receiver. Pinned OwnTone forwards the rate to FFmpeg;
[FFmpeg's resampler options](https://github.com/FFmpeg/FFmpeg/blob/n4.4.2/libswresample/options.c)
permit rates up to `INT_MAX`, and its
[initialization](https://github.com/FFmpeg/FFmpeg/blob/n4.4.2/libswresample/swresample.c)
checks positivity rather than providing a practical resource limit.

```sh
python3 tests/native/check_offset_arithmetic.py
python3 tests/native/check_offset_arithmetic.py --source /path/to/composed/owntone
```

The actual-code ASan/UBSan checks cover 556,952 offset, modular RTP,
configuration, discovery and conversion cases, plus backend-width boundaries
and 21 complete RAOP request-header admission cases.
The preimage tests reproduce delay/configuration wraps, unbounded discovery,
RTP/Pulse truncation, and signed-overflow sanitizer failures in RAOP offset
multiplication, ALSA prebuffer byte multiplication and CSeq increment at
`INT_MAX`. They use no audio device,
network or Linux root authority. The repaired seams compile with `-Werror`;
only the intentionally faulty preimage suppresses its unsigned-comparison
warning. Physical playback and mixed-speaker synchronization remain separate
integration checks.

## BlueALSA synchronous restricted controller

`bluealsa-5.0.0-drop-sync.patch` targets exact upstream
`1a84465dd860d1be9dcf62339c6273e9e0632dd2`. Its current candidate SHA256 is
`964c756acfc82d8ab6877e9be3741fdf754b835fff1b7024b7347c1be46705a2`.
The binary marker is `5.0.0-shiri-dropsync1`. This separately staged candidate
has passed its Linux build, linked codec check and actual private
D-Bus/controller/SBC test. Paired Bluetooth acceptance remains pending;
[private test evidence](../../tests/linux/README_bluealsa_private.md) records
the exact binary, isolation and cleanup scope.

Stock `Drop` sends an encoder-thread signal and replies `OK` immediately. The
new PCM1 `SynchronousDrop` property is true only for the reviewed A2DP-source
SBC playback PCM. `DropSync` carries an exact monotonic request token through a
separate nonblocking pipe. It empties the selected PCM FIFO, rewinds its raw
encoder buffer and checks SBC reinitialization before completing that token.
Previously queued legacy DROP/CLOSE/OPEN controls are processed before the
synchronous acknowledgement. Tokens from timed-out, canceled, released or
terminated requests cannot acknowledge a successor. Monotonic lock/wait/flush
deadlines are 150 ms, leaving delivery time inside Shiri's 200 ms client limit;
a late completion or waiter wake returns failure.

The typed `RestrictedController=true` capability and zero-input
`PCM1.OpenRestricted` method return the usual `(hh)` PCM/controller descriptors.
That controller admits only the exact `DropSync` command. Legacy Drain, Drop,
Pause, Resume, empty, prefix and unknown commands fail before entering the
legacy handlers. Unsupported codecs fail restricted opening. Upstream `Open`
and its controls retain their existing behavior. Shiri requires both typed
capabilities and uses `OpenRestricted`; neither stock Drop nor a fixed wait is
an acknowledgement fallback.

Primary sources are the pinned [controller and PCM opening](https://github.com/arkq/bluez-alsa/blob/1a84465dd860d1be9dcf62339c6273e9e0632dd2/src/bluealsa-dbus.c),
[transport requests](https://github.com/arkq/bluez-alsa/blob/1a84465dd860d1be9dcf62339c6273e9e0632dd2/src/ba-transport-pcm.c),
[PCM polling](https://github.com/arkq/bluez-alsa/blob/1a84465dd860d1be9dcf62339c6273e9e0632dd2/src/io.c)
and [SBC encoder](https://github.com/arkq/bluez-alsa/blob/1a84465dd860d1be9dcf62339c6273e9e0632dd2/src/a2dp-sbc.c).
The barrier covers BlueALSA userspace buffers; packets already submitted to the
Bluetooth kernel/remote device can retain an audible tail.

```sh
python3 tests/native/check_bluealsa_drop_sync.py
sudo SHIRI_INSTALL_PREFIX=/opt/shiri-bluealsa-candidate bash install/build_bluealsa.sh
```

The portable checker compiles actual request, polling, release, controller,
opening-wrapper and codec-reset seams under ASan/UBSan. Forty-one cases cover
stale tokens and legacy controls, cancellation, held mutexes, continuous writers,
codec reset failure and restricted-controller attacks. Removed stock functions
reproduce the early `OK`. The Linux builder additionally requires the linked
SBC test, which compares post-reset encoder output with a fresh encoder and
proves retained history differs before reset. The linked test has not run on
this Mac because `pkg-config sbc` is unavailable.

The private Ubuntu 22.04 ARM build passed on September 30, 2026, including
GCC ASan/UBSan, all 41 actual-source cases and the linked SBC reset proof.
The installed `5.0.0-shiri-dropsync1` binary SHA256 is
`ffa7d0d7ccf06e04699a3f98e149433a4eec2f51b014cee80f2ef12f69d66c32`.
Its root-owned provenance and trusted-prefix checks passed. Existing service
identities, ALSA packages/files and PCM open states were preserved. This build
includes the compiled isolated-storage override. It did not start the daemon,
exercise a live controller or use paired hardware.

The standalone builder validates the root-owned prefix, verifies source and
patch digests, runs the checks, and writes source/binary provenance to
`share/shiri/bluealsa.json`. It enables the compiled `STATE_DIRECTORY` override
needed for isolated daemon storage; service unit and D-Bus policy files remain
inside the private prefix, without activation. Only missing build packages are requested;
existing development libraries are checked by configure rather than upgraded.
Host daemon activation, account/policy
provisioning, paired hardware and exact cleanup tests remain separate gates.

## OwnTone late speech overlay

`owntone-29.3-late-speech.patch` applies after the partial-write layer and
adds the `-speech1` marker. It mixes private 48 kHz, S16LE stereo program PCM
immediately before OwnTone's existing output conversion and dispatch. Speech
uses a separate authenticated Unix datagram socket; it does not wait behind
the native input FIFO. Program timestamps, frame counts, player state and
transport queues retain their existing scheduling.

The socket is enabled only with the explicit framed profile and complete
room, launch and audio UID configuration. The fixed 80-byte header fences
those identities, a strictly increasing sequence and packet age. Incoming
speech is mono 48 kHz S16LE, at most 20 ms per datagram. The player reads at
most 25 datagrams per tick and retains at most 250 ms of speech. Malformed,
stale, wrong-UID and overflowing packets have no program effect. Ancillary
descriptors are closed on rejection. The endpoint must be newly created in
the prepared output-owned, audio-group directory; disposal checks its exact
owned inode and preserves replacements.

Audible queued samples retain their own ducking gain through EOF and later
controls. Quiet samples do not refresh or clear an earlier audible lease.
Normal EOF ends the active lease while allowing admitted speech to drain;
queued speech still expires after 250 ms. Gain changes use the existing
40 ms duck and 250 ms restore law, and addition clips to signed 16-bit PCM.
The original program path remains unchanged when the profile is disabled.

```sh
python3 tests/native/check_late_speech.py --source /path/to/composed/owntone
sudo python3 tests/native/check_late_speech.py --source /path/to/composed/owntone --require-credentials
```

The portable actual-source ASan/UBSan proof covers 34,715 parser, queue,
expiry, gain, EOF and PCM checks. The separate Linux root fixture uses only
private Unix sockets and disposable capability-free UIDs, including failed
setup cleanup, real credentials, truncated ancillary descriptors and
replacement-safe disposal. The actual composed module passed those 99 parent
checks and all 34,715 sanitizer checks on fresh Ubuntu ARM64 October 1, 2026.
The repaired candidate35 also passed the complete unmodified backend/application
installer and full Linux Python suite (2304 passed, 4 skipped). Earlier timer
and partial-read scaffold failures remain preserved in the rebuild record.
Cold startup and measured speech latency remain separate integration gates.
Output lead and packets already encoded
or submitted to a device still limit how soon a new overlay can be heard.

## Private cold speech readiness proposal

`owntone-29.3-cold-speech-ready.patch` follows the unchanged late-speech layer.
Its SHA256 is `3aabf70608d609ea2258c0c6e3c63e3a308c4e049d1edd9498308eddf80cb760`;
the exact ready1 layer marker is `29.3-shiri-swvol1-timed1-source1-guard1-transport1-offset1-buffer1-resample1-framed1-alsa1-speech1-ready1`.
The private builder checks applicability and this digest, runs the actual C
readiness seam, and records its source digest in `share/shiri/backends.json`.
Private runtime preflight rejects the older binary. This proposal has not been
installed or exercised in a VM.

An authenticated `/api/player/shiri-speech-ready` request prearms only an exact
idle source's selected outputs. It returns immediately and uses its own five
second deadline, so a missing transport START callback cannot retain the player
command lane. Readiness requires the same source operation, room, launch,
speech nonce, selected output identities/offsets and connected sessions, plus
an actual recent anchored player output dispatch. Advancing native music uses
pure observation: speech does not pause, seek, flush, reconnect or replace it.
The dispatch clock is the latest fresh tick first acknowledged by this request;
it is not the first-ever transport tick or physical playback time.

Python overlaps this setup with RTC negotiation, admits unexpected decoded PCM
into one bounded 200 ms prefix, and checks its original receive age against the
unchanged 250 ms media bound before releasing it. Failed, canceled or replaced
offers discard their own unsent prefix. Current API session/request identity,
the private nonce and the raw authenticated readiness ACK remain in worker
health. Original receive, setup, dispatch and release clocks stay separate.
Idle prearm has the existing finite ten second delayed-stop cleanup; a live
silent speech peer maintains only its own finite zero bed. There is no permanent
warm bed or new playback scheduler.

`tests/native/check_cold_speech_ready.py` passes 2,456 actual player/callback
checks and eight strict JSON cases under ASan/UBSan. The tests use exact
maintained callback functions and controlled backend completions; they do not
prove kernel credentials or a real network speaker. Sensitive removed guards
fail those tests. The existing 34,715 late-overlay checks still pass. Full Linux
compilation, actual cold/warm finite utterance completeness, software latency
qualification, and the earlier matrix42 warm-player failure remain integration
gates. Five seconds is a setup deadline, not an accepted speech latency.

### Fresh first native anchor after input mutex wait

The additive `owntone-29.3-native-anchor.patch` applies after the unchanged
cold-speech readiness layer and appends `-anchor1` to that exact version. The
runtime requires this marker; older ready1 builds must be rebuilt. Before this
repair, `input_peek_sync()` could wait on the input mutex until after the native
anchor, while the timer admitted it using the clock read before the wait.

The existing player timer now reads `CLOCK_MONOTONIC` again after that peek,
including a missing marker, and rejects failed clocks, elapsed five-second
admission budgets, and anchors at or before the refreshed clock. Original
presentation timestamps, the anchored timer path, and ordinary file/raw inputs
are unchanged. This closes the demonstrated stale-clock admission; it does not
bound OS scheduling after the refreshed check or prove physical receiver timing.

`tests/native/check_native_anchor.py` reproduces the exact removed function's
10ms stale-clock acceptance before exercising 25 repaired cases on both timer
and timerfd. The actual composed five player functions retain disabled-overlay
poll/mix/ready ordering. Run `python3 tests/native/check_native_anchor.py --source
/path/to/composed/owntone --compiler /usr/bin/cc` for the installed build's source.
The builder verifies the additive patch SHA and records it in `backends.json`.


The additive speech-owner layer follows the unchanged fourteen-layer jitter1
backend and appends `-owner1` to its exact version. The builder verifies
`owntone-29.3-speech-owner.patch`, executes prior-layer checks before applying
it, then runs the owner media/player/JSON and Linux credential checks before
building. The runtime requires that exact coherent marker; earlier binaries
cannot admit the v2/96byte producer. BEGIN and exact observed retirement reuse
the authenticated nine-field readiness request and existing speech_id. The
layer preserves original250ms packet expiry,20ms reserve, natural EOF tail,
source/output transport files and music presentation/buffering policy. See
[the voice owner contract](../../docs/SPEECH_JITTER.md). Portable sanitizer proofs
do not establish a built Linux backend or physical output acceptance.


`owntone-29.3-event-ack.patch` follows the exact paused-speech layer and adds
`-event1`. It repairs the dictionary-valued initial `updateInfo` exchange,
strictly frames retained/coalesced RTSP and complete authenticated cipher
records, and echoes the receiver's full uint64 revision without playback
mutation. It changes only `configure.ac` and `outputs/airplay_events.c`.
The builder pins it independently at manifest argument31 after the original
bed checks. Its strict checker reverses only this exact layer privately and
invokes the unchanged historical bed/transition/owner guards, then compiles
the actual event source with libplist/libevent and the unchanged pairing
library with libgcrypt/libsodium under sanitizers. This event ACK does not
add OwnTone device-volume application or qualify iPhone UI/acoustics.

## Idle speech after a suspended input

`owntone-29.3-idle-speech.patch` follows the unchanged event1 layer and adds
`-idle1`. Its SHA256 is
`19161c472ceb6ab80d88d3e22202ea16c37a7829ed5d44db027cdcd3b73ac29c`.
The builder records the additive layer in `share/shiri/backends.json`. This
layer's source marker ends in `-transition1-bed1-event1-idle1`; the combined
runtime contract appears below.

The old idle speech path generated timed silence in the program FIFO. After an
utterance ended, OwnTone could suspend that exhausted input and wait for its
buffer-full callback before resuming the same item. The framed input's existing
capacity threshold is six seconds of 48 kHz S16 stereo PCM, while speech
preparation has a five-second deadline. Reopening the FIFO did not start the
same paused item through pipe autostart. Refilling it in real time therefore
could not produce a fresh mix before the deadline; its earliest original
presentation marker would also age while waiting. A gap flag permits sequence
discontinuity but does not resume the player or replace that original marker.

Idle preparation now arms OwnTone's existing output-only speech bed, also used
for retained paused phone sources. The backend mixes the private speech queue
on its existing player timer without reading a program FIFO or advancing the
program's presentation and frame counters. Python suppresses synthetic idle
FIFO writes when this authenticated speech output is configured, so those
packets cannot compete with the output-only bed. The legacy mixer without that
output retains its existing behavior. Genuine music still takes priority at
its original anchor through the unchanged source and timing gates.

If setup expires before its first mix, the exact prepared idle source and
connected output-session snapshot restore the existing finite delayed-stop
cleanup. Natural FINISH retains its admitted tail; CANCEL retires its exact
voice. Neither a stale session nor a paused phone authorizes stopping another
source's outputs. No permanent warm bed, new scheduler, music-buffer reduction,
retiming or increased setup/RPC timeout is introduced.

```sh
python3 tests/native/check_idle_speech.py --source /path/to/idle1/owntone \
  --preimage /path/to/event1/owntone
```

The exact event1 player source reproduces the missing idle output clock. The
candidate passes 44,433 ASan/UBSan checks using actual composed C bodies and
controlled output callbacks: paused and never-produced idle sources, cold/warm
FINISH and CANCEL, finite expiry cleanup, selected-session and source fences,
and unchanged original music anchors/counters. The checker privately reverses
only idle1 and invokes the unchanged event1 validator, which in turn retains
the exact historical bed, transition, owner, media and authentication guards.
Seventy-two related Python tests pass, including suppression of idle program
audio and preservation of the legacy mixer path.

These results qualify the exercised software seams. Installation of the new
binary and live speaker acceptance remain separate gates; they do not establish
acoustic onset latency or microphone synchronization.

## Natural speech EOF and finite packet completion

`owntone-29.3-speech-drain.patch` follows the unchanged idle1 layer and adds
`-drain1`. Its SHA256 is
`2e240eecaad804b805d886b961116e142f0dff94d6c437f134b96f80b8408563`.
The final player source SHA256 is
`9d11e31ca558861951a6f5285aa4b933b58cecc6aeb59ea806420aa3258eef6c`.

PCM datagrams and the HTTP FINISH command travel over independent transports.
The original FINISH could close admission between player ticks while its last
successfully submitted datagram still waited in the kernel. FINISH now polls
the existing bounded receive burst before sealing natural EOF. A foreign
request fails the exact voice-owner check before polling; CANCEL still closes
pending media immediately.

After admitted speech drains, the exact output-only bed can emit at most 480
additional zero frames (10 ms at 48 kHz). This releases resampler history and the
incomplete 352-frame AirPlay ALAC packet. A short timer catch-up interval retains
the unused part of that finite budget for the next past interval. Source and
selected-output-session checks remain exact; genuine queued music wins its
original anchor and supplies subsequent audio normally. CANCEL permits no
padding. There is no extra startup prebuffer or permanent silence stream.

```sh
python3 tests/native/check_speech_drain.py --source /path/to/drain1/owntone \
  --preimage /path/to/idle1/owntone --capture /tmp/shiri-bed-pcm.bin
python3 tests/native/check_speech_packetizer.py --source /path/to/configured/owntone \
  --capture /tmp/shiri-bed-pcm.bin
```

The actual player C fixture covers the pending-datagram EOF race, every prefix
and tail sample of distinct 4.6-second cold/warm utterances, a 1 ms terminal
interval, replacement output sessions/sources, CANCEL and resumed music's
original presentation anchor. Its strict inverse applies the unchanged idle,
event, bed, transition, owner, media and authentication validators. The Linux
packetizer check links the actual complete OwnTone output conversion and
transcode sources, executes the actual AirPlay packetizer, and independently
decodes real FFmpeg ALAC packets. Every decoded stereo sample matches the
encoder input; any remaining incomplete packet contains only zero audio. It
opens no network socket or audio device. These checks establish sample
preservation through packetization, not acoustic output onset.

## Initial AirPlay metadata readiness

`owntone-29.3-startup-metadata.patch` follows drain1 and adds `-startupmeta1`.
Its SHA256 is
`11cd0aec9232b716d2ea3c28a9ac2d321ca24779937063178ad40982d45df525`;
the final AirPlay source SHA256 is
`9b598a3fe18042476af9e6a535f3201c74276eeee86da84c95c7fc51438e27b9`.
This layer's source marker ends in
`-transition1-bed1-event1-idle1-drain1-startupmeta1`; the combined runtime
contract appears below.

An idle speech bed has no queue item's metadata. Upstream OwnTone skipped its
initial metadata request in that state, although some AirPlay receivers need
initial DMAP metadata before playing audio. Music Assistant's pinned
[AirPlay client implementation](https://github.com/music-assistant/airplay-cli/blob/8e79242996b7ef52352ee49d390e6db434bf88a6/src/ap2_client.c)
and [design notes](https://github.com/music-assistant/airplay-cli/blob/8e79242996b7ef52352ee49d390e6db434bf88a6/DESIGN.md)
document this requirement for Sonos and the required `RTP-Info` header.

For Shiri's framed speech-enabled input with no real metadata, START_PLAYBACK
now sends a transient 71-byte DMAP placeholder after stream SETUP. Its
`RTP-Info` uses the exact initialized RTP session position, including uint32
rollover; it does not compute a timestamp from the not-yet-initialized output
clock. A successful metadata response must precede the existing final volume
request and CONNECTED acknowledgment. Rejection, disconnect and stale callback
incarnations fail closed. Metadata failure preserves the already verified
pairing key; other startup failures retain the upstream error policy. The
placeholder never replaces global or queued
music metadata and contains no spoken text. A durable acknowledgment log records
the exact device, callback and sent RTP position for later passive diagnosis.

```sh
python3 tests/native/check_startup_metadata.py --source /path/to/startupmeta1/owntone \
  --preimage /path/to/drain1/owntone
```

Actual sequence/payload/DMAP C tests capture the request body and hold RTSP
replies to prove ACK ordering, timestamp boundaries and existing ownership
guards under ASan/UBSan. The exact preceding source reproduces readiness
without initial DMAP metadata. The silent tests and complete isolated Linux
candidate build pass. They establish the repaired protocol contract; the
reported physical missing speech prefix and receiver clock readiness still
require later speaker acceptance. No live output was used for these tests.

## Framed music after an empty interval

`owntone-29.3-cold-music.patch` follows startupmeta1 and adds `-coldmusic1`.
Its SHA256 is
`58f3346db99fadfa448a1c02c91d1ec95656b04fc0100928ef4aa790cada6c80`.
The final player SHA256 is
`8b845515d43a4334f90ee4779a800b4dba0c1bb9a5fb2c3891f28ba546a4b9fc`;
the final AirPlay SHA256 is
`405243e11ea3f24301f449346b359d81134ae20ff75c5389c491e776f1dcc046`.
Runtime preflight requires the complete marker ending in
`-transition1-bed1-event1-idle1-drain1-startupmeta1-coldmusic1`.

An already admitted framed source could enter upstream file-style underrun
recovery after 1.5 seconds of missing input. That stopped its native timer and
waited for the six-second buffer-capacity callback. Original presentation
markers aged inside that refill buffer; the unchanged future-anchor guard
correctly rejected them when recovery finally restarted. The capacity bound
had become a readiness requirement even though framed input already carried
its own clock.

A genuine empty read now clears only this file-style read debt when the exact
framed source operation is still armed. Its admitted native timer, output
sessions, source position and original timestamps continue. It does not turn
an ordinary continuing packet into a new cold admission: a packet that arrives
after its input-read time can still preserve its original future device-output
deadline. Initial and accepted-FLUSH anchor checks remain unchanged. EOF,
errors, flagged controls, partial reads and ordinary file/raw underruns retain
their existing paths. No buffer size, queue capacity, PCM or clock is changed.

AirPlay's sample-count-based periodic sync also stops advancing during empty
input. A continuing receiver could receive almost one second of new payload
before learning its resumed clock mapping. Each framed master now tracks the
end of the original raw block, using that block's sample rate rather than the
converted rate. A forward or backward presentation discontinuity greater than
one source sample forces the existing sync packet before the next payload;
smaller integer-floor and mapped-clock perturbations retain the normal cadence.
That pending sync survives conversion warmup and resets the periodic sample
counter when emitted. It changes neither the supplied P nor buffered samples.
Retained partial ALAC samples remain included in the existing RTP-position
calculation, so a fresh prefix receives its original P plus the backend buffer.

```sh
python3 tests/native/check_cold_music.py --source /path/to/coldmusic1/owntone \
  --preimage /path/to/startupmeta1/owntone
python3 tests/native/check_speech_packetizer.py --source /path/to/configured/coldmusic1/owntone \
  --preimage /path/to/configured/startupmeta1/owntone --capture /tmp/shiri-bed-pcm.bin
```

The exact input-write/marker/read and player-source/timer/suspend bodies reproduce
the old six-second refill failure. Both POSIX and Linux timerfd branches pass
676,378 ASan/UBSan checks per candidate branch: a partial first packet, a long
empty interval, original-timestamp resumption, 221,184 steady samples from an
8 ms producer on a 10 ms player, valid continuation jitter, timer catch-up,
source-owner fences and EOF/control/error/file behavior. Strict inversion of
only coldmusic1 retains every prior metadata, speech-drain and timing validator.

The codec fixture now uses the actual RTP commit and sync-cadence functions.
With 1,024 input frames followed by a 65-second gap, the exact preceding source
sends resumed payload without a fresh mapping. The candidate synchronizes
before that payload while retaining 221 converted frames (about 5 ms). Real
ALAC decoding is identical with and without the gap, including the fresh prefix
and tail. Fractional durations and ±100 ns mapped perturbations retain ordinary
sync cadence; actual 1 ms forward/backward gaps and a source timeline handoff
each receive a fresh mapping. These silent software checks and the isolated
full Linux build pass. Physical first-play and idle-speech prefix acceptance
remain separate checks; no live speaker output was used here.
The additive `owntone-29.3-output-clock.patch` follows the immutable
`coldmusic1` layer and appends `outputclock1`. It adds a titled
`shiri_airplay_timing "<canonical decimal uint64 output ID>"` section with
`protocol = "ntp"` or `protocol = "ptp"`. Automatic mode omits that section
and preserves OwnTone's native feature detection, name-based legacy settings
and PTP service availability. Explicit selectors use only the parsed stable
device ID, overriding a legacy name-based `ptp_disable` choice without changing
the other legacy device options. Config load rejects noncanonical/overflowing
IDs, missing or unknown protocols and duplicate titles. Explicit PTP refuses
devices lacking the advertised capability or an available local PTP service.

The selected native AirPlay 2 output timing is exposed as `airplay_timing` in
OwnTone's output JSON; it is absent for other output types. This value records
the backend's timing choice and does not establish receiver clock lock or
rendered audio. Applying a different preference requires the ordinary
configuration-controlled room restart. The source fixture executes the actual
config validator, discovery callback, feature parsing, speaker-info copy and
JSON serialization, and checks the existing SETUP dispatch. The Linux fixture
also parses the actual schema with real libconfuse, including duplicate title
rejection. Neither fixture opens a socket or audio device. Strict inversion of
this layer retains every previous source and codec guard.

### Stable output clock preferences (`outputclock1`)

A controlled cold-start comparison on the test SYMFONISK speaker retained the
whole spoken sentence with AirPlay 2 NTP timing, while its automatic PTP path
lost the opening. The PTP capture scheduled speech before the receiver's first
clock exchange. This does not establish a general Sonos rule, receiver clock
lock or acoustic synchronization across speakers.

`owntone-29.3-output-clock.patch` adds the private titled section
`shiri_airplay_timing "canonical-decimal-output-id" { protocol = "ntp" }`.
`ptp` is also supported; Automatic emits no override. Parsing rejects malformed,
overflowing or duplicate identities and missing/invalid protocol values. The
AirPlay 2 backend looks up the stable ID rather than the speaker's mutable or
nonunique advertised name. Explicit PTP requires advertised support and the
native timing service; it refuses unsupported discovery rather than silently
selecting another mode. Separate RAOP discovery retains its existing policy.

The effective `airplay_timing` field is returned only for an AirPlay 2 output
and follows the same backend selection used by session SETUP. It reports the
chosen clock protocol, not servo lock or audible readiness. The Shiri adapter
requires matching readback before and after selecting an explicit mode.
Clock edits follow normal room retirement/restart; a newer requested or applied
snapshot cannot hide a backend still configured with the previous mode.

Schema v4 retains preferences by physical speaker identity through removal and
room reassignment. Old databases migrate with `auto` and unchanged existing
intent. Default `auto` is omitted from old calibration fingerprints; an explicit
clock change invalidates measurements for that configuration.

The patch is additive after the frozen `coldmusic1` layer. Its checker inverts
only this layer before applying all historical source guards. Sanitized fixtures
execute actual configuration validation, mDNS feature/discovery selection,
speaker-info and JSON serialization, and NTP/PTP SETUP dispatch. The Linux
qualification also uses real libconfuse parsing and the existing actual ALAC
encoding/independent decoding fixture. None of those checks opens a speaker or
an audio device; physical prefix acceptance is reported separately.

### Per-producer speech fade envelope (`duck1`)

`owntone-29.3-duck-envelope.patch` is additive after the frozen `outputclock1`
layer. It changes the private late speech mixer, adds a gain-only source ARM
hook and appends `-duck1` to the native marker. Stable NTP/PTP selection, source ownership, music timestamps,
packetization and output transport code remain unchanged.

The authenticated version-2 speech header's reserved word at offset 76 accepts
an optional packed policy: high 16 bits are attack milliseconds and low 16 bits
are release milliseconds. Both zero retain the exact legacy 40/250 ms per-unit
gain slope. Otherwise attack must be 40–2000 ms and release 40–5000 ms. The first
valid PCM or CONTROL packet locks this policy to the admitted speech owner;
every later packet under that identity must match. Invalid policy, malformed
media or a stale owner cannot change sequence, queue, lease or envelope state.

An early authenticated CONTROL can duck currently playing music before speech
PCM is generated. A nonzero policy fades from the current gain to the actual
new target over the full chosen duration, rather than shortening the duration
for a small gain change. Repeated lease refreshes keep that ramp running;
speech PCM never waits for fade completion. Gain applies to each original music
sample and the original music clock remains in charge.

EOF retains queued speech and its own gain; cancel discards that exact owner's
voice. Cancellation, normal completion and lease expiry retain the envelope
for smooth restoration. A successor preserves the current gain and any release
until its first valid packet adopts its own policy. Fades progress in audio
samples: stopped playback does not add silence solely to finish restoration,
and a resumed music mix continues from the retained gain. An exact idle-to-new
music transition resets only terminal, inactive, empty speech gain after its
successful source ARM, so an unrelated first phone playback inherits no idle
speech fade. Ongoing/queued speech and an existing music owner's pause or flush
retain their envelope and current gain.

`tests/native/check_duck_envelope.py` reverses only this layer before executing
the unchanged historical source guards. Sanitized fixtures exercise early
CONTROL without PCM, exact duration endpoints, heartbeat and direction changes,
policy validation/locking, quiet PCM, queued EOF and cancellation/successor
restoration. Actual source admission/ARM fixtures verify the full first music
prefix after terminal idle speech, unchanged ongoing/queued speech gain, paused
owner continuity, and no gain reset on failed, stale or replayed ARM. The legacy
zero-word vector compares all 153,600 PCM bytes against
the actual frozen preimage, and the original admitted-owner fixture runs on the
new module. These checks open no socket or audio device and do not establish
physical listening latency or quality.

### Finite connection leases (`warm1`)

`owntone-29.3-warm-lease.patch` follows the frozen `duck1` layer. The private
`POST /api/player/shiri-warm` accepts exact room/launch identity, a separate
nonzero warm ID and increasing lease generation, action, and an absolute
monotonic deadline. A room's audio worker aggregates external callers into
one native lease. Acquire and deadline adjustment are bounded to five minutes;
initial transport setup has a separate three-second admission deadline.
An expired identity cannot renew before a queued expiry callback has run.

Acquire uses existing selected-output START without admitting a music source,
speech owner, playback clock or PCM. Observe only copies the exact connection
snapshot. Deadline adjustment changes existing exact-session holds and cannot
reconnect. Timer expiry never retries a connection. Release is terminal;
generation tombstones also fence a delayed first acquire when its retirement
arrives first. Source ownership, mixer gain and audio timestamps are untouched.

Natural OwnTone delayed teardown can be extended only for the same selected
device/session until its finite lease expires. A process-monotonic session
serial also fences allocation-address reuse; session add/remove clears old
holds. Immediate stop, discovery
removal and transport failure remain authoritative. Lease release or expiry
cannot stop an admitted music owner, active or queued speech, a preparation
bed, or a replacement session. Idle cleanup retains OwnTone's ordinary
ten-second delayed-stop grace. A cancelled unfinished START replaces only
its own callback before bounded cleanup; it never completes a player command.

AirPlay's existing 25-second `/feedback` timer is armed at connection instead
of waiting for first PCM. This enables a silent connection lease without
periodic RTP audio. The protocol's existing timing exchanges remain in place.
Connection status establishes successful backend setup; it does not prove
speaker power state, clock lock, acoustic readiness or synchronized playback.
The AirPlay 2 setup/feedback path has been reviewed for silent retention.
Other backend types report only OwnTone connection state and still require
their own standby/traffic qualification.

`tests/native/check_warm_lease.py` strictly inverts this additive layer before
the unchanged duck/clock/historical guards. Sanitized actual-source fixtures
exercise initial, inline and failed START, passive observation, deadlines,
terminal identity ordering, exact session retention, lost/replaced callbacks,
media-preserving release and direct stop. Strict actual JSON parsing rejects
wrong types, extra fields, duplicate keys, embedded NULs and unknown actions.
No fixture opens a socket or audio device; speaker standby and retained-session
playback require separate physical qualification.
