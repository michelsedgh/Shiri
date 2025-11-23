#include <algorithm>
#include <atomic>
#include <chrono>
#include <set>
#include <exception>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>
#include <deque>

#include <sys/ioctl.h>
#include <sys/select.h>
#include <termios.h>
#include <unistd.h>

// --- Libraop Globals Definition ---
extern "C" {
#include "cross_log.h"
#include "cross_net.h"
#include "cross_ssl.h"
}

// These globals are required by libraop/cross_net linkage
extern "C" {
    log_level util_loglevel = lERROR; // Default to ERROR to keep TUI clean
    log_level raop_loglevel = lWARN;
    log_level main_log = lERROR;
    log_level *loglevel = &main_log;
}
// ----------------------------------

#include "Discovery.h"
#include "Shairport.h"
#include "RaopHostage.h"
#include "AppState.h"
#include "Tui.h"

// Global state containers are declared in AppState.h and defined here.
std::map<std::string, SpeakerState> speakerStates;
std::map<std::string, GroupInfo>   groups;
std::mutex                         stateMutex;
std::atomic<uint64_t>              chunkCounter{0};
std::atomic<bool>                  running(true);

// Minimal ANSI color helpers used for early fatal errors on stderr. The TUI
// has its own styling inside Tui.cpp.
const std::string RESET = "\033[0m";
const std::string RED   = "\033[31m";

void groupStreamerLoop(std::string groupName) {
    while (true) {
        std::vector<uint8_t> chunk;
        std::vector<std::pair<std::string, RaopHostage*>> hostages;
        bool runningLocal = true;
        bool isSilenceChunk = false;
        {
            std::lock_guard<std::mutex> lock(stateMutex);
            auto it = groups.find(groupName);
            if (it == groups.end() || !it->second.streamerRunning) {
                break;
            }
            auto& group = it->second;
            auto& queue = group.chunkQueue;

            // Send audio immediately when available - RAOP protocol handles timing
            if (!queue.empty()) {
                chunk = std::move(queue.front());
                queue.pop_front();
                // Log transition from silence to audio
                if (group.consecutiveSilenceChunks > 0) {
                    Tui::AppendRaopLog("Audio resumed after " + std::to_string(group.consecutiveSilenceChunks) + " silence chunks");
                }
                for (const auto& id : group.speakerIds) {
                    auto sit = speakerStates.find(id);
                    if (sit != speakerStates.end() && sit->second.hostage) {
                        hostages.emplace_back(id, sit->second.hostage.get());
                    }
                }
            } else {
                // Generate silence chunk to keep speakers alive during pauses
                chunk.resize(kChunkBytes, 0); // Silent audio (all zeros)
                isSilenceChunk = true;
                runningLocal = group.streamerRunning;
                for (const auto& id : group.speakerIds) {
                    auto sit = speakerStates.find(id);
                    if (sit != speakerStates.end() && sit->second.hostage) {
                        hostages.emplace_back(id, sit->second.hostage.get());
                    }
                }
            }
        }

        if (chunk.empty()) {
            if (!runningLocal) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
            continue;
        }

        bool requeue = false;
        std::string blockedId;
        for (const auto& [id, hostage] : hostages) {
            if (!hostage) continue;
            if (!hostage->isConnected()) {
                Tui::AppendRaopLog("Hostage disconnected before frames ready: " + id);
                blockedId = id;
                requeue = !isSilenceChunk;
                break;
            }
            if (!hostage->waitForFramesReady()) {
                Tui::AppendRaopLog("Hostage not ready yet: " + id);
                blockedId = id;
                requeue = !isSilenceChunk;
                break;
            }
        }

        if (!blockedId.empty()) {
            std::lock_guard<std::mutex> lock(stateMutex);
            auto sit = speakerStates.find(blockedId);
            if (sit != speakerStates.end()) {
                auto& state = sit->second;
                state.notReadyStreak++;
                if (state.notReadyStreak >= 1 && state.hostage) {
                    state.reconnectAttempts++;
                    const auto& speaker = state.info;
                    if (!speaker.ip.empty() && speaker.port > 0) {
                        Tui::AppendRaopLog("Hostage stuck not ready, reconnecting: " + blockedId);
                        state.hostage->disconnect();
                        if (state.hostage->connect()) {
                            Tui::AppendRaopLog("Reconnected hostage after not-ready streak: " + blockedId);
                            state.notReadyStreak = 0;
                        } else {
                            Tui::AppendRaopLog("Failed to reconnect hostage after not-ready streak: " + blockedId);
                        }
                    }
                }
                if (requeue) {
                    auto git = groups.find(groupName);
                    if (git != groups.end()) {
                        git->second.chunkQueue.emplace_front(std::move(chunk));
                    }
                }
            }
            if (requeue) {
                std::this_thread::sleep_for(std::chrono::milliseconds(2));
                continue;
            }
        }

        uint64_t chunkId = ++chunkCounter;

        // Update silence tracking
        {
            std::lock_guard<std::mutex> lock(stateMutex);
            auto it = groups.find(groupName);
            if (it != groups.end()) {
                if (isSilenceChunk) {
                    it->second.consecutiveSilenceChunks++;
                } else {
                    it->second.consecutiveSilenceChunks = 0;
                }
            }
        }

        for (const auto& [id, hostage] : hostages) {
            if (!hostage || !hostage->isConnected()) continue;
            if (!hostage->sendAudioChunk(chunk.data(), chunk.size())) {
                Tui::AppendRaopLog("RAOP send failed: " + id + " - attempting reconnect");
                // Attempt to reconnect the hostage
                std::lock_guard<std::mutex> lock(stateMutex);
                auto sit = speakerStates.find(id);
                if (sit != speakerStates.end() && sit->second.hostage) {
                    const auto& speaker = sit->second.info;
                    if (!speaker.ip.empty() && speaker.port > 0) {
                        sit->second.hostage->disconnect();
                        if (sit->second.hostage->connect()) {
                            Tui::AppendRaopLog("Reconnected hostage: " + id);
                        } else {
                            Tui::AppendRaopLog("Failed to reconnect hostage: " + id);
                        }
                    }
                }
            } else if (!isSilenceChunk && (chunkId <= 10 || chunkId % 500 == 0)) {
                Tui::AppendRaopLog("Chunk #" + std::to_string(chunkId) + " sent to " + id);
            } else if (isSilenceChunk && chunkId % 1000 == 0) {  // Less frequent logging for silence
                Tui::AppendRaopLog("Silence chunk #" + std::to_string(chunkId) + " sent to " + id);
            }
        }

        // Adaptive sleep timing based on silence duration
        if (isSilenceChunk) {
            // During long silence periods, use slightly longer sleep to reduce CPU usage
            // but still maintain connection health
            std::lock_guard<std::mutex> lock(stateMutex);
            auto it = groups.find(groupName);
            if (it != groups.end() && it->second.consecutiveSilenceChunks > 1000) {  // ~1 second of silence
                std::this_thread::sleep_for(std::chrono::milliseconds(2));
            } else {
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
            }
        }
    }
    Tui::AppendRaopLog("Streamer exited for group " + groupName);
}

