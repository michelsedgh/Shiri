# Room readiness and low-latency replies

Shiri assigns a speaker to exactly one room. Phones play through that room's
AirPlay receiver; Nobly sends text or streamed speech to its admitted room UUID.
Keeping the outgoing connection ready is separate from assigning the speaker,
admitting a voice, playing audio, and lowering music.

## Choosing a readiness policy

`SHIRI_SPEAKER_READINESS` selects the installation policy:

| Value | Behavior | Tradeoff |
| --- | --- | --- |
| `ready` (default) | Keep enabled rooms connected automatically, including after long idle periods. | Prioritize first-request latency, as selected in the October 4 product requirements; device-specific standby and power effects require measurement. |
| `adaptive` | Prepare an enabled room at startup; retain its connection for five minutes after recent music or a speech request. Nobly may extend readiness with a presence hint. | Explicit power-saving option; the first request after a long idle can need setup. |
| `on_demand` | Leave connection setup to actual playback and explicit hints. | Least proactive connection activity; cold requests include setup. |

Automatic holds have sixty-second deadlines and renew every thirty seconds.
Shiri manages them without Nobly. Adaptive holds never extend beyond their
current five-minute activity window. Fresh committed music PCM and admitted
speech count as activity; a paused input owner alone does not. Failed automatic
preparation backs off from five seconds to two minutes. A disabled/deleted room
or changed output route revokes its old hold. Volume and speaker balance changes
preserve a compatible connection.

These deadlines are internal crash guards, not limits on permanent ownership.
A healthy Shiri can renew a connection indefinitely. A crashed API cannot leave
an unbounded native hold. A speaker may still close a connection or lose network
access; readiness observations report that condition rather than claiming sound
was heard.

## What keeping a connection ready costs

Upstream OwnTone 29.3 retains stopped outputs for ten seconds. Its AirPlay 2
backend also has a twenty-five-second feedback interval. Shiri's connection-only
hold uses backend setup and that protocol feedback; it does not claim a music
source, admit a speech producer, send a continuous PCM bed, or duck music.
See the pinned [output lifecycle](https://github.com/owntone/owntone-server/blob/29.3/src/outputs.c)
and [AirPlay backend](https://github.com/owntone/owntone-server/blob/29.3/src/outputs/airplay.c).

Network connection, amplifier state and acoustic readiness are different
observations. A retained session may prevent a particular device from entering
its deepest standby state. Existing mains-powered network speakers already
consume power while idle, but manufacturer idle figures do not measure the
increment caused by a held AirPlay session. Sonos publishes
[idle consumption by model](https://support.sonos.com/en-in/article/sonos-power-consumption-while-idle);
that table cannot establish the held-session draw of the tested SYMFONISK lamp.

Measure a representative device with a plug meter under ordinary standby,
connection-only hold and actual playback. Compare at least several minutes per
state, with lamp/lighting loads held constant, and measure first audible onset
with the future room microphone. Record retries and packets during the same
window. For scale, an *illustrative, unmeasured* extra watt per speaker across
twenty speakers would add 175.2 kWh per year if held continuously. Do not infer
that extra watt from network packet counts or Mac CPU usage.

Continuous silent playback is not the default remedy for standby. A device that
cannot remain useful through protocol keepalive needs its own measured profile.
Battery Bluetooth speakers, Bluetooth speaker groups, Cast outputs and local
devices need individual qualification; software connection state alone cannot
prove their amplifiers are awake or synchronized.

## Optional Nobly preparation

Nobly can hint that someone is present or that an agent is forming a reply:

```http
POST /api/v1/nobly/rooms/Living%20Room/warm
Content-Type: application/json

{"request_id":"0123456789abcdef0123456789abcdef","ttl_seconds":120,"purpose":"presence"}
```

Use `purpose: "interaction"` and an optional configured `model_id` to prepare the
speaker connection and request quiet model priming in parallel. A presence hint
does not generate discarded speech. Neither hint lowers music. Actual text
speech starts its music fade after exact voice admission and before delivering
the first PCM chunk.

The response returns an external `lease_id`, `admitted_room_id`, current state
and remaining duration. The external binding is resolved once. Poll, renew or
release by that UUID, even if Nobly's binding subsequently moves:

```text
GET    /api/v1/rooms/{admitted_room_id}/warm/{lease_id}
POST   /api/v1/rooms/{admitted_room_id}/warm/{lease_id}
DELETE /api/v1/rooms/{admitted_room_id}/warm/{lease_id}
```

A renewal body supplies a new `request_id` and `ttl_seconds`. TTL must be an
integer from five through three hundred seconds, measured from admission rather
than setup completion. Retrying the same request never extends its deadline;
conflicting reuse is rejected. Ended leases cannot be resurrected. Request
receipts and terminal external leases are retained for ten minutes within
bounded capacity. Automatic policy holds have no public lease ID and cannot be
renewed or released through these external endpoints.

Independent hints share one native connection hold per room. Releasing or
expiring one caller preserves other callers, music and admitted speech; the
native crash deadline is reduced to the latest remaining caller deadline.
Releasing the last connection hold removes retention without stopping active
music or speech. Room launch generations and stable transport fingerprints
prevent late acknowledgements from attaching to a new route.

Release observations distinguish pending, acknowledged and unconfirmed native
retirement. If control fails, the last acknowledged native deadline remains the
crash bound; Shiri does not claim an earlier physical release was confirmed.

## Model readiness is shared across the house

The persistent generation worker keeps the selected model loaded. Quiet Qwen
tests on this Mac found first HTTP PCM around 57–61 ms on immediate warm repeats,
and approximately 110–224 ms after controlled one-to-thirty-second idle gaps.
Longer-idle requests varied further; these are model/HTTP observations, not
speaker onset measurements or a latency promise.

An explicit interaction hint primes only the first generated chunk, discards it,
and joins the decoder's cooperative reset before reporting completion. In quiet
tests that took about 170–190 ms and roughly 0.13–0.14 seconds of child CPU time.
Generating an entire discarded short greeting took about 2.8 seconds. Successor
utterances were byte-for-byte unchanged. After first-chunk priming, the tested
successor's first HTTP PCM arrived in about 98/138/151 ms at one/three/five-second
offsets. Preparation work and the intervening gap are excluded from those
successor timings; serial priming plus generation is not faster than an already
warm direct request.

There is no default model priming loop. The global worker never loads another
model implicitly, interrupts actual speech, or creates one model instance per
room. A real generation request takes priority over an owned quiet prime and
joins its bounded reset. A busy or mismatched-model hint is reported as skipped.
GPU hints alone were inconsistent in controlled comparisons. Permanent repeated
discarded inference would require separate CPU/GPU energy measurements before
being selected as a house-wide default.

The web Speak panel's **Prepare room quietly** button exercises the same
interaction hint for one minute. It does not play the priming audio. Room and
model observations remain separate; `acoustic_ready` is unknown until measured
with a microphone.
