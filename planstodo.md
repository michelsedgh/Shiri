### Notes — Current Plan and Decisions (no implementation yet)

- **Goal**
  - Single AirPlay target (e.g., “Living Room”) that users select.
  - Server fans out the audio to many speakers across brands.
  - Priority: tight sync across all outputs.

- **Scope (AirPlay-only)**
  - Ignore UPnP/Chromecast for now.
  - Focus on one aggregate AirPlay endpoint → multi-speaker AirPlay fan-out.

- **Protocol realities**
  - **AirPlay 2**: proprietary; reliable open-source sender not production-ready.
  - **AirPlay 1 (RAOP)**: feasible to implement for multi-destination sending.
  - **HomePod**: effectively AP2-only as receiver; exclude from v1 aggregate.
  - Result: v1 supports RAOP-capable receivers; AP2-only devices are deferred.

- **What’s wrong in current repo (high-level)**
  - Docker-based AirPlay endpoint on macOS is brittle for Bonjour/mDNS and dual-NIC binding.
  - App browses for AirPlay devices but doesn’t actually route to speakers as designed.
  - `AudioPipelineManager` uses AVAudioEngine (local playback), not AirPlay fan-out.
  - Multiple instances of `AudioPipelineManager` → state drift.
  - Incomplete stop/start paths; AirPlay name mismatch; placeholders in UI.

- **Decisions**
  - Drop Docker. Run native processes on macOS for clean mDNS and interface binding.
  - Do not use AirConnect (per-device endpoints, no single aggregated sync).
  - Build our own: one AirPlay receiver + multi-destination AirPlay (RAOP) sender with sync.

- **Target architecture**
  - `AirplayReceiverManager`: supervises a single receiver process (e.g., `shairport-sync`) that advertises the aggregate endpoint name and outputs decoded PCM to a FIFO.
  - `RaopFanoutManager`: our core; reads PCM and sends to many RAOP receivers with:
    - Per-destination encryption/handshake (RAOP).
    - Master clock, per-receiver jitter buffers.
    - Continuous drift correction via tiny resampling to keep phase lock.
    - Optional per-device trim in ms for edge alignment.
  - `AirplayDiscoveryManager`: Bonjour discovery of `_raop._tcp`/`_airplay._tcp` devices; build groups from discovered receivers.
  - `GroupConfig` (replaces `BridgeConfig` semantics): `airplayName`, `bindIP`, `targetDeviceIDs`, `latency/trim`, codec settings.
  - UI: create/edit groups, pick interface to bind, select target receivers, show in-sync status.

- **Sync strategy**
  - Clock master based on receiver ingest timestamps.
  - Per-destination buffer to absorb jitter; resample to correct long-term drift.
  - Aim for a few ms alignment; user-adjustable fine trims.

- **Networking**
  - Bind the receiver’s mDNS advertisement to the client-facing NIC (`bindIP`) so phones see the single endpoint on the right network.
  - No reliance on container host networking.

- **Risks / constraints**
  - AP2-only devices (HomePod, newer speakers) cannot join v1 aggregate.
  - AirPlay 2 sender support is a future milestone; complex and proprietary.
  - Network variability can affect startup time; buffers mitigate steady-state.

- **Future milestones (post-v1)**
  - Add AP2 sender path when a stable implementation is available.
  - Optional: per-device metadata/volume control mapping.
  - Optional: persistent per-device latency profiles.

- **Immediate next steps (when we start)**
  - Replace Docker/AVAudioEngine with the three managers above.
  - Update models/views for `GroupConfig` and device selection.
  - Implement the RAOP fan-out core loop with basic resampling and buffering.
  - Expose minimal UI to create “Living Room”, pick receivers, start/stop.



  ### Confirmed design (your intent)
- **Per-room endpoints**: Run one AirPlay receiver per room (e.g., “Living Room”, “Kitchen”, “Bedroom”). Each is its own shairport(-sync) instance, ideally containerized, each with its own IP and AirPlay name.
- **Per-room fan-out**: For each room, your app takes that room’s PCM stream and re-sends it to that room’s selected speakers (many devices, mixed brands), keeping them tightly in sync.
- **UI behavior**: In the app, you create rooms; pick which discovered AirPlay receivers belong to each room; start/stop that room. On iPhone, you AirPlay to “Bedroom” and only the Bedroom group plays—synchronized.

### How to realize it (short notes)
- **Containers**
  - Prefer macOS 26 Containerization (per-container IP). Bind each shairport to the client-facing NIC of the network where phones live. Use unique `-a <name>` per room.
  - Inside each container, run shairport-sync with `-o pipe` (and `-M` if you want metadata) writing to a per-room FIFO mounted from the host (e.g., `/tmp/rooms/bedroom/audio`).
  - If you insist on AP2 for shairport: you’ll also need NQPTP in the container and unique IPs per instance. AP2 multi-instance on the same IP confuses clients—unique IPs fix that.

- **Per-room fan-out engine**
  - Read PCM from the room’s FIFO.
  - For each selected speaker (AirPlay receivers you discover via Bonjour), open a RAOP connection, handle crypto, ALAC/RTP framing, and send.
  - Keep a master clock and per-destination jitter buffers; apply tiny resampling to remove drift so all speakers stay locked within a few ms.
  - Optional per-device trim (±ms) to hand-correct stubborn devices.

- **Discovery and selection**
  - Discover targets via Bonjour (`_raop._tcp`, `_airplay._tcp`).
  - UI lets you assign devices to each room and persist that mapping.

- **Networking**
  - One advertised AirPlay endpoint per room, bound to the correct interface/IP, so the phone sees “Living Room”, “Kitchen”, “Bedroom”.
  - Dual-network Mac is fine; bind each container’s shairport to the client network. Fan-out can send to devices on either network (route allowed).

- **Limitations to note**
  - AP2-only devices (e.g., HomePods) can’t join the RAOP fan-out path today; keep them out of v1 groups or handle later when an AP2 sender is viable.
  - Multiple shairport containers give you multiple endpoints (as desired) but do not provide sync by themselves—that’s your fan-out engine’s job.

### Minimal data model and app pieces
- **`GroupConfig` (per room)**: `id`, `airplayName`, `bindIP`, `containerName/processId`, `[targetDeviceIDs]`, `latency/trim`.
- **Managers**:
  - `AirplayReceiverManager` (start/stop per-room shairport container; watch FIFO).
  - `FanoutManager` (one per room; multi-RAOP sender with sync).
  - `AirplayDiscoveryManager` (Bonjour; device list for assignment).
- **UI**
  - Create/edit room; pick bind IP; pick target speakers; Start/Stop; show “in sync/adjusting”.

If you want, I can turn these notes into a concrete TODO list and wire it to your `planstodo.md` without implementing code yet.