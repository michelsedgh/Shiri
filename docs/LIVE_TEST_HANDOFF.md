# Live test handoff — October 3, 2026

**Current speech status:** acknowledged initial AirPlay metadata, natural-EOF
drain, live delivery progress and the cold-music input repair are installed.
Cold idle speech still loses opening words with this Sonos output's automatic
PTP timing. The user heard the entire phrase during a controlled temporary NTP
comparison; normal PTP configuration was restored afterward. The next
`outputclock1` build adds saved timing preferences by stable output identity and
reports the effective clock, but is **not installed yet**. See
[the startup diagnosis](SPEECH_STARTUP_DIAGNOSIS.md). Neither control acceptance
nor clock exchanges establish acoustic readiness or multi-speaker sync.

The text-to-speech release is installed in **Shiri Speaker Test**, with its
native MLX worker on the Mac. Open **http://shiri-speaker-test.local:8080/** on
the LAN, or **http://192.168.1.200:8080/** if the hostname does not resolve.
Connect an iPhone to **Shiri Test Living Room** using its AirPlay controls.
Keep the VM running for listening.

The previous release's physical phone test passed fresh playback without a web
volume adjustment, phone volume changing sound and the room slider, web volume
updating the phone, and resume after a 40-second pause. That observation remains
historical: it is not a new physical acceptance test of today's native update.

## Trying the current speech release

Open **Speech voices**, select a model and load it. Wait for `ready`, then use
**Quiet model measurement** to generate a reply without opening room speakers.
Its completed result offers a browser audio preview. Use **Speak** on the
enabled Living Room Test room to send text to its assigned Sonos speaker.
**Stop** cancels that exact request. Models and measurements share one job slot.

Kokoro is the default. Qwen3's incremental audio route is selectable; its pinned
MLX input compatibility repair follows the official full-text CustomVoice
layout and preserves streaming audio. Earlier controlled short replies emitted
first PCM in about 55 ms. The reported live request measured 158 ms; later quiet
probes of that exact text measured 445–461 ms to HTTP PCM. These are different
measurements and conditions, not a guaranteed house-speaker startup delay.
Soprano is another compact English option. Voice/language/speed capabilities
come from the model catalog. See [local streaming speech](LOCAL_TTS.md) for
model limits, setup, API examples and honest latency definitions.

## What is installed

The current 60-file wheel has SHA-256
`5573a5faf5eab4936f66b24b451bfc20062b365992e23a87616e50111365a81a`.
The coherent rollout verified all package files and matching native identity.
Shairport retains the previous `timed3-startup1-volume2` binary, SHA-256
`4e051902f8ea7095385367ca4260372bda4813b8e6a84bd50abe08b8eb2fb5d6`.
OwnTone now ends `idle1-drain1-startupmeta1-coldmusic1`, SHA-256
`be6d2398f134667c319a6ff40b5e32d7b3a1db391dcc63be79d2530025d641fc`.
This rollout preserved room revision 81 and master volume 26, and retired the
old actors and owned networks before replacing the package/native pair.

The earlier `idle1` installation remains a historical checkpoint: its 60-file
wheel had SHA-256
`064fd36922f49f5ae735013206ea0b88856ab13a8edbcde1de2b3522bf29f3f9`,
verified against the Mac worker's package manifest; its OwnTone ended
`balance1-transition1-bed1-event1-idle1`, SHA-256
`0be833a0780a55b5a4ea53fea2aaf4ed2a9bbfa9c885ebf1708d20a12e4cef47`.

The additive native repair makes idle speech use the existing output-only
mixer instead of waiting for six seconds of silent input refill. It preserves
music timing and buffer policy. The actual Linux build reproduced the old
failure and passed sanitized native lifecycle and framed-resampler checks
before installation. See [patch notes](../install/patches/README.md).

The earlier `idle1` software regression passed 4,197 portable tests
(85 explicit/platform skips), 58 browser/client checks and the actual Linux
native build checks. The
14-case live route sequence passed natural speech, cancel/successor and idle
reuse with both Kokoro and Qwen; each confirmed generation cleanup and retained
the same room actors/settings. This confirms software routing and cleanup,
without a new human/acoustic observation. Initial-frame pacing now follows the
room's verified actual PCM admission rather than starting before dispatch.

