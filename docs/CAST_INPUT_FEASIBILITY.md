# Cast input feasibility addendum

Reviewed 2026-10-01 against Google's current public documentation and project maintainers. This supplements `docs/RECEIVER_RESEARCH.md`; it does not replace its admission tests. No SDK, binaries, credentials or accounts were obtained, no vendor was contacted, and no receiver or VM was started.

After this review, the user explicitly deferred Chromecast input for the
current release: "if you cant find a open source receiver lets leave chromecast
for now". This document retains future integration options. Cast speaker
outputs remain in scope.

## Result and scope

**No public, supported, complete receiver/provisioning path was found that turns this Ubuntu ARM VM into independently named Cast destinations accepted by unmodified phone apps.** This is a scoped research conclusion, not a claim that Linux Cast products are impossible: a maintained commercial Linux supplier path exists, with external access and platform constraints. Cast input is deferred by the user for the current release.

A working receiver must support the selected apps' native Cast modes, not only advertising mDNS or accepting a custom sender. The stock phone's chosen app selects the receiver application and content path. In-app media casting and screen/audio mirroring are separate compatibility rows; no universal “anything from any phone” guarantee follows from Cast support.

## Public Google SDK: app development is not device provisioning

Documented facts: Google's SDK supports sender apps on Android, iOS and Web, and receiver apps on Web and Android TV. A receiver app runs on an already Cast-enabled device. Except for the Default Media Receiver, an App ID matches senders to the corresponding receiver application and prevents attachment to an unrelated receiver app. [Google Cast overview](https://developers.google.com/cast/docs/overview)

A custom Web Receiver is a hosted HTML application executed on that device; its App ID must be supplied by the sender. Registering/publishing a receiver application, or registering an existing device's Cast serial for development, does not document issuance of a new Cast device identity for arbitrary Linux hardware. [Web Receiver setup](https://developers.google.com/cast/docs/web_receiver/basic), [Google registration](https://developers.google.com/cast/docs/registration)

**Inference for Shiri:** serving receiver HTML in Ubuntu or registering a Shiri App ID is insufficient. Existing phone apps configured with their providers' App IDs will not simply switch to a Shiri-owned receiver application.

## Open Screen: real Linux protocol implementation, limited proof

Google's Open Screen libcast contains Cast application-launch and streaming components and standalone media-streaming examples. [libcast scope](https://chromium.googlesource.com/openscreen/+/HEAD/cast/README.md)

