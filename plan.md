# Shiri Bridge Architecture Plan

## Architecture Snapshot

- Minimal C++ `shiri-bridge` daemon runs a clean ingest → buffer → fan-out pipeline with clear interfaces per step.
- `shairport-sync` supplies AirPlay 2 audio + timing via a defined IPC surface; start with FIFO PCM + JSON metadata for simplest integration.
- Downstream fan-out begins with AirPlay 1 (`libraop`) clients on Interface B; design extension points for future AirPlay 2 sender.
- Local HTTP API (e.g., `cpp-httplib`) offers status, volume, and TTS hooks once core audio path is proven.

## Phase 0 · Shairport Sync Deep-Dive

1. Build and run `shairport-sync` standalone; capture PCM/timing outputs using built-in pipe backend.
2. Document available metadata (latency, timestamps, volume) and confirm how AirPlay 2 multi-room control appears.
3. Decide IPC contract (named pipes vs. shared memory) and create a small throwaway C++ prototype that reads frames to verify format and throughput.

## Phase 1 · Project Skeleton

1. Initialize fresh repo layout (`/src`, `/include`, `/third_party`, `/config`, `/docs`, `/scripts`).
2. Author minimal `CMakeLists.txt` that builds `shiri-bridge` and pulls `cpp-httplib`, `nlohmann_json`, and wraps installed `shairport-sync` + `libraop` headers.
3. Add coding-standard scaffolds (clang-format, warnings-as-errors) and a smoke test target.

## Phase 2 · Ingest Adapter

1. Implement `ShairportProcess` wrapper to launch/manage `shairport-sync` with our pipe backend config.
2. Develop `PcmFifoReader` that streams PCM/timing into a lock-free ring buffer with bounded size + basic stats.
3. Expose a clean `IngestStream` interface returning frames + monotonic timestamps; include unit tests reading prerecorded data.

## Phase 3 · Core Bridge MVP

1. Implement `VirtualRoom` struct storing buffer, target latency, speaker list; keep types lean and header-only where feasible.
2. Build `RaopClient` using `libraop` for single speaker playback; support connect, send frame, heartbeat, error callbacks.
3. Create `SyncFanout` loop that reads from `IngestStream`, applies fixed latency (e.g., 2s), and pushes frames to all `RaopClient`s with basic drift correction.
4. Verify end-to-end audio locally with one speaker before expanding to multiples; log only essential events.

## Phase 4 · Control Layer Basics

1. Parse `config/config.json` for rooms→speakers mapping and default volumes.
2. Stand up HTTP server with `/api/status`, `/api/volume`, `/api/speak` (speak can stub to log until TTS ready).
3. Ensure API shares state with core via light `BridgeState` singleton or message queue; keep payloads minimal JSON.

## Phase 5 · Deployment & Validation

1. Write `shiri-bridge.service` systemd unit and simple startup script wiring Interface A/B env vars.
2. Document setup flow in `docs/getting-started.md`, including shairport install, config, and network checklist.
3. Add integration smoke test script that plays sample audio through shairport and verifies RAOP speaker playback via loopback capture.

## Later Enhancements Parking Lot

- Replace `libraop` fan-out with native AirPlay 2 sender once available.
- Metrics/observability stack, richer TTS pipeline, web UI.
- Advanced clock discipline and dynamic room membership.