#include <signal.h>

void signalHandler(int signum) {
    std::cerr << "Caught signal " << signum << ", cleaning up..." << std::endl;
    running = false;
}

int main() {
    // Disable OpenSSL hardware acceleration capability detection on ARM to prevent SIGILL in VM
    // This is a known issue when running Aarch64 code in some virtualized environments
    setenv("OPENSSL_armcap", "0", 1);

    signal(SIGSEGV, signalHandler);
    signal(SIGILL, signalHandler);
    signal(SIGINT, signalHandler);

    // Initialize platform specifics for libraop
    std::cerr << "Initializing platform..." << std::endl;
    netsock_init();

    // Initialize SSL/Crypto subsystem (Required for RAOP to work)
    std::cerr << "Loading SSL libraries..." << std::endl;
    if (!cross_ssl_load()) {
         std::cerr << RED << "Fatal: Failed to load SSL libraries." << RESET << std::endl;
         return 1;
    }
    std::cerr << "Platform initialization complete." << std::endl;

    Discovery discovery;
    discovery.start([](const std::vector<Speaker>& speakers) {
        std::lock_guard<std::mutex> lock(stateMutex);
        std::vector<std::string> seen;
        seen.reserve(speakers.size());
        for (const auto& speaker : speakers) {
            auto& state = speakerStates[speaker.id];
            state.info = speaker;
            state.connected = true;
            seen.push_back(speaker.id);
        }
        // Mark speakers not in current snapshot as offline
        for (auto& [id, state] : speakerStates) {
            if (std::find(seen.begin(), seen.end(), id) == seen.end()) {
                state.connected = false;
                // Disconnect hostage when speaker goes offline
                if (state.hostage) {
                    state.hostage.reset();
                    Tui::AppendRaopLog("Disconnected (offline): " + id);
                }
            }
        }
        Tui::RequestRefresh();
    });

    // Check if discovery actually started
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
    if (!discovery.isRunning()) {
        std::cerr << RED << "Fatal: Discovery failed to start (mDNS init failed)." << RESET << std::endl;
        return 1;
    }

    Tui::SetStatus("Ready.");
    Tui::RequestRefresh();

    // Hand off control to the TUI. This will block until the user quits or
    // a signal sets the global `running` flag to false.
    Tui::Run();

    running = false;
    discovery.stop();

    std::vector<std::thread> streamerJoins;
    {
        std::lock_guard<std::mutex> lock(stateMutex);
        for (auto& [name, group] : groups) {
            group.streamerRunning = false;
            if (group.streamerThread.joinable()) {
                streamerJoins.push_back(std::move(group.streamerThread));
            }
        }
    }
    for (auto& t : streamerJoins) {
        if (t.joinable()) t.join();
    }

    {
        std::lock_guard<std::mutex> lock(stateMutex);
        for (auto& [name, group] : groups) {
            if (group.process) {
                group.process->stop();
            }
        }
        // Release hostages
        for (auto& [id, state] : speakerStates) {
            state.hostage.reset();
        }
    }
    
    // Cleanup platform
    cross_ssl_free();
    netsock_close();

    std::cout << "Goodbye!\n";
    return 0;
}