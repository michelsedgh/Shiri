# Live test handoff — October 2, 2026

The qualified rebuild is running in **Shiri Speaker Test**. The user confirmed
fresh iPhone playback without a web-volume adjustment, phone volume changing
both audible volume and the web room slider, web volume updating the phone,
and normal resume after the requested 40-second pause.

Open **http://localhost:8767/** on this Mac. Connect the phone to
**Shiri Test Living Room** using its normal AirPlay controls. The old temporary
`localhost:58768` link is no longer responding. Keep the VM running for listening.

## What is installed

The live installation uses the qualified candidate182 package and native pair:
52 unchanged production package files, Shairport `timed3-startup1-volume2` and
OwnTone `balance1-transition1-bed1-event1`. Room names, speaker assignments,
saved timing profiles and volume were preserved. The final read-only check found
the room running with no error or pending volume update, matching room/backend
master 28, and the same nine audio-service identities through the phone test.
Both normal services were running; every actor retained its restrictive
namespace policy. These observations leave the user's latest settings intact.

The rollout first retained a complete verified backup of the old installation,
application state, runtime state, native binaries and configuration. It then
retired only the nine recorded old actors. Normal production recovery released
their saved reservations before the API started; no manual ownership-ledger,
network-interface or DHCP-lease edits were used. Backups and unsuccessful
attempts remain preserved privately.

## Accepted software behavior

| Area | Evidence |
| --- | --- |
| AirPlay receiver and volume | Encrypted protocol checks plus the actual iPhone/Sonos report above |
| Zone speech | Complete cold/warm speech, advancing music, ducking/restoration and untouched other-zone checks |
| Timing | Declared 12-case calibrated digital speech matrix; grouped digital audio and offset checks |
| Recovery | Owned actor/network cleanup, manager reload, fault recovery and post-reboot encrypted regression |
| Installation safety | Exact package/native preflight, two-stage rollback and normal rehearsal VM shutdown |

Normal output-buffer/relay-horizon defaults are **40/140 ms** for local or framed
Bluetooth, **250/350 ms** for Cast or Pulse, and **500/600 ms** for AirPlay.
Grouped zones use their slowest route and saved correction. These are Shiri's
route settings, not a promise about total phone-to-speaker latency. There is no
generic four-second buffer. The measured speech performance and its exact
qualification boundary are recorded in [speech acceptance](SPEECH_ACCEPTANCE.md).

## Later physical measurements

The current live result covers one iPhone and its assigned AirPlay speaker.
Mixed AirPlay/Cast/Bluetooth/wired speakers, native phone grouping, acoustic
offsets/drift, physical speaker trims and extended household use still need the
later measurements we agreed on. The [calibration UI](CALIBRATION.md) supports
recording, reviewing, applying and verifying stable corrections; variable radio
or transport timing requires repeated measurement.

Chromecast input remains deferred. Bluetooth input is excluded. A Bluetooth
speaker group is assigned through its paired primary; its vendor manages the
followers. Nobly does not exist yet; the room-addressed speech API is ready for
that future client. See [product requirements](PRODUCT_REQUIREMENTS.md) and
[architecture](ARCHITECTURE.md).

The [checkpoint](CHECKPOINT_2026-10-02.md) records build identities, original
failures and retained evidence. Private validation files live outside Git in
`/Users/homr/Documents/Shiri-Validation/release-2026-10-02`; credentials and full
runtime backups remain protected in the guest.
