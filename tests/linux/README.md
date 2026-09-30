# Manual Linux candidate validation

`check_candidate_services.py` is a manual Linux root harness. It is not collected
by normal pytest and is not run by CI. It validates actual systemd services,
namespace/DHCP ownership and the rootless API against staged candidate binaries.

Prepare the Linux machine manually before running it:

- Stage the candidate source, unit templates and audio dependencies at
  `/opt/shiri-v2`, with Python at `/opt/shiri-v2/.venv/bin/python`.
- Build the pinned backends into `/opt/shiri-v2-deps`; install the dedicated
  namespace DHCP hook and its AppArmor permission.
- Provide systemd, an eligible bridged LAN interface and an ALSA Loopback card
  with at least eight substreams. **Slot 7 must be free.** The harness selects no
  house speakers.
- Keep `/var/lib/shiri-v2-test-runtime` intact between runs. Its installation
  identity and stable MACs are reused; existing owned resources must be cleared
  through their owning runtime before this harness can start.

Run from the staged candidate checkout:

```sh
sudo /opt/shiri-v2/.venv/bin/python /opt/shiri-v2/tests/linux/check_candidate_services.py
```

The paths and service names are deliberately explicit. The harness creates only
`shiri-v2-test-runtime.service` and `shiri-v2-test-api.service`, uses API port 18082
on localhost, and stores its configuration/token under `/etc/shiri-v2-test`. It
creates the non-login `shiri` account if needed. It archives the disposable API
database under `/var/lib/shiri-v2-test-api/archive` before each run; it preserves
the runtime installation identity and token.

The test creates eight disabled rooms to reserve slot 7 for its one enabled
room. It checks authentication, durable rootless database writes, revision
conflicts, real backend startup, host-visible namespace mount/inode ownership,
closed auxiliary OwnTone listeners and denied anonymous LAN control. It sends
SIGKILL only to the verified main process of the exact managed test runtime unit
and verifies automatic broker recovery. Finally, it disables/deletes all test
rooms and stops/disables only the test units.

Results are written to `/tmp/shiri-v2-service-check-result.json`, with service
logs at `/tmp/shiri-v2-test-*.service.validation.log`. The report checks an empty
ownership manifest, no remaining owned namespace/control links, closed slot-7
endpoints, and exact host link/address baseline restoration. Tokens are redacted.
Exit status 0 means all checks and final cleanup passed; inspect the report and
logs when it exits nonzero.

This harness passed on the Ubuntu candidate in 29.2 seconds on September 30,
2026. It does not test a stock phone, native grouped playback, physical speakers,
Bluetooth hardware, Cast input or completed daemon privilege separation.

## Installation trust and state-adoption boundaries

`check_installation_boundaries.py` is a separate manual 64-bit Linux root
check. It creates one randomly named disposable tree under `/opt` and removes
that exact tree on exit. It invokes no apt/build command, changes no systemd
unit, and accesses no production application state.

```sh
sudo /usr/bin/python3 -I /opt/shiri-v2/tests/linux/check_installation_boundaries.py --source /opt/shiri-v2
```

It verifies actual root-owned custom prefixes and normal venv links, refusal
of writable/foreign-owned/linked executable paths, Linux OFD lock persistence,
retained SQLite companion preservation, hardlink/symlink refusal, an idle
connection in another process, exact quiescent database ownership/mode changes,
and exclusion of concurrent service installers. JSON results are printed to
stdout; no token is generated or printed. This checks the adoption helpers;
the complete installer sequence on a fresh host remains a separate gate.
All 16 checks passed on the Ubuntu candidate in 0.11 seconds on September 30,
2026; the retained report is `/tmp/shiri-v2-installation-boundaries.log`.

## DHCP address renewal

`check_dhcp_renewal.py` is a separate manual Linux root check. It creates a fresh
isolated namespace with a dummy interface, then verifies same-IP subnet changes,
changed-IP/subnet replacement and unchanged renewals. It preserves an unrelated
test address and verifies the host network baseline after exact-inode cleanup.
It obtains no LAN lease and does not test a DHCP server's lease table.