The current standalone guide supports Linux/macOS. Its example creates developer credentials and explicitly supplies the generated trust certificate to the example sender; Chrome interoperability uses a special certificate command-line flag. These are development arrangements, not evidence of unmodified phone acceptance. [Open Screen standalone guide](https://chromium.googlesource.com/openscreen/+/HEAD/cast/docs/USING.md)

Google's inspected authentication implementation verifies the device certificate chain against trusted Cast roots and verifies a signature over the challenge material. The pinned source is evidence of the authentication boundary, not a newly obtained device credential. [Google device authentication source](https://chromium.googlesource.com/openscreen.git/+/e4ca7e5c8f0a72fcb2278a7c30e6ac6770030d50/cast/sender/channel/cast_auth_util.cc)

The openchromecast maintainer reports that its self-signed credentials block stock Android Google apps, despite custom-client media-URL playback. Shanocast documents Chrome interoperability using precomputed signatures from AirReceiver. Neither is evidence of a maintained, legitimately provisioned generic stock-phone receiver. [openchromecast maintainer](https://github.com/yukarikaname/openchromecast), [shanocast maintainer](https://github.com/rgerganov/shanocast)

## Android bridge: Cast Connect does not intercept other apps

Documented facts: Cast Connect integrates a receiver into an Android TV app using Google's Play Services Cast libraries and a media session. The sender must enable Android receiver compatibility, and the Cast console associates **your Android package with your Cast App ID**. Development requires the device's software Cast serial; without development registration, the documented route requires a Play Store installed app. [Cast Connect setup](https://developers.google.com/cast/docs/android_tv_receiver/core_features)

**Inference:** installing a Shiri Cast Connect APK on an Android TV is useful for senders already targeting that Shiri application. It does not make the APK receive arbitrary existing music apps' provider-specific sessions. Multiple APK processes do not establish multiple separately named native Cast devices. A generic AOSP VM is not shown to be a provisioned Cast device by these SDK instructions.

Capturing an existing Android receiver's playback in a separate app is conditional. Android's playback-capture API requires Android 10+, RECORD_AUDIO, explicit MediaProjection consent, and matching user profiles. The playing app must use an eligible audio usage and allow capture; its most restrictive capture policy wins. The user can revoke the projection token. [Android playback capture](https://developer.android.com/media/platform/av-capture)

**Inference:** an Android capture bridge cannot promise every provider's audio. A certified Android TV's physical audio output is another possible capture boundary, but actual device/output/media compatibility, session control and latency still need qualification. A Cast Connect launch rejection can fall back to a Web Receiver, so rejecting the Android app alone cannot enforce Shiri's zone ownership.

## Maintained Linux supplier path and access constraints

StreamUnlimited's StreamSDK Lite is a container-based SDK with StreamAPI metadata/source switching and ALSA, PipeWire or shared-memory audio. Its stated Linux kernel minimum is 5.10; TrustZone is listed as desirable specifically for Cast. It describes simultaneous sources for zones. [StreamSDK Lite](https://www.streamunlimited.com/streamsdk-lite/)

Its MultiStream material depicts multiple SDK instances on Linux and includes Cast; the CEDIA page describes multiple endpoints in one device and certified Cast integration. Those are credible product capabilities, but do not prove separate stock-phone-compatible Cast identities for each instance on this Ubuntu ARM VM. [MultiStream leaflet](https://streamunlimited.com/wp-content/uploads/leaflets/MultiStream.pdf), [CEDIA integration](https://www.streamunlimited.com/cedia/)

A concrete external access restriction is documented: the supplier's evaluation-kit page accepts business contacts and says it cannot support private projects. No public evaluation download or license for this installation was established. [Evaluation access](https://www.streamunlimited.com/evaluation-kits/)

Before selecting this route, obtain legitimate evaluation access and confirmation of the exact ARM/Ubuntu/VM/security platform, per-instance Cast credential provisioning and identities, required native app modes, setup without a separate phone playback app, session/control events, capturable PCM and timing. Linux container compatibility alone is insufficient. Vendor certification claims do not admit an untested VM.

## Architecture recommendation

Keep Cast as an inbound adapter behind the existing zone source actor, mixer and OwnTone output engine. The viable routes to qualify are:

1. **Licensed Linux/OEM receiver integration**, if the supplier approves the exact deployment and provisions independently named Cast endpoints plus PCM/control interfaces.
2. **One already certified Cast receiver and dedicated audio capture channel per zone**, if a tested control interface can identify, stop/revoke and observe its native sessions. This is an external hardware dependency; it does not implement the user's exact all-virtual deployment by itself.

Use stable receiver identity plus capture channel, never IP alone, to bind a zone. Convert incoming receiver-session and connection-generation events into the existing `SourceToken` boundary. An admission must fence old PCM and callbacks before the new source can write. When AirPlay supersedes Cast, stop/revoke the exact old Cast session and drain its capture path; on failed revocation, expose degraded/rejected status instead of accepting ambiguous audio. A capture channel emitting sound is insufficient proof of session ownership. TTS remains an overlay and must not claim or stop the music source.

Capture timing is a new clock domain: measure ingress delay/jitter and clock drift, preserve timestamps/provenance, and qualify the same mixer/output scheduling path. Do not infer grouped-zone or mixed-output synchronization from Cast input success.

The minimum unavoidable external prerequisite identified here is **a genuinely trusted Cast device/platform and a usable per-zone PCM plus control boundary**: either licensed/provisioned receiver software on an approved platform, or certified receiver hardware with capture. A public App ID, fake discovery advertisement, developer-trusted sender, or custom phone sender cannot replace that prerequisite. The seven existing receiver admission tests still decide acceptance; none has been newly passed by this research.
