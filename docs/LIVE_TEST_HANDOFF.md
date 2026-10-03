# Live test handoff — October 2, 2026

The qualified rebuild is running in **Shiri Speaker Test**. The user confirmed
fresh iPhone playback without a web-volume adjustment, phone volume changing
both audible volume and the web room slider, web volume updating the phone,
and normal resume after the requested 40-second pause.

Open **http://shiri-speaker-test.local:8080/** on the LAN, or
**http://192.168.1.200:8080/** if the hostname does not resolve. Connect the phone
to **Shiri Test Living Room** using its normal AirPlay controls. The temporary
localhost forwards, including ports 8767 and 52609, stopped after reboot. Keep the
VM running for listening.

Both `shiri-runtime` and `shiri-api` are already enabled for boot. The actual
reboot to `6f410567-1a9d-4669-8ed7-a4b2bdb70b37` verified both enabled and active
without a manual start. Fresh installations intentionally only configure the
services; their operator still uses `systemctl enable --now` after setup.

The current live configuration explicitly sets `SHIRI_ALLOW_UNAUTHENTICATED=1`:
the UI and real state API work without a token, while same-origin browser-write
checks remain enforced. Authentication stays enabled by default elsewhere.
This opt-in does not enable simulation.

## What is installed

The live installation retains the qualified candidate182 native pair:
Shairport `timed3-startup1-volume2` and OwnTone
`balance1-transition1-bed1-event1`. The current 52-entry wheel has SHA-256
`486df5161544bd498e6b2e25f909a00876f4ccc9f5b40557facad3d892125020`;
50 package files match the qualified baseline, with only API/settings changed
for the explicit authentication opt-in. No audio or native code changed.

Room names, speaker assignments and saved timing profiles were preserved. The
current rebooted room retains saved master 15 and Sonos endpoint `92539824408726`.
The earlier phone-test readback matched room/backend master 28, with no room
error or pending volume update and the same nine restrictive daemon identities
through that test. These historical observations and the user's latest saved
volume remain separate; the reboot does not reset user intent.

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
| Qualified baseline regression | [CI37055838875](https://github.com/michelsedgh/Shiri/actions/runs/37055838875): 3,988 Python tests passed, 19 explicit/platform skips; native and browser/frontend checks passed on code checkpoint3797e23 |

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

The final evidence bundle is `batch-0012/INDEX.json` (126 files, 53,256,914 bytes;
SHA-256 `58a44696a1b24d0275bea77cf7227d82a45aea6c1051b8a8c27b897433c7b2a4`).
It includes the original live rollout, rollback/shutdown, user phone report,
post-phone observation and successful CI records. Earlier failures and indices
remain unchanged. That evidence bundle predates the authentication opt-in and
latest live reboot described above; it does not rewrite those historical
receipts or claim that the newer API/settings wheel is the old baseline wheel.