```sh
sudo /opt/shiri-v2/.venv/bin/python /opt/shiri-v2/tests/linux/check_dhcp_renewal.py
```

The candidate hook defaults to the same checkout. `--hook /path/to/hook.py`
selects another explicit hook; `--before-hook /path/to/previous-hook.py`
optionally reproduces the former stale-prefix bug first. Results are written
atomically to `/tmp/shiri-v2-dhcp-renewal-result.json`. The kernel reproduction
and all three corrected transitions passed on September 30, 2026; test
namespace removal and exact host network preservation passed too.

## Synthetic AirPlay input and direct-worker speech

`check_airplay_input.py` and `check_airplay_tts.py` are separate manual root
harnesses, not pytest tests or CI jobs. Run them one at a time after stopping
the dedicated candidate test services. Both preserve the existing candidate
installation identity, require an empty ownership manifest at
`/var/lib/shiri-v2-test-runtime`, and reject identities outside the known
`b265...` candidate. They require Linux root, pinned backends at
`/opt/shiri-v2-deps`, the staged candidate Python environment, wired interface
`enp0s1`, and all four Loopback slot-7 endpoints closed. They select no house
outputs. These fixed paths and guards are intentional; do not aim them at a
production installation.

```sh
sudo /opt/shiri-v2/.venv/bin/python /opt/shiri-v2/tests/linux/check_airplay_input.py
sudo /opt/shiri-v2/.venv/bin/python /opt/shiri-v2/tests/linux/check_airplay_tts.py
```

The input harness generates a private 440 Hz WAV and starts an owned synthetic
OwnTone sender. It discovers and selects only the candidate AirPlay 2 receiver
whose name and MAC-derived output ID match, then observes real Shairport →
Loopback slot 7 → mixer FIFO audio and music-start/stop hooks. The source must
keep playing, retain its exact output selection and advance program progress.
This harness passed in 34.7 seconds on 2026-09-30.

The TTS harness adds a real paced 880 Hz Opus WebRTC source directly through
the room AudioWorker RPC. It waits for actual sustained 440 Hz FIFO PCM before
taking its baseline; a `play` state or music hook alone cannot pass that gate.
It checks music ducking, decoded speech, restoration while the same connected
session sends silence, and bounded healthy close with no replay tail. Source
state/progress/queue identity, exact AirPlay selection and owned PID/birth
identities are sampled throughout the onset wait, negotiation and speech
stages. It sets and reads back source volume 20 after output selection.

The real Ubuntu TTS run passed in 32.5 seconds between 08:25:20 and 08:25:52 UTC
on 2026-09-30. Music amplitude ratios were 0.200005 while ducked, 1.000000 after
silence and 1.000000 after close; decoded 880 Hz speech amplitude was 423.0.
First qualifying music was observed 0.986 seconds after control-play
observation; the sustained gate passed at 1.259 seconds. Source progress
advanced from 10 to 8,200 ms, owned process identities stayed unchanged, and
reported FIFO drops remained zero. All seven final cleanup checks passed.

Reports are `/tmp/shiri-v2-airplay-result.json` and
`/tmp/shiri-v2-airplay-tts-result.json`. Private source configs, logs, WAV and
observed PCM remain in mode-0700 directories beneath the dedicated runtime
state directory; generated credentials and speech SDP are not report fields.
The harnesses stop their owned sender and broker, verify an empty manifest,
closed slot and exact host networking baseline, and include failure/cleanup
details in the report. The TTS check also closes its speech session and peer.

The TTS route bypasses the rootless API and broker speech routing: that broker
correctly rejects speech when no output is assigned. Neither harness
proves stock-phone interoperability, native multi-zone grouping, Cast input,
physical-speaker output, API/broker TTS routing or acoustic synchronization.
OwnTone may consume some FIFO writes concurrently, so observed blocks do not
establish full-rate delivery. Continuity observations are control samples at
roughly 100 ms in the TTS harness, not proof about shorter interruptions.

