# Shiri

Shiri turns each configured zone into an **AirPlay 2 receiver**. Phones use their existing AirPlay controls, without a Shiri phone app. Shiri sends the incoming audio to that zone's assigned speakers, which can use different supported output protocols. TTS targets the same zones, lowering music volume while music keeps playing, then smoothly restoring it. The user deferred Chromecast input on October 1; Chromecast speaker outputs remain supported through OwnTone. The authoritative behavior and release requirements are in [docs/PRODUCT_REQUIREMENTS.md](docs/PRODUCT_REQUIREMENTS.md).

This branch is an **incomplete replacement under validation**. The candidate implements AirPlay input, exact source ownership, OwnTone output, targeted speech and recorded timing calibration. Chromecast input is deferred pending a viable receiver; [receiver feasibility](docs/CAST_INPUT_FEASIBILITY.md) records the evidence. The previous application and live Ubuntu deployment are preserved. Passing software checks does not establish native phone compatibility or synchronization at the final speakers. The review loop, completed checks, and remaining release gates are recorded in [docs/REBUILD.md](docs/REBUILD.md).

## Candidate paths and missing requirements

| Path | Implementation | Practical limit |
| --- | --- | --- |
| AirPlay input | Shairport Sync 5.5.2, one advertised receiver per enabled room | Linux, bridged LAN, working multicast and PTP |
| Room-addressed speech | Authenticated WebRTC audio API, one active producer per room | Nobly is a future external client; it is not installed |
| AirPlay output | OwnTone 29.3 | Device authorization may require setup |
| Google Cast output | OwnTone's Cast implementation | Cross-protocol synchronization is approximate; device support varies |
| Wired/local output | Enrolled physical ALSA device, with opened-device identity checks | Requires a usable Linux audio device; isolated Linux playback validation is in progress |
| Bluetooth output | Exact paired A2DP endpoint through maintained BlueALSA and a descriptor-only worker | Combined route and physical acceptance remain open; pairing, adapter setup and VM USB passthrough are external setup |
| Google Cast input | **Deferred by the user for this release** | Any future receiver needs authentication, stock-phone compatibility and audio/control validation |

One OwnTone instance delivers each zone's mixed program to its selected outputs. Different zones can receive different programs. When a phone selects several Shiri receivers, the patched receiver, mixer and OwnTone input preserve the common presentation timeline instead of starting each relay from its arrival time. The short synthetic Ubuntu grouped-audio check passed through final digital PCM, including speech, source takeover and end-of-stream. Stock-phone grouping, physical-speaker timing and longer playback remain acceptance gates. See [architecture](docs/ARCHITECTURE.md).

OwnTone supports per-output timing offsets from −2000 to +2000 ms. Positive values add delay. Shiri preserves these profiles across deselection and verifies backend readback. Recorded calibration measures speakers within one zone or across grouped zones, retains reviewable results and applies or rolls back a correction with the target zone off. Direct administrative offset changes during playback restart that room's output session because OwnTone applies the offset when a session starts. Speech never invokes that operation. Readback confirms the setting; verification recordings confirm the measured result. See [docs/CALIBRATION.md](docs/CALIBRATION.md).

## Development

Python 3.10 or newer is required. A Mac can run the interface and simulation; physical audio workers run on Linux.

```sh
uv sync --extra audio --extra test
SHIRI_STATE_DIR=/tmp/shiri-dev uv run shiri serve --simulation --port 8080
```

Open `http://127.0.0.1:8080`. Simulation is clearly labelled, exposes fake speakers, and cannot negotiate or play real audio. It disables authentication and is restricted to loopback by the CLI.

```sh
uv run ruff check shiri tests
uv run pytest -q
node --test tests/web/*.test.mjs
uv run python -m playwright install chromium
SHIRI_BROWSER_TESTS=1 node --test tests/web/*.test.mjs
```

Linux GStreamer integration tests additionally require the system GI and audio packages installed by `install/install.sh`:

```sh
SHIRI_LINUX_AUDIO_TESTS=1 .venv/bin/pytest -q tests/test_linux_audio.py
```

The Linux tests use generated PCM and local WebRTC peers. They do not select house speakers or substitute for physical-device testing.

## Ubuntu installation

Use a bridged wired LAN adapter. A VM must permit additional macvlan MAC addresses; NAT or a bridge that blocks them cannot provide room receivers reliably. The broker reports DHCP or gateway failures instead of claiming the room is running.

```sh
sudo SHIRI_INSTALL_PREFIX=/opt/shiri bash install/install.sh --with-backends
sudo SHIRI_INSTALL_PREFIX=/opt/shiri bash deploy/install_services.sh
```

Backend source revisions are pinned in `install/build_backends.sh`; Python runtime dependencies and hashes are pinned in `install/requirements.lock`, generated from `uv.lock`. Installation configures the application without activating it. Inspect `/etc/shiri/shiri.env` and complete the migration and validation gates before replacing an existing deployment.

The installers validate a root-owned executable tree and its parents before privileged use and after producing files. Prefix/ancestor symlinks and writable or foreign-owned files are refused; trusted venv interpreter links remain supported. Service installation requires a stopped API and refuses active SQLite connections or retained WAL/SHM/journal files, preserving them for an operator checkpoint. See [install/README.md](install/README.md) for adoption instructions. A complete fresh-host installation remains a validation gate. The rootless API boundary and pending daemon privilege work are described in [docs/DAEMON_PRIVILEGES.md](docs/DAEMON_PRIVILEGES.md).

