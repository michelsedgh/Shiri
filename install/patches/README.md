# OwnTone 29.3 room-local software volume

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
compensation, resampling and queue decisions. Its existing partial-write/drop
behavior is unchanged. The adjacent unsigned start-offset guard is corrected:
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