## Authenticated API speech and final local output

`check_airplay_api_tts.py` exercises the actual rootless HTTP API → broker →
room worker → selected OwnTone ALSA output. It requires the same known
`b265...` candidate, empty ownership manifest, Linux root, pinned backends,
`enp0s1` and four free slot-7 endpoints described above, plus the existing
nonroot `shiri` account. Stage `check_airplay_tts.py` alongside it. Stop the
dedicated candidate test services and run only one harness at a time.

Run the entire harness and its children under a transient systemd watchdog:

```sh
sudo systemd-run --wait --pipe --collect --unit=shiri-v2-api-tts-check \
  --property=RuntimeMaxSec=240 --property=TimeoutStopSec=10 \
  --property=KillMode=control-group \
  /opt/shiri-v2/.venv/bin/python /opt/shiri-v2/tests/linux/check_airplay_api_tts.py
```

The watchdog bounds the transient service cgroup, including a potentially
blocked GStreamer shutdown thread that Python cannot forcibly cancel. A
watchdog expiry is a failed run; inspect the exact owned manifest/resources and
recover them through their owning runtime before retrying. It does not prove
driver shutdown latency. Normal success still requires all explicit cleanup
checks, not merely a successful supervisor exit.

The harness seeds seven disabled reservations and one fixed-identity slot-7
room in a disposable database. Its API child runs as `shiri` with no effective
capabilities; anonymous state reads must return 401, then a temporary token
creates an HttpOnly session. Authenticated API calls enable the room, assign
only local ALSA output ID 0, change its volume and admit/close real Opus speech.
Disabled offers, an unknown Nobly binding and wrong-room session close are
rejected. A positive external Nobly connector is not exercised.

The synthetic sender selects only the exact AirPlay 2 receiver by name and
MAC-derived ID. Input uses `hw:Loopback,0,7` → mixer capture on pair 1; OwnTone's
selected local output uses `hw:Loopback,1,7` → final capture on pair 0. The
observer never competes for the mixer FIFO. It records the actual negotiated
PCM rate/format and requires consecutive frame offsets, rejects later
discontinuities or observer losses, and checks 440 Hz music in every complete
post-onset block. Actual final-PCM transition gates replace assumed buffering
delays. API volume 100 → 50 → 100 must follow the pinned cubic software-volume
curve, then 880 Hz speech must duck music to 0.2, restore during same-session
silence, resume audibly and restore again after closing the audible session.
Source/output state, queue identity, progress, volume, selection and owned
PID/birth identities are sampled throughout.

The Ubuntu run passed in 33.4 seconds, 10:07:05–10:07:39 UTC on 2026-09-30.
API UID was 999 with zero effective capabilities. Measured volume-50 music ratio
was 0.124934 (expected 0.125), restored volume-100 ratio was 1.000000, speech
duck ratio was 0.199999, and music ratios after silence/close were 1.000000.
Final capture verified 882,240 stereo S16LE frames at 48 kHz across 919
consecutive buffers; only the initial buffer was discontinuous, maximum
callback gap was 25.5 ms, and observer/FIFO drops were zero. All ten cleanup
checks passed, including disposable room deletion, closed slot and exact host
network baseline restoration.

The private report is `/tmp/shiri-v2-airplay-api-tts-result.json`; source config,
temporary login token, API logs, WAV and observed PCM stay in their private
candidate directories. No credentials or SDP belong in repository artifacts.
Offline regressions in `tests/test_airplay_observation.py` run in the existing
audio-enabled CI job and require no Linux hardware or live app.

This verifies a synthetic AirPlay source and kernel Loopback output. It does
not prove stock-phone interoperability, native multi-zone grouping, Cast input,
physical/Bluetooth speaker behavior, acoustic sync or completed daemon privilege
separation. Control polling cannot exclude transients shorter than its roughly
100 ms interval, and block fits cannot exclude sub-block audio transients.
