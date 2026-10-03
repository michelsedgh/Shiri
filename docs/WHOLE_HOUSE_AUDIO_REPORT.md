# Whole-house audio assessment — October 2, 2026

Shiri's current AirPlay music engine is usable on the live test VM. The user
confirmed first play, phone-to-web and web-to-phone master volume, and playback
after a 40-second pause. The whole-house microphone, Nobly integration and
automatic acoustic calibration system still require substantial work. Those
are separate from the completed software qualification and this single real
speaker check.

This report records the requested design, current implementation, measured
limits and recommended next work. Statements about future features are
proposals. See [the live handoff](LIVE_TEST_HANDOFF.md) for the deployed instance
and [speech acceptance](SPEECH_ACCEPTANCE.md) for qualified software measurements.

## Intended system and current coverage

Each configured zone exposes its own native AirPlay 2 receiver. A phone selects
one zone or groups several using its normal AirPlay controls. Shiri delivers
each zone's audio to its assigned speakers; those speakers can use different
supported output transports. Speech targets a specific zone and lowers music
temporarily while music continues advancing. One room master volume is shared
with the phone. Per-speaker attenuation is a persistent balancing adjustment.

Chromecast input remains deferred at the user's request; Bluetooth input is
excluded. Chromecast speaker output and Bluetooth speaker output are different
capabilities from those inputs. A vendor-linked Bluetooth speaker group is
addressed through its paired primary device; its followers and internal timing
remain controlled by the vendor.

| Area | Implemented or verified | Work remaining |
| --- | --- | --- |
| Native phone music | AirPlay 2 receiver per zone, native presentation timestamps, backend readiness before admission; real iPhone/Sonos play and pause/resume confirmed | Actual multi-zone phone grouping, mixed physical speakers and long household sessions |
| Volume | Shared phone/web room master and saved speaker balance; both master directions confirmed | Acoustic balance and vendor-specific response tests |
| Speech overlay | Streamed, exact-zone WebRTC audio mixed in OwnTone; music duck/restore and cancellation software-qualified | Physical first-audio latency, Nobly producer integration and common-calendar multi-zone speech |
| Lifecycle | Owned service/device/network resources, bounded recovery and rollback; actual VM reboot passed | Broader power, network, standby and physical Bluetooth recovery trials |
| Calibration | Probe generation, shared-clock recording analysis, guarded offset application, fresh verification and rollback | Distributed room capture, clock qualification, automatic campaign playback and whole-house solver |
| Microphones/Nobly | Stable external room bindings and room-addressed speech boundary | Room agents, microphones, wake-word/ASR routing, playback reference for echo cancellation and presence registration |
| Capacity | Eight configured zones, fixed daemon identities and local audio slots | Configurable allocation and measured capacity with actual outputs |
| Video audio | Audio can be received from compatible sources | Separate screen-to-speaker lip-sync qualification; Shiri is not an AirPlay video receiver |

The architecture continues to use OwnTone's conversion and output transports.
Shiri coordinates zone ownership, preserved input timing and speech around that
backend. Replacing OwnTone with a new transport scheduler would discard useful
backend behavior and create more timing work. A reproducible transport defect
should be fixed at the responsible layer. Starting another OwnTone instance
alone does not provide a shared clock or eliminate a changing speaker delay.

## Live startup, access and addressing

On October 2, `shiri-runtime.service` and `shiri-api.service` were explicitly
enabled and the **Shiri Speaker Test** VM was rebooted. Both units started
without a manual start command. Runtime readiness, the saved room, its Sonos
selection and all nine owned actors were verified on the new boot. Saved room
volume remained 15; no test changed the user's volume.

Use **http://shiri-speaker-test.local:8080/** while the VM is running. The current
fallback is **http://192.168.1.200:8080/**. These are the VM's control addresses.
The room receiver currently uses **192.168.1.109**. Control and receiver
addresses are distinct; neither address should become a room identity. The
previous localhost preview/tunnel addresses are temporary and do not survive
every VM restart.

The live test explicitly sets `SHIRI_ALLOW_UNAUTHENTICATED=1`. The UI and API
therefore work without an access token, using the real backend. Same-origin
write protection remains active; a foreign-origin edit was rejected with HTTP
403. Authentication remains the default for other installations. Anyone who
can reach this opted-in instance can control it. Remove the flag or set it to
`0`, then restart the API service to restore token access.

Only the API/settings package files changed for this access option; the other
50 production package files and native audio binaries remain unchanged.
Thirty-four focused access/API tests passed. The previously qualified package
and private guest environment backup remain available for rollback.

