
#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "Discovery.h"
#include "RaopHostage.h"
#include "Shairport.h"

// Core shared state for Shiri Bridge. This header exposes the model types and
// extern declarations so that non-core modules (like the TUI) can inspect and
// present the current state without duplicating definitions.

struct SpeakerState {
    Speaker info;
    bool connected = false;
    bool reserved = false;
    std::unique_ptr<RaopHostage> hostage;
    std::uint32_t notReadyStreak = 0;
    std::uint32_t reconnectAttempts = 0;
};

struct GroupInfo {
    std::string name;
    int port = 0;
    std::string parentInterface;               // Network interface for this group's AirPlay 2 namespace
    std::vector<std::string> speakerIds;       // IDs of member speakers
    std::unique_ptr<Shairport> process;        // Shairport process feeding PCM into this group
    std::deque<std::vector<uint8_t>> chunkQueue;
    std::vector<uint8_t> pendingBytes;         // Partial PCM bytes waiting to form full chunks
    std::thread streamerThread;                // Thread that pushes PCM to RAOP hostages
    bool streamerRunning = false;
    uint64_t consecutiveSilenceChunks = 0;     // How many consecutive silence chunks we have sent
};

// Audio pipeline configuration shared between the shairport callback and the
// RAOP streaming thread.
constexpr std::size_t kAudioBytesPerFrame = 4;        // 16-bit stereo PCM
constexpr std::size_t kFramesPerChunk     = 352;      // RAOP default
constexpr std::size_t kChunkBytes         = kAudioBytesPerFrame * kFramesPerChunk;
constexpr std::size_t kMaxQueuedChunks    = 16;       // ~0.14 seconds of headroom

// Global state containers. Defined in main.cpp and used by both the audio
// pipeline and the TUI.
extern std::map<std::string, SpeakerState> speakerStates;
extern std::map<std::string, GroupInfo>   groups;
extern std::mutex                         stateMutex;
extern std::atomic<uint64_t>              chunkCounter;
extern std::atomic<bool>                  running;

// RAOP group streaming loop, implemented in main.cpp and invoked when a new
// group is created.
void groupStreamerLoop(std::string groupName);