```sh
sudo env SHIRI_BINARY_DIR=/opt/shiri LD_LIBRARY_PATH=/opt/shiri/lib /opt/shiri/venv/bin/shiri doctor
sudo systemctl enable --now shiri-runtime shiri-api
```

`doctor` reports command/module availability. The broker additionally checks backend versions/features, matching timing interfaces and audio prerequisites before creating network resources. Neither result certifies native phone interoperability or final-speaker timing.

The API defaults to localhost. Use a local tunnel or an HTTPS reverse proxy for access from another device. The proxy must preserve the original `Host`, including the external port, and forward the original scheme with `X-Forwarded-Proto`; otherwise same-origin checks will reject browser writes. Forwarded scheme/address headers are trusted only from loopback by default; configure `SHIRI_TRUSTED_PROXY_IPS` explicitly for another proxy address. Proxying HTTP control does not relay WebRTC audio: the speech client must also reach the worker's LAN ICE candidates. There is no public STUN/TURN service configured. Admin access uses the token in `/etc/shiri/api-token`; the browser receives an HttpOnly session cookie and does not save the token in local storage.

## Migration and rollback

The importer reads the old `config.json` and never modifies it. Its default is a dry run. Imports are transactional, preserve exact external room IDs, allocate stable new room IDs, and create **disabled** rooms. Unknown speaker types and local outputs without a configured physical device are reported for manual selection.

```sh
sudo -u shiri /opt/shiri/venv/bin/shiri migrate /path/to/old/config.json
sudo -u shiri /opt/shiri/venv/bin/shiri migrate /path/to/old/config.json --apply
```

These commands read `SHIRI_*` settings from their process environment, not automatically from the systemd environment file. Run with the same installation settings when using custom paths. The default database is `/var/lib/shiri/shiri.sqlite3`.

Keep the old services stopped while enabling equivalent migrated rooms. For rollback, disable new rooms, stop `shiri-api` and `shiri-runtime`, confirm owned namespace and DHCP cleanup, then start the preserved old deployment. Stop the API before taking a filesystem backup of the SQLite database and its WAL/SHM companions, or use SQLite's online backup facility. Do not copy only an active database file.

The local legacy baseline is preserved on `main`; audited legacy fixes are on `codex/robust-room-audio` at `a3ef0fc`. The discovered Ubuntu deployment was an older checkout at `0f59284429c568415f2f98e6baacbf8e65683be6` with a modified wrapper. Its source and state were backed up before this rebuild.

## API and operations

The rootless HTTP service owns validated room intent in SQLite. A separate privileged broker owns network namespaces and daemon lifecycles. It accepts bounded local RPC operations; HTTP requests do not execute arbitrary shell commands. Root runtime manifests and generated daemon configs are stored separately from API-writable data.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/state` | Saved rooms, observed runtime, discovery, capabilities |
| `GET /api/v1/local-devices` | Current local audio devices and supported physical bindings |
| `POST /api/v1/local-devices/bind` | Enroll an exact serial, port or platform binding |
| `POST /api/v1/rooms` | Create a disabled room |
| `PATCH /api/v1/rooms/{id}` | Update with `expected_revision` and `changes` |
| `PUT /api/v1/rooms/{id}/speakers` | Assign discovered speaker IDs with a revision |
| `PATCH /api/v1/rooms/{id}/speakers/{speaker}/offset` | Save an offset with a revision |
| `/api/v1/rooms/{id}/calibration` | Recorded timing sessions, analysis, guarded correction and rollback |
| `POST /api/v1/rooms/{id}/speech` | Negotiate or close explicitly identified speech |
| `POST /api/v1/nobly/rooms/{external_id}/speech` | Route by an exact configured Nobly room binding |
| `GET /api/v1/health/live` | HTTP process liveness |
| `GET /api/v1/health/ready` | Cheap broker and enabled-room readiness |
| `GET /api/v1/events` | Bounded durable configuration event history |
| `GET /api/v1/rooms/{id}/diagnostics` | Runtime state and bounded daemon logs |
| `GET /api/openapi.json` | Complete typed HTTP API schema |

Automation clients send `Authorization: Bearer <installation-token>`. A successful setting write means durable intent; `runtime_accepted`, room status, and backend readback describe whether hardware has applied it. Revision conflicts return HTTP 409 without overwriting newer settings. Saved assignments can be removed during an outage. A speaker endpoint is reserved for one room, including disabled rooms, until explicitly unassigned.

Speech offers require a safe explicit `session_id`, a distinct `request_id`, `type: "offer"`, and one sending audio track. Another producer cannot take over the room. Closing the session, a failed connection, a media stall, cancellation, or 30 seconds without audible speech releases it. Silence packets do not hold music ducked. Music returns smoothly after speech; idle mixers stop writing to OwnTone rather than permanently streaming silence.

Inspect services with `journalctl -u shiri-api -u shiri-runtime`. Daemon logs rotate within the root-owned runtime state directory. Recovery retries use bounded backoff and isolate room failures. Cleanup checks namespace identity, interface MAC and alias, and process birth time before releasing DHCP or signalling a process. Never use broad process-name or namespace-prefix kill commands to repair the app.
