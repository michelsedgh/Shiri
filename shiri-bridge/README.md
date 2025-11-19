# Shiri Bridge

Minimal control-plane daemon that wraps `shairport-sync` for AirPlay ingest and fans audio toward RAOP clients.

## Linux prerequisites (tested on Ubuntu 22.04/24.04)

```
sudo apt update && sudo apt install \
    build-essential cmake ninja-build autoconf automake libtool pkg-config \
    libpopt-dev libconfig-dev libdaemon-dev libssl-dev
```

If you prefer another distribution, install the equivalent development packages.

## Build `shairport-sync`

```
cd /mnt/macos/Shiri/shiri-bridge/scripts
./build_shairport.sh
```

This populates `third_party/shairport-sync/shairport-sync`, which the bridge launches for each group.

## Build the bridge

```
cd /mnt/macos/Shiri/shiri-bridge
cmake -S . -B build -G Ninja
cmake --build build
```

The resulting binary lives at `build/shiri-bridge`.

## Run

```
./build/shiri-bridge
```

- The UI is ncurses-like; press `c` to create speaker groups, `d` to delete, `q` to quit.
- The process spawns one `shairport-sync` instance per group, binding ports starting at `6000`.
- Ensure multicast/mDNS is allowed on your VM interface so RAOP speakers can be discovered.

## Troubleshooting

- If the build script complains about missing tools, install the packages it lists and rerun.
- When running inside a VM, allow inbound UDP 5353 (mDNS) and the playback ports (6000-20000 by default).
- Use `SHIRI_ENABLE_SANITIZERS=ON` when configuring CMake to enable ASan/UBSan for debugging.


