# Cast input: deferred scope

The user deferred Chromecast input on October 1, 2026 after the receiver
feasibility review. AirPlay 2 input remains required; Chromecast **speaker
output** remains supported through OwnTone. Bluetooth phone input is excluded.
This decision is part of [the product requirements](PRODUCT_REQUIREMENTS.md).
The duplicated receiver candidate/design survey has been removed; its source
history is available in Git.

## Reason for deferral

The October 1 research did not establish a public, supported provisioning path
that makes this Ubuntu ARM VM independently named Cast destinations accepted
by unmodified phone apps. This is the scope of that review, not a claim that
commercial Linux Cast receivers are impossible. No vendor access, binaries,
accounts or device credentials were obtained, and no vendor was contacted.

Google's public SDK supports applications running on existing Cast-enabled
receivers; registering a receiver app does not provision a new generic Linux
Cast device. Open Screen contains real protocol and media components, but its
standalone examples' developer credentials and custom-sender trust do not
establish acceptance by ordinary phone apps. The inspected authentication
implementation validates a device certificate chain and challenge signature.
[Google Cast overview](https://developers.google.com/cast/docs/overview),
[Open Screen standalone guide](https://chromium.googlesource.com/openscreen/+/HEAD/cast/docs/USING.md),
[reviewed authentication source](https://chromium.googlesource.com/openscreen.git/+/e4ca7e5c8f0a72fcb2278a7c30e6ac6770030d50/cast/sender/channel/cast_auth_util.cc).

Output support, mDNS advertising, browser mirroring or a custom sender alone
does not satisfy Shiri's native phone requirement. Certified external receiver
hardware or a legitimately provisioned Linux/OEM receiver could be evaluated
later; neither is a current Shiri dependency or an implemented design.

## Admission requirements if revisited

A future receiver must first demonstrate stock-phone discovery, authentication,
required app/media modes, independent per-zone identities, capturable PCM with
clock provenance, session controls and recovery on the actual platform.
In-app media casting and screen/audio mirroring are separate capabilities.

Only after that proof should an input adapter enter the existing music source
actor. It must bind exact receiver/session generations, quiesce or fence the
previous source before new PCM, and reject stale end/volume/media callbacks.
Its clock conversion and grouped output timing need qualification through the
same native output path. TTS remains an independent overlay and cannot become
a competing music source. See [architecture](ARCHITECTURE.md).