Future Nobly presence should use a stable installation ID, stable zone IDs,
capabilities and the current control address. Publish readiness after the
runtime and intended rooms are ready, renew a bounded lease, and update it when
reachability changes. Publish individual receiver addresses separately. Nobly
does not exist yet, so no registration service or announcement has been
implemented or sent. The working local hostname already reduces dependence on
a manually remembered control IP.

## Occasional extra connection time

An additional one or two seconds on a cold connection is plausible, but the
reported delay has not been timed or attributed. There is no normal fixed
one-to-two-second sleep in the repaired admission path. A cold speaker can need
authentication, wake-up or connection work; the phone and wireless network can
also contribute. These are possible causes, not a diagnosis.

The first-play repair waits until selected downstream outputs are actually
ready before acknowledging the phone's successful setup. Returning success
earlier previously allowed a connected phone with silent output. The native
transition deadline and outer setup deadline are maximum failure bounds, not
deliberately inserted buffers. A warm, already-ready output can reuse its
readiness.

The next useful improvement is a bounded success trace, with one correlation
ID for phone session, zone and backend incarnation:

1. First accepted phone request and setup arrival.
2. Native begin, selected-output start and each output-ready callback.
3. Grant/setup reply, first received PCM and its presentation timestamp.
4. First backend emission; later, the room microphone's first audible sample.

