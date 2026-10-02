# One room master and AP2 sender feedback

The saved room master is one integer from 0 through 100. OwnTone applies that
master with the separately saved speaker balances. A speech duck or a speaker
balance edit never changes the room master or creates an iPhone volume command.
An already owed web master notification survives a same-master balance edit.
An accepted phone move commits that same master and advances the worker's room
revision. A web edit first receives OwnTone's exact gain acknowledgement, then
queues optional sender feedback. Feedback cannot pause/reselect outputs or
revoke a working music source if the phone does not support it.

## Receiver control boundary

`shiri.volume_socket` points to `input/volume.sock`, beside `music.sock`. The
worker owns the Unix SEQPACKET listener; mode 0660 and the existing receiver
input directory group permit only the receiver to connect. Both endpoints
check Linux SO_PEERCRED against their configured distinct daemon uid. The
worker replaces and joins an older feedback binding; controller shutdown
cancels and joins its readers before closing descriptors. This socket is
independent of PCM, BEGIN/FLUSH/GRANT and speaker scheduling.

The fixed 80-byte SHV1 envelope carries worker incarnation, native session,
epoch, flush generation, saved room revision and integer master. Idle commands
have zero session/epoch/generation and update only the locked receiver default
used for the next connection. Active notifications target the session present
when that web edit was queued. They never retarget a successor phone; a new
edit can deliberately target the current sender. Every exchange and reply is
fenced again against the current source and revision. The worker coalesces
edits and ignores an obsolete acknowledgement. A newly announced room revision
is not sent with the preceding unacknowledged master.

An undelivered web master has one retained immutable notification identity,
volume and original source target. A newer name, duck or balance revision
suspends dispatch until its gain ACK, then preserves that notification when
the master is unchanged. A different master cancels or replaces it. Only an
exact successful notified acknowledgement clears the same retained identity;
a stale ACK cannot clear a replacement command or a newer FLUSH generation.
A successful old-revision ACK can satisfy the retained same-master command
without reporting the newer revision delivered. Repeated wakes for an already
applied definition do not overwrite pending notification intent.

An accepted same-value phone ACK has the same worker payload as a same-master
administrative ACK. It may preserve redundant feedback of that same value to
the original phone; it cannot change the master or target a successor. A
phone ACK with a different master cancels the old web notification. Once the
notification succeeds, subsequent administrative revisions do not resend it.

The dedicated receiver thread performs no blocking audio callback. It bounds
a complete encrypted AP2 event exchange, including principal connection and
event-mutex acquisition, by one 750 ms monotonic deadline. It uses nonblocking
send/recv and short poll intervals, upstream pairing crypto, bounded frame and
response buffers, complete RTSP status/body parsing, and matching CSeq when
present. The worker bounds its acknowledgement wait at one second. An event
channel that is not ready is retried at most three times per edit/source.

A partial or ambiguous encrypted exchange retires only optional feedback for
that phone connection, since cipher counters cannot safely be replayed. Music
and the authoritative room gain continue. A new phone connection gets a fresh
event channel. The health API reports `receiver_volume` delivery/unavailable/
timeout/protocol status independently of audio readiness. Classic AirPlay
sender feedback is explicitly unsupported; this input design uses AirPlay 2.

## Sender echoes and verification limits

AP2 SET_PARAMETER volume callbacks carry no origin or saved-room revision.
The worker retains at most 32 exact source/generation/value receipts for two
seconds and suppresses matching recent command echoes before committing a new
phone intent. Different values, a successor session and a new flush generation
remain normal phone input. A same-value human move inside that interval is
indistinguishable from an echo. When it matches the current master, the gain is
already the same; when a newer web edit superseded that earlier sent value, a
human returning to it may be temporarily ignored. A sender echo outside the
interval is indistinguishable from a later human edit. Actual
sender/application behavior therefore needs the physical test below.

The notification plist is the upstream development protocol from
[Shairport Sync commit 9a168bf774bb](https://github.com/mikebrady/shairport-sync/blob/9a168bf774bb78d936c9a4557a44d4aa1a07cc5a/remote/remote.c):
`type=sendMediaRemoteCommand`, `value=dvlc`, and matching unit volume at the
root and in `params`. The maintainer describes AP2 event-channel control work
in [discussion 2262](https://github.com/mikebrady/shairport-sync/discussions/2262).
This backport intentionally implements a bounded exchange rather than calling
the pinned blocking event helper or the upstream wrapper that re-enters player
volume callbacks. The pinned original initial updateInfo event helper remains
outside the new lane; contention with it is bounded by the new try-lock gate.

## Build and tests

Pinned receiver Git HEAD remains 7bad231c18368dbd26f298577f6210e36e4b0797.
Keep its real Git metadata through staged builds: the receiver reports its
truthful dirty Git origin and feature `-shiri-timed3-startup1-volume1`.
The builder pins the additive receiver patch by SHA256, applies it after the
clock and startup layers, sanitizes its exact C files and reserves manifest
argument 28. OwnTone transition27 and paused-speech29 remain independent.

`tests/native/check_receiver_volume.py --source <composed backend> --sanitize`
compiles the exact added transport and receiver lane with controlled pairing
and plist seams. Real Unix sockets exercise fragmented acknowledgements,
bounded timeout, EOF, current-source/revision fencing, optional lane retirement,
allocation cleanup and joined shutdown. The full Ubuntu receiver build links
against the real pinned protocol libraries. These seams do not certify crypto
or iPhone UI behavior. The existing startup14 and clock50 tests remain strict:
only the SHA-pinned exact receiver overlay can be backed out to the unchanged
PCM callback before its original clock gate runs.

Python tests use real control sockets for coalescing, stale replies, phone
commit origin, bounded retry, idle defaults, source replacement, echo handling,
failure retaining music, EOF and shutdown. macOS uses a fixed-message stream
socket lifecycle seam because it has no Unix SEQPACKET; the Linux-only test
uses the actual packet listener and SO_PEERCRED acceptance/rejection.

Physical acceptance still requires: connect and play without a web-volume
nudge; move the web master while playing and paused and observe the iPhone's
per-zone displayed master; move the iPhone master repeatedly including mute
and unmute; confirm per-speaker balances survive both directions; pause for
more than 40 seconds and resume; rapidly alternate phone/web moves; switch
sender sessions with an outstanding web edit; disconnect/reconnect after a
failed feedback exchange. Event acknowledgement alone does not prove that an
application rendered its slider correctly.