That earlier installation preserved the exact room revision, assignments and
saved master volume 15, including Sonos endpoint `92539824408726`. Normal service shutdown
retired the recorded actors and released their owned networks before the
package/native swap. No ownership ledger or DHCP reservation was edited
manually. Verified backups and coherent native/package rollback remain private
in `/var/lib/shiri-text-tts-checkpoint-2026-10-03-idle1` in the guest.

## Startup and access

`shiri-runtime` and `shiri-api` remain enabled for VM boot. The earlier real
reboot to `6f410567-1a9d-4669-8ed7-a4b2bdb70b37` verified automatic startup.
Today's update restarted both normally; it did not reboot the VM.

The Mac's `org.shiri.tts` LaunchAgent starts its native worker after this user's
login and keeps it running. Models and its private environment live at
`/Users/homr/Library/Application Support/Shiri/TTS`. The VM reaches it through
`Homrs-MacBook-Air.local:8091`. Wait for the selected model to become ready;
first loading and compilation are intentionally separate from normal speech.

The live LAN UI has the requested `SHIRI_ALLOW_UNAUTHENTICATED=1` opt-in, with
same-origin browser-write checks retained. Other installations remain
authenticated by default. The generation worker always requires its separate
private bearer credential. Its startup is separate from the VM's services.

## Timing and remaining physical checks

Normal output-buffer/relay-horizon defaults remain **40/140 ms** for local or
framed Bluetooth, **250/350 ms** for Cast or Pulse, and **500/600 ms** for
AirPlay. Grouped zones use their slowest route and saved correction. These
settings do not promise total phone-to-speaker latency. There is no generic
four-second speech buffer. The previous digital qualification is recorded in
[speech acceptance](SPEECH_ACCEPTANCE.md); today's release adds separate native
and live software regression evidence in [the checkpoint](CHECKPOINT_2026-10-03.md).

The reported slower first sound after a fast iPhone connection still needs a
fresh session trace. [Music-onset diagnostics](MUSIC_ONSET_DIAGNOSTICS.md)
separate backend preparation, first incoming PCM, original presentation and
FIFO delivery. They do not measure the phone's Play-button time or acoustic
onset. Compare fresh playback and later pause/resume when the phone is available.

The cold NTP speech comparison kept the output on AirPlay 2 / ALAC and captured
five NTP timing anchors, with no PTP anchors or capture drops. First PCM dispatch
was 1,036 ms; 179 ms of speech was already delivered by 1,217 ms while synthesis
took 2,715 ms. All 4.64 seconds were delivered with no
reported speech drops, and the user confirmed the full sentence was heard.
The cold PTP baseline still clipped the phrase and did not show a receiver
timing probe until 2,991 ms. This supports changing this tested output's timing
preference; it does not qualify other speakers, prove clock lock or measure
heard latency. The diagnostic restored the ordinary PTP configuration and
services, so the temporary result is not a permanent installed NTP fix.

Mixed speaker protocols, physical volume balance, native phone grouping,
acoustic offsets/drift, streamed speech heard latency and extended household use
remain later tests. Independently clocked room microphones on Orin/USB systems
fit the [whole-house plan](WHOLE_HOUSE_AUDIO_REPORT.md); their clock uncertainty
and acoustic placement must be measured. [Calibration](CALIBRATION.md) can apply
and verify stable corrections; changing transport timing needs repeated capture.

Chromecast input remains deferred, Bluetooth input is excluded, and a Bluetooth
speaker group is represented by its paired primary. Nobly does not exist yet;
exact externally bound room text/audio APIs are ready for its future client.
Speaker standby policies remain deferred until real devices can be measured.

The new checkpoint preserves generation trials, old failures and successful
software checks outside Git under
`/Users/homr/Documents/Shiri-Validation/release-2026-10-03`. The previous
`release-2026-10-02` evidence and failed attempts remain unchanged.