Compare fresh-after-idle connections, warm reconnects and pause/resume. Record
median, 95th percentile and failed starts. This distinguishes slow discovery,
phone negotiation, downstream startup and queued media. Keep the readiness
barrier; optimize the measured delay. Upstream's negotiated playback latency
also differs from the time a phone spends connecting. [Shairport timing
documentation](https://github.com/mikebrady/shairport-sync#latency-stuffing-timing).

## Music and streaming speech latency

Shiri accepts speech as a stream and does not wait for an entire utterance.
Speech enters the common OwnTone mix directly rather than traveling through
the music receiver again. The current zero-correction software policy is:

| Selected route | OwnTone output lead B | Shared music horizon H when this is the slowest enabled route |
| --- | --- | --- |
| Local ALSA, including private framed Bluetooth output | 40 ms | 140 ms |
| Chromecast or PulseAudio route | 250 ms | 350 ms |
| AirPlay output | 500 ms | 600 ms |

These values are software scheduling leads, not measured speaker latency.
Enabled zones share `H = max(B) + 100 ms` to preserve a common music calendar.
Negative saved corrections can require more lead; positive corrections add
output delay. A mixed zone must retain the slowest selected route's lead.
Speech does not alter the frozen music horizon. Bluetooth hardware buffering
and a vendor group's internal delay are additional measurements.

The earlier four-second default relay horizon is not the current policy. Nor
does a universal zero buffer work: codecs, transport deadlines and hardware
queues need usable lead. In the pinned AirPlay output implementation, reducing
the requested lead to 250 ms makes its calculated packet window invalid;
500 ms preserves a positive window. Smaller values require a validated change
to that backend/protocol path, not just a changed constant.

The qualified local software speech matrix measured cold offer-to-ready at
69–201 ms and warm request-to-first-encoder-frame at approximately 8–22 ms.
Its zero-correction encoder-to-decoded-reference intervals were approximately
125–186 ms after independently measured timestamp uncertainty. These results
belong to the original qualified boot/profile and local digital reference.
They exclude model/TTS generation and physical speaker/radio delay, and do not
measure first Shiri-received chunk to acoustic output on the current Sonos.
They must not be presented as a 22 ms audible response promise.

For Nobly's agentic responses, measure the requested boundary explicitly:
first received speech chunk → decoded PCM → mixed PCM → backend emission →
first audible sample. Also measure negotiation separately, completion and
cancellation tail. Start peer/output preparation when an interaction begins,
before generated audio arrives, and reuse a bounded healthy session. Current
speech sessions expire after 30 seconds without audible speech; an indefinitely
silent peer is not a supported always-ready mechanism.

The pinned WebRTC receiver has four-frame audio prefetch. A speech-specific
smaller jitter window is a worthwhile experiment only with packet-loss,
reordering, complete-prefix/tail and music-continuity checks. No unqualified
global buffer reduction was deployed for this report. Already-mixed downstream
audio cannot be recalled by canceling an upstream queue without also affecting
music; measured small queues improve both onset and barge-in.

For the strongest conversational latency, qualify a zone configured with a known
low-delay local output first. Current speech reaches that zone's selected
assigned outputs through one common mix; selecting a separate fast speech
endpoint would require an explicit future routing feature.
Synchronized whole-house speech needs a separate common utterance calendar and
per-route delivery lead. Sending unrelated WebRTC offers to several rooms does
not establish simultaneous audible speech.

## Whole-house calibration with room Orins and USB microphones

One microphone or array on each Orin Nano is a workable design. NVIDIA documents
USB audio through ALSA, with formats dependent on the attached device. Choosing
one microphone model is not required to define the capture interface now.
[NVIDIA USB audio guide](https://docs.nvidia.com/jetson/archives/r36.5/DeveloperGuide/SD/Communications/AudioSetupAndDevelopment.html#usb-audio).

Independent USB microphones normally have separate ADC clocks and capture
chains. Their recordings therefore need a measured relationship to time, not
just a network upload timestamp. A microphone's observed arrival includes
speaker delay, acoustic travel, capture/DSP delay and clock offset. Unknown
microphone delay cannot safely be applied as a speaker correction.

Each room agent should record raw calibration audio locally with:

- Stable microphone/channel identity and a new capture generation after
  restart, unplug or an overrun.
- Actual sample rate/format, monotonic sample indices and explicit missing data.
- Requested and returned ALSA timestamp types, clock identity and known or
  unknown accuracy; sample-to-host-time observations and uncertainty.
- Device, driver, firmware and active DSP settings, with bounded local storage.

ALSA can report several timestamp locations and fallback when a requested type
is unsupported. USB timing may use indirect frame-counter estimates; internal
processing delay can remain uncertain. Check the actual returned information
rather than treating a nanosecond-valued field as nanosecond accuracy.
[Linux audio timestamping](https://docs.kernel.org/sound/designs/timestamping.html).

Wired PTP is worth qualifying on the selected carrier/NIC/driver. Inspect
hardware timestamp and PHC support rather than assuming every Orin Nano has it.
Synchronizing host clocks does not automatically synchronize USB sample clocks
or measure ADC/DSP delay. Estimate sample-clock skew separately and recalibrate
the capture chain after relevant changes. [LinuxPTP clock synchronization](https://www.linuxptp.org/documentation/phc2sys/).

There are three practical measurement routes:

| Route | What establishes comparability | Main constraint |
| --- | --- | --- |
| Shared-clock reference | Multiple microphones captured on one ADC/timebase | Initial reference setup needs appropriate hardware |
| Qualified distributed capture | Host-clock mapping, USB sample-clock estimation and measured capture-chain delay | Every device/driver/DSP route needs uncertainty and reconnect qualification |
| Acoustic overlap graph | Each mic compares identifiable speakers on its own clock; overlapping comparisons connect rooms | Disconnected rooms need another reference or a qualified capture chain |

The overlap approach can cancel a microphone's fixed delay when it hears
multiple identifiable outputs. A mic hearing only its own speaker in a sealed
room cannot by itself distinguish that speaker's delay from its own delay.
Adjacent-room overlaps, a temporary cross-room reference or independently
qualified distributed capture resolve that ambiguity. This is an engineering
design proposal, not a distributed feature already implemented.

Start with separately identifiable probes per speaker, then measure normal
grouped playback. Identical probes played everywhere simultaneously can create
ambiguous arrivals and echoes. Estimate relative offsets, changing delay,
clock-rate drift and uncertainty. Comparisons require a known relative probe
emission timeline and accounted-for acoustic geometry. A connected measurement
graph then permits whole-house relative comparisons; inconsistent comparison
cycles reveal a bad measurement or unstable route. Independently started
programs or unrelated probe times do not establish this common timeline.

Apply only a stable correction supported by repeated evidence. Require fresh
post-change recordings and retain rollback. A constant offset cannot repair
changing network buffering or sample-rate drift. Those require transport/clock
improvements or an explicitly looser compatibility profile. OwnTone itself
documents that Chromecast cannot be precisely synchronized with AirPlay;
Shiri's scheduling policy does not remove that limitation.
[OwnTone Chromecast documentation](https://owntone.github.io/owntone-server/audio-outputs/chromecast/).

Also declare what is aligned: electrical output, arrival at a fixed listening
position, or an adjacent-room transition. Speaker-to-microphone distance is a
real delay; one correction cannot make every seat acoustically simultaneous.
Arrays should expose raw channels for measurement. Beamforming, AGC, AEC and
noise suppression belong to the voice path unless their timing is qualified.

Current [calibration](CALIBRATION.md) analyzes shared-ADC stereo recordings,
checks repeated markers and rejects unstable/ambiguous evidence. It supports
review, guarded apply, verification and rollback. It does not yet coordinate
Orin recordings or automatically play a whole-house campaign. Short captures
also do not establish long-term stability.

A useful first distributed milestone is two Orin capture nodes checked against
an independent shared-clock reference. Repeat over microphone reconnects,
speaker wake-up and long captures with inference, USB and network activity.
Only enable automatic correction after independent recordings validate the
estimated timing. Then expand the capture fleet and measurement graph.

## Capacity and CPU measurements

The eight-zone limit is a conservative implementation allocation, not a
measured limit of this Mac. `MAX_ROOMS=8`, settings/database validation, fixed
daemon identity slots and Bluetooth/local Loopback mapping currently agree on
that capacity. Six role identities per slot plus shared identities reserve a
fixed identity bank. Raising one constant leaves other allocations inconsistent.

Expansion should use explicit configurable capacity, stable allocation,
migrations and resource admission, with qualified active-load profiles. The
user does not need to choose a final room count now. Active zones, speakers per
zone, codecs, resampling, simultaneous speech, encryption, wireless airtime and
USB/Bluetooth topology all matter more than the count of saved room names.

The intended deployment connects Shiri by wired Ethernet. Wire room Orins where
practical as well; this keeps the server and measurement traffic off a wireless
hop. Wireless speakers remain supported. For larger homes, qualify the actual
mesh layout, preferably with wired backhaul where practical, including speakers
on different nodes. Good coverage is helpful but does not by itself measure
discovery reachability, packet jitter, roaming or stable audible alignment.
Retain the wired-server assumption in future load and recovery profiles.

The actual host is an **Apple M3, eight cores, 16 GiB RAM**. **Shiri Speaker Test**
has **four vCPUs and 4 GiB RAM**, using hardware virtualization and Ubuntu's
generic kernel. Bounded silent profiling completed on that VM without changing
rooms or sending audio to speakers. Each processing stream continuously decoded
48 kHz stereo ALAC music and Opus speech, used the production timed FIFO and
speech output, ran the maintained OwnTone C duck/mix, resampled to 44.1 kHz, and
encoded one ALAC output. The final profile used OwnTone's 352-frame encoder
cadence, including its post-open frame-size override. [OwnTone encoder
implementation](https://github.com/owntone/owntone-server/blob/29.3/src/transcode.c#L635-L641).

Each load ran for 20 seconds, following a five-second baseline. Here **100% CPU
means one core**, not the entire machine:

| Simultaneous processing streams | Pipeline CPU, one VM core | Entire QEMU CPU, one Mac core | Entire QEMU share of eight Mac cores | Private processing workload peak memory |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 5.41% | 46.69% | 5.84% | 53.5 MiB |
| 2 | 10.20% | 52.49% | 6.56% | 62.0 MiB |
| 4 | 16.00% | 66.59% | 8.32% | 78.8 MiB |
| 8 | 25.76% | 75.53% | 9.44% | 112.8 MiB |

The private processing workload had a 45.0 MiB shared baseline; its incremental
memory was about 8.5 MiB per processing worker. These numbers exclude native
zone-daemon memory. In a separate ten-second observation, the actual configured
one-room system, with its player stopped, used approximately **13% of one VM
core and 293 MiB** across its eleven distinct service cgroups. About **61 MiB**
belonged to that zone's actors; most remaining memory belonged to the shared
API/runtime. This is an idle observation on this boot, not an eight-zone active
memory promise. The complete QEMU resident footprint varied around 1.6–2.0 GiB
across the two sweeps; guest RAM allocation remained 4 GiB. Summing worker RSS
would double-count shared pages and is not how workload memory was calculated.

All fifteen workers in the final sweep processed 1,000 music/speech blocks and
2,505 ALAC output packets each, with zero measured FIFO/speech drops, speech
refusals or expired frames. All benchmark children exited, and production
service PIDs, runtime readiness and the owned-actor ledger remained unchanged.
The low-priority run recorded three scheduling excursions over 20 ms, with a
maximum arrival lateness of 24.2 ms; one processing block took 24.6 ms. Worst
per-worker 95th-percentile processing times were 1.6–2.1 ms. The preserved
calendar and zero drops do not establish a real-time deadline guarantee.

The workload was restricted to private local IPC, CPU quota of two cores and
512 MiB memory, with low-priority workers. No memory-limit events occurred;
the one observed quota throttle occurred during setup. The first 4,096-frame
codec profile and both smoke checks are retained alongside the more
representative final 352-frame profile.

This measures useful processor throughput, **not full live-zone capacity**.
AirPlay encryption/negotiation, WebRTC SRTP/ICE, PTP, actual discovery, physical
speaker/network traffic and additional per-speaker codecs/fanout were excluded.
Codec/resampler residual tails were not drained; this is not a new speech
completion qualification. Per-worker cost changed with warming/cache/power
conditions, so linear extrapolation beyond the measured loads is inappropriate.
The result suggests substantial compute headroom on this Mac. Full actor
scaling, actual transport fanout, sustained timing and network behavior remain
the tests needed to lift the eight-zone cap responsibly.

Original receipts, scripts, package, failures and the derived capacity summary
are retained privately in
`/Users/homr/Documents/Shiri-Validation/release-2026-10-02/batch-0013`.

## Speaker standby and reconnection

Current room health checks run every five seconds and failed starts use bounded
backoff up to 60 seconds. Native output protocols already have their own
heartbeats. OwnTone also stops idle outputs; keeping a zone's services alive
does not necessarily keep the physical speaker transport warm. Bluetooth output
admission currently requires an already-connected, unambiguous paired A2DP
endpoint. It does not implement a general powered-off-speaker wake feature.

Add a persisted, per-speaker policy with progressively stronger modes:

| Mode | Intended behavior |
| --- | --- |
| Normal | Current protocol health, idle release and bounded reconnect |
| Warm for a period | Retain an owned healthy transport for a limited interaction/idle window where supported |
| Always ready | Opt-in supported transport retention, with schedules and visible health |
| Continuous silent audio | Opt-in only when real hardware tests show it necessary and effective |

Implement retention inside the backend's actual transport lifecycle and
ownership rules. Do not invent a music session to conceal standby. Observe
whether silence really prevents a given speaker's sleep; some devices can
detect silence. Continuous audio uses energy/network capacity and may hold a
session another controller wants. Give it an expiry, quiet schedule and clear
release behavior.

Use jittered, staggered checks, bounded concurrency, exponential backoff and a
circuit breaker. Reconnect only the saved device identity; an IP alone does
not identify a speaker. Avoid broad network scans or duplicate heartbeats
already supplied by OwnTone. Retaining a stale discovery entry is not proof
that the speaker is awake and does not reserve its address.

For Bluetooth, an opted-in reconnect should connect the exact paired primary,
observe its connection, retire the old worker/lease and admit the new endpoint
generation. Vendor followers remain the primary's responsibility. A generic
BlueZ connection attempt cannot turn on every unplugged or powered-off remote
speaker. [BlueZ device connection API](https://bluez.readthedocs.io/en/latest/device-api/).

## Nobly listening, echo control and video

Room Orins/Nobly should own capture, wake-word detection, ASR, agent reasoning
and speech generation. Shiri should own audio delivery, zone state, mixing and
calibration. Use exact stable room bindings for response routing; a changing
IP or a similar room name must not redirect private speech.

Microphones hearing Shiri need a playback reference for echo cancellation and
self-speech suppression. The reference should reflect the actual mixed room
audio, volume and ducking with a known time relationship. That feed is not yet
implemented. Raw calibration capture and processed conversational capture can
coexist, with their different timing contracts made explicit.

Whole-house music synchronization does not automatically provide screen lip
sync. Relaying an AirPlay audio stream to another speaker route introduces
delay that a video source/display may not know. Measure screen-to-audible timing
separately, expose total route delay where useful, and qualify display/source
compensation when available. A controlled wired output is a sensible first
video/gaming target; arbitrary apps and mixed consumer routes cannot yet be
called video-ready.

## Recommended order of work

1. Keep the now-working phone playback and shared volume stable. Add successful
   setup/speech timing traces and retain a reproducible smoke check.
2. Complete bounded compute profiling, then qualify actual speaker fanout and
   native grouping without treating processor headroom as network capacity.
3. Measure physical streamed-speech onset, prefix/completion and cancel tail on
   one fast route and the existing Sonos route. Optimize measured contributors.
4. Add per-speaker standby/reconnect policy with physical idle/wake/recovery
   measurements and bounded network behavior.
5. Define the room-agent capture/playback-reference contract and prove two
   independent Orin microphones against a shared-clock reference.
6. Add distributed calibration campaigns and common-calendar multi-zone speech;
   validate corrections independently before automatic application.
7. Replace fixed capacity allocations and qualify larger active deployments.
   Connect Nobly's real presence, room routing and speech producer when it exists.

There is no meaningful completion percentage for the entire house before the
device and microphone routes are qualified. The music/speech delivery core has
substantial software evidence and a working basic physical music path. The
distributed measurement, microphone echo-reference and Nobly system are the
next engineering phase; they are not finished by the existing test count.
