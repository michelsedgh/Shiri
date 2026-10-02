# Native receiver evaluation

Research recorded 2026-09-30, with scope updated 2026-10-01. AirPlay 2 input
per zone remains required. The user explicitly deferred Chromecast input after
the open-source receiver review; [current feasibility](CAST_INPUT_FEASIBILITY.md)
records that conclusion and future options. A future Cast receiver must accept
existing phone controls without a Shiri sender app. An output adapter, mDNS
advertisement or successful custom-client demonstration does not satisfy that.
See [PRODUCT_REQUIREMENTS.md](PRODUCT_REQUIREMENTS.md).

## AirPlay

Shairport Sync provides an AirPlay audio receiver, including an AirPlay 2
build. The candidate has pinned that receiver and matching NQPTP, with one
private receiver namespace per zone. Actual stock-iPhone discovery, native
multi-selection, control and final speaker timing remain acceptance tests.
Its ALSA timing must survive the later mixer/output relay, rather than being
assumed from successful receiver startup.
[Shairport Sync](https://github.com/mikebrady/shairport-sync)

## Cast candidates

| Candidate | Established capability | Open production requirement |
| --- | --- | --- |
| Google Cast Web Receiver SDK | Receiver application running on a Cast device | It does not by itself make an Ubuntu process a phone-compatible Cast device |
| Google Open Screen | Open Cast discovery/control/media-streaming modules and standalone examples | Verify stock-phone authentication, required streaming/media modes, headless PCM capture and independent per-zone identities |
| openchromecast | Rust media-URL receiver with transport/control/playback and custom-client tests | Its authors report stock Android Google apps reject the default self-signed device credentials; mirroring is outside its scope |
| shanocast | Linux Open Screen adaptation demonstrated with Chrome | Its documented authentication uses precomputed signatures from another receiver; no verified production stock-phone/app coverage |
| AirServer desktop | Commercial Cast mirroring | Vendor documents that ordinary in-app Cast media is unsupported; multiple separately named zone receivers and PCM integration are unverified |
| StreamUnlimited StreamSDK Lite + MultiStream | Commercial Linux container SDK with ALSA/PipeWire/shared-memory audio and multiple simultaneous players; official MultiStream leaflet lists Google Cast | Confirm supported Ubuntu ARM/platform and security requirements, independent per-instance Cast identities/credentials, PCM/control APIs and stock-phone acceptance |
| WiiM Pro/Pro Plus external adapter | Chromecast Audio receiver with analog/optical/coax audio outputs and a published product control API | Requires a physical receiver and capture channel per zone; verify API behavior during native Cast, session takeover and captured timing |
| Certified/managed Cast hardware | Candidate external input adapter | Verify controllable per-zone capture, supported phone modes, recovery and cost; hardware availability would be a real deployment dependency |

Google's receiver SDK runs applications on a Cast receiver device, so hosting
a receiver HTML page on Shiri is insufficient.
[Google receiver documentation](https://developers.google.com/cast/docs/web_receiver/basic)

Open Screen is a real implementation of discovery, application control and
media streaming. It deserves direct source/build evaluation; its existence
must not be presented as proof that ordinary phones accept any server using
it. [Google Open Screen](https://chromium.googlesource.com/openscreen/+/HEAD/README.md)

The openchromecast authors distinguish media-URL playback from streaming and
document a device-certificate trust requirement in stock Android clients.
Their reported Python-client success therefore does not pass Shiri's native
phone gate. Their suggested extracted device credentials are not a verified
deployment basis for this product.
[openchromecast implementation and limitations](https://github.com/yukarikaname/openchromecast)

Shanocast's README documents Chrome interoperability and its authentication
method; compatibility and maintainability need independent evaluation.
[shanocast](https://github.com/rgerganov/shanocast)

AirServer's desktop mirroring and its hardware media-casting capability must
be evaluated separately. A commercial receiver is not automatically a
headless, multi-zone input backend.
[AirServer limitations](https://support.airserver.com/support/solutions/folders/43000556156),
[AirServer hardware network guide](https://support.airserver.com/support/solutions/articles/43000512459-airserver-connect-hardware-network-and-security-information)

## Licensed Linux prospect: StreamUnlimited

StreamSDK Lite documents a Linux container integration with ALSA, PipeWire or
shared-memory audio, StreamAPI playback metadata and source switching, and
simultaneous streams for different zones. Its stated minimum kernel is 5.10;
TrustZone support is listed as a Google Cast-specific desirable feature.
This is a credible architecture match, but the public requirements do not
establish compatibility with this Mac's Ubuntu ARM virtual machine.
[StreamSDK Lite](https://www.streamunlimited.com/streamsdk-lite/)

The official MultiStream leaflet explicitly lists Google Cast among supported
services and depicts four separate StreamSDK instances, an instance manager
and a sound server on Linux. It describes multiple simultaneous audio players
on Stream1955/Stream1832 modules and integration through StreamSDK Lite.
Multiple audio players do not alone prove that each instance advertises an
independently named, stable Cast receiver accepted by stock phones.
[MultiStream leaflet](https://streamunlimited.com/wp-content/uploads/leaflets/MultiStream.pdf)

StreamUnlimited also documents Google Cast for Audio 2.0 porting and
certification on third-party or custom hardware, and an approved Stream210
Cast platform. This establishes a production supplier path, rather than
assuming that developer-generated device certificates are sufficient.
The leaflet requires a configuration app for initial device setup; clarify
whether setup can be integrated into Shiri without a separate playback app.
[GC4A 2.0 offering](https://streamunlimited.com/wp-content/uploads/leaflets/Google%20Cast%20for%20Audio%202.0.pdf),
[certified Stream210 platform](https://www.streamunlimited.com/stream210-is-now-a-google-cast-certified-platform/)

Before obtaining or integrating an evaluation package, confirm the supported
CPU/OS/security platform, licensing and credential provisioning for each
virtual instance, independently named receivers, supported stock music apps,
PCM format/timestamps and control/session/disconnect APIs. Then run the
admission test below with two concurrent native-phone sessions. No vendor
has been contacted, no SDK installed and no phone interoperability verified.

## External audio adapter fallback: WiiM

WiiM Pro/Pro Plus specifications list Chromecast Audio input and analog,
optical and coaxial audio outputs. One receiver per zone could feed a host
capture interface before Shiri's mixer. This is an external hardware adapter,
not a software-only virtual receiver; capture interfaces, wiring and
additional device recovery become deployment requirements.
[WiiM specifications](https://wiimhome.com/WiiMPro/specs)

The published HTTPS API documents playback status, pause/resume/stop, volume,
input switching and output selection. Its response examples use WiiM Mini,
so those methods must be tested on a Cast-capable Pro while native Cast is
active. The document does not prove Cast-specific session identity,
disconnect/takeover semantics or event delivery. Validate native phone
control, gain ownership, capture rate/latency and restart recovery before
admitting this adapter. No hardware has been purchased or tested.
[WiiM product API](https://www.wiimhome.com/pdf/HTTP%20API%20for%20WiiM%20Products.pdf)

## Admission test for an input adapter

Before selecting a backend, reproduce these on the actual network:

1. Two separately named receivers coexist and remain stable across restart.
2. Stock phone clients discover and authenticate without modified software.
3. Required native media and audio-streaming paths produce capturable PCM.
4. Start, pause, stop, volume and disconnect have bounded, identifiable events.
5. Competing sources can be admitted/rejected or disconnected by the zone's
   source policy; delayed events cannot affect a newer owner.
6. Captured audio can enter the existing zone mixer without leaking to host
   speakers or another zone, with its timing provenance retained.
7. Backend/network failure releases only owned resources and remains visible.

Current conclusion: **no inbound Cast backend has passed these tests yet**.
Keep it as an unresolved required implementation gate, and investigate the
responsible receiver/authentication boundary before expanding output code.
The existing OwnTone output engine does not supply this missing input path.
