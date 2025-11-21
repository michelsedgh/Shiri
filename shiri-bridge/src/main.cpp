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

// Log window for debugging RAOP issues
struct LogWindow {
    std::deque<std::string> lines;
    std::mutex mutex;
    
    void add(const std::string& msg) {
        std::lock_guard<std::mutex> lock(mutex);
        lines.push_back(msg);
        if (lines.size() > 5) lines.pop_front(); // Keep last 5 messages
    }
    
    std::vector<std::string> getRecent() {
        std::lock_guard<std::mutex> lock(mutex);
        return std::vector<std::string>(lines.begin(), lines.end());
    }
};

LogWindow raopLog;

struct SpeakerState {
    Speaker info;
    bool connected = false;
    bool reserved = false;
    std::unique_ptr<RaopHostage> hostage;
};

struct GroupInfo {
    std::string name;
    int port = 0;
    std::string parentInterface; // network interface used for this group's AirPlay 2 netns
    std::vector<std::string> speakerIds;
    std::unique_ptr<Shairport> process;
    std::deque<std::vector<uint8_t>> chunkQueue;
    std::vector<uint8_t> pendingBytes;
    std::thread streamerThread;
    bool streamerRunning = false;
    uint64_t consecutiveSilenceChunks = 0;  // Track silence duration for robustness
};
constexpr size_t kAudioBytesPerFrame = 4; // 16-bit stereo PCM
constexpr size_t kFramesPerChunk = 352;   // RAOP default
constexpr size_t kChunkBytes = kAudioBytesPerFrame * kFramesPerChunk;
constexpr size_t kMaxQueuedChunks = 128;       // ~1.0 second of audio buffering (reduced)
constexpr size_t kPrebufferChunks = 32;        // ~0.25 seconds initial buffer (reduced)

std::map<std::string, SpeakerState> speakerStates;
std::map<std::string, GroupInfo> groups;
std::mutex stateMutex;
std::atomic<uint64_t> chunkCounter{0};
void groupStreamerLoop(std::string groupName);

std::string statusMessage;
std::mutex statusMutex;

std::atomic<bool> running(true);
std::atomic<bool> uiDirty(true);

const std::string RESET = "\033[0m";
const std::string BOLD = "\033[1m";
const std::string RED = "\033[31m";
const std::string GREEN = "\033[32m";
const std::string YELLOW = "\033[33m";
const std::string BLUE = "\033[34m";
const std::string CYAN = "\033[36m";

void setStatusMessage(const std::string& message) {
    std::lock_guard<std::mutex> lock(statusMutex);
    statusMessage = message;
    uiDirty = true;
}

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
                    raopLog.add("Audio resumed after " + std::to_string(group.consecutiveSilenceChunks) + " silence chunks");
                }
                for (const auto& id : group.speakerIds) {
                    auto sit = speakerStates.find(id);
                    if (sit != speakerStates.end() && sit->second.hostage && sit->second.hostage->isConnected()) {
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
                    if (sit != speakerStates.end() && sit->second.hostage && sit->second.hostage->isConnected()) {
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
        for (const auto& [id, hostage] : hostages) {
            if (!hostage || !hostage->isConnected()) continue;
            if (!hostage->waitForFramesReady()) {
                raopLog.add("Hostage not ready yet: " + id);
                requeue = true;
                break;
            }
        }

        if (requeue && !isSilenceChunk) {  // Don't requeue silence chunks
            std::lock_guard<std::mutex> lock(stateMutex);
            auto it = groups.find(groupName);
            if (it != groups.end()) {
                it->second.chunkQueue.emplace_front(std::move(chunk));
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
            continue;
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
                raopLog.add("RAOP send failed: " + id + " - attempting reconnect");
                // Attempt to reconnect the hostage
                std::lock_guard<std::mutex> lock(stateMutex);
                auto sit = speakerStates.find(id);
                if (sit != speakerStates.end() && sit->second.hostage) {
                    const auto& speaker = sit->second.info;
                    if (!speaker.ip.empty() && speaker.port > 0) {
                        sit->second.hostage->disconnect();
                        if (sit->second.hostage->connect()) {
                            raopLog.add("Reconnected hostage: " + id);
                        } else {
                            raopLog.add("Failed to reconnect hostage: " + id);
                        }
                    }
                }
            } else if (!isSilenceChunk && (chunkId <= 10 || chunkId % 500 == 0)) {
                raopLog.add("Chunk #" + std::to_string(chunkId) + " sent to " + id);
            } else if (isSilenceChunk && chunkId % 1000 == 0) {  // Less frequent logging for silence
                raopLog.add("Silence chunk #" + std::to_string(chunkId) + " sent to " + id);
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
    raopLog.add("Streamer exited for group " + groupName);
}

void getTerminalSize(int& rows, int& cols) {
    struct winsize w { };
    if (ioctl(STDOUT_FILENO, TIOCGWINSZ, &w) == -1) {
        // Fallback if ioctl fails
        const char* env_cols = getenv("COLUMNS");
        const char* env_lines = getenv("LINES");
        cols = env_cols ? std::atoi(env_cols) : 80;
        rows = env_lines ? std::atoi(env_lines) : 24;
    } else {
        rows = w.ws_row;
        cols = w.ws_col;
    }
}

void clearScreen() {
    std::cout << "\033[2J\033[1;1H";
}

void setCursor(int row, int col) {
    std::cout << "\033[" << row << ';' << col << 'H';
}

int kbhit() {
    timeval tv { 0L, 0L };
    fd_set fds;
    FD_ZERO(&fds);
    FD_SET(STDIN_FILENO, &fds);
    return select(STDIN_FILENO + 1, &fds, nullptr, nullptr, &tv);
}

int getch() {
    unsigned char c;
    if (read(STDIN_FILENO, &c, sizeof(c)) < 0) {
        return -1;
    }
    return c;
}

void setNonCanonicalMode(bool enable) {
    static termios oldt;
    static bool saved = false;
    if (enable) {
        if (!saved) {
            tcgetattr(STDIN_FILENO, &oldt);
            saved = true;
        }
        termios newt = oldt;
        newt.c_lflag &= ~(ICANON | ECHO);
        tcsetattr(STDIN_FILENO, TCSANOW, &newt);
    } else if (saved) {
        tcsetattr(STDIN_FILENO, TCSANOW, &oldt);
    }
}

void drawBox(int row, int col, int width, int height, const std::string& title) {
    if (width < 2 || height < 2) return;
    setCursor(row, col);
    std::cout << '+';
    for (int i = 0; i < width - 2; ++i) std::cout << '-';
    std::cout << '+';
    if (!title.empty()) {
        setCursor(row, col + 2);
        std::cout << ' ' << BOLD << title << RESET << ' ';
    }
    for (int i = 1; i < height - 1; ++i) {
        setCursor(row + i, col);
        std::cout << '|';
        setCursor(row + i, col + width - 1);
        std::cout << '|';
    }
    setCursor(row + height - 1, col);
    std::cout << '+';
    for (int i = 0; i < width - 2; ++i) std::cout << '-';
    std::cout << '+';
}

constexpr int kBaseGroupPort = 6000;
constexpr int kMaxGroupPort = 20000;

int allocatePortLocked() {
    std::set<int> used;
    for (const auto& [name, group] : groups) {
        used.insert(group.port);
    }
    for (int port = kBaseGroupPort; port < kMaxGroupPort; ++port) {
        if (!used.count(port)) {
            return port;
        }
    }
    return -1;
}

struct GroupSnapshot {
    std::string name;
    int port = 0;
    bool healthy = false;
    uint64_t bytesReceived = 0;
    uint64_t lastChunkBytes = 0;
    int64_t lastChunkAgeMs = -1;
    std::vector<std::pair<std::string, bool>> members; // name, connected
};

struct SpeakerSnapshot {
    std::string name;
    std::string ip;
    int port = 0;
    bool connected = false;
    bool reserved = false;
    bool hostage = false;
};

std::string formatBytes(uint64_t bytes) {
    const char* units[] = {"B", "KB", "MB", "GB", "TB"};
    constexpr size_t unitCount = sizeof(units) / sizeof(units[0]);
    size_t idx = 0;
    double value = static_cast<double>(bytes);
    while (value >= 1024.0 && idx < unitCount - 1) {
        value /= 1024.0;
        ++idx;
    }
    std::ostringstream oss;
    oss << std::fixed << std::setprecision(value >= 10.0 ? 0 : 1) << value << ' ' << units[idx];
    return oss.str();
}

std::string formatAge(int64_t ms) {
    if (ms < 0) return "no data yet";
    if (ms < 1000) return "<1s ago";
    if (ms < 60'000) {
        return std::to_string(ms / 1000) + "s ago";
    }
    return std::to_string(ms / 60'000) + "m ago";
}

void renderUI() {
    if (!uiDirty.load()) {
        return;
    }
    uiDirty = false;

    int rows, cols;
    getTerminalSize(rows, cols);
    if (rows < 24 || cols < 80) {
        clearScreen();
        setCursor(1, 1);
        std::cout << "Terminal too small (need at least 80x24)." << std::flush;
        return;
    }

    std::vector<GroupSnapshot> groupData;
    std::vector<SpeakerSnapshot> speakerData;
    auto recentLogs = raopLog.getRecent();
    {
        std::lock_guard<std::mutex> lock(stateMutex);
        for (const auto& [name, group] : groups) {
            GroupSnapshot snap;
            snap.name = name;
            snap.port = group.port;
            bool healthy = true;
            for (const auto& id : group.speakerIds) {
                auto it = speakerStates.find(id);
                bool connected = (it != speakerStates.end() && it->second.connected);
                std::string display = (it != speakerStates.end() ? it->second.info.name : id);
                snap.members.push_back({display, connected});
                if (!connected) healthy = false;
            }
            snap.healthy = healthy;
            if (group.process) {
                snap.bytesReceived = group.process->bytesReceived();
                snap.lastChunkBytes = group.process->lastChunkBytes();
                snap.lastChunkAgeMs = group.process->millisSinceLastChunk();
            }
            groupData.push_back(std::move(snap));
        }
        for (const auto& [id, state] : speakerStates) {
            SpeakerSnapshot snap;
            snap.name = state.info.name.empty() ? id : state.info.name;
            snap.ip = state.info.ip;
            snap.port = state.info.port;
            snap.connected = state.connected;
            snap.reserved = state.reserved;
            snap.hostage = (state.hostage != nullptr && state.hostage->isConnected());
            speakerData.push_back(std::move(snap));
        }
    }

    std::string statusCopy;
    {
        std::lock_guard<std::mutex> lock(statusMutex);
        statusCopy = statusMessage;
    }

    clearScreen();
    setCursor(1, 1);
    std::cout << BOLD << CYAN << "Shiri Bridge" << RESET << "  ·  Multi-Room AirPlay Controller";
    setCursor(2, 1);
    for (int i = 0; i < cols; ++i) std::cout << '=';

    int panelWidth = cols - 4;
    int groupPanelHeight = (rows - 13) / 2; // Reserve space for log
    int speakerPanelHeight = rows - 13 - groupPanelHeight;
    int logPanelHeight = 5;

    drawBox(4, 2, panelWidth, groupPanelHeight, "Groups");
    int groupY = 5;
    if (groupData.empty()) {
        setCursor(groupY, 4);
        std::cout << YELLOW << "No groups defined." << RESET;
    } else {
        for (const auto& grp : groupData) {
            if (groupY >= 4 + groupPanelHeight - 1) break;
            setCursor(groupY++, 4);
            std::string badge = grp.healthy ? (GREEN + "[ONLINE]" + RESET)
                                             : (YELLOW + "[DEGRADED]" + RESET);
            std::cout << badge << ' ' << grp.name << "  (port " << grp.port << ')';
            if (grp.bytesReceived > 0) {
                std::cout << "  ·  " << formatBytes(grp.bytesReceived) << " received";
                if (grp.lastChunkBytes > 0) {
                    std::cout << " (last " << formatBytes(grp.lastChunkBytes)
                              << ", " << formatAge(grp.lastChunkAgeMs) << ')';
                }
            } else {
                std::cout << "  ·  waiting for audio…";
            }
            for (const auto& member : grp.members) {
                if (groupY >= 4 + groupPanelHeight - 1) break;
                setCursor(groupY++, 6);
                std::cout << (member.second ? GREEN : RED)
                          << (member.second ? "* " : "x ")
                          << RESET << member.first;
            }
            groupY++;
        }
    }

    int speakerPanelTop = 4 + groupPanelHeight + 1;
    drawBox(speakerPanelTop, 2, panelWidth, speakerPanelHeight, "Speakers");
    int speakerY = speakerPanelTop + 1;
    if (speakerData.empty()) {
        setCursor(speakerY, 4);
        std::cout << YELLOW << "Discovering speakers..." << RESET;
    } else {
        for (const auto& sp : speakerData) {
            if (speakerY >= speakerPanelTop + speakerPanelHeight - 1) break;
            setCursor(speakerY++, 4);
            std::string badge = sp.connected ? (GREEN + "[ON]" + RESET)
                                             : (RED + "[OFF]" + RESET);
            std::cout << badge << ' ' << sp.name;
            if (sp.hostage) {
                std::cout << RED << " [HOSTAGE]" << RESET;
            }
            setCursor(speakerY++, 6);
            std::cout << sp.ip << ':' << sp.port
                      << (sp.reserved ? "  (locked)" : "  (free)");
            speakerY++;
        }
    }

    int logPanelTop = speakerPanelTop + speakerPanelHeight + 1;
    drawBox(logPanelTop, 2, panelWidth, logPanelHeight, "RAOP Logs");
    int logY = logPanelTop + 1;
    for (size_t i = 0; i < recentLogs.size() && logY < logPanelTop + logPanelHeight; ++i) {
        setCursor(logY++, 4);
        std::cout << recentLogs[i];
    }

    setCursor(rows - 3, 1);
    for (int i = 0; i < cols; ++i) std::cout << '=';
    setCursor(rows - 2, 2);
    std::cout << BOLD << "Controls:" << RESET
              << "  [C]reate group  [D]elete group  [Q]uit";
    setCursor(rows - 1, 2);
    std::cout << (statusCopy.empty() ? "Ready." : statusCopy);
    std::cout << std::flush;
}

struct SelectableSpeaker {
    int index = 0;
    std::string id;
    std::string name;
    std::string ip;
};

bool createGroupFlow() {
    setNonCanonicalMode(false);
    int rows, cols;
    getTerminalSize(rows, cols);

    setCursor(rows - 4, 1);
    std::cout << "\033[J";
    std::cout << "Enter new group name: " << std::flush;
    std::string name;
    std::getline(std::cin, name);
    if (name.empty()) {
        setStatusMessage("Group creation cancelled.");
        setNonCanonicalMode(true);
        return false;
    }

    std::vector<SelectableSpeaker> available;
    {
        std::lock_guard<std::mutex> lock(stateMutex);
        if (groups.count(name)) {
            setStatusMessage("Group already exists.");
            setNonCanonicalMode(true);
            return false;
        }
        int idx = 1;
        for (const auto& [id, state] : speakerStates) {
            if (state.connected && !state.reserved) {
                SelectableSpeaker option;
                option.index = idx++;
                option.id = id;
                option.name = state.info.name.empty() ? id : state.info.name;
                option.ip = state.info.ip;
                available.push_back(std::move(option));
            }
        }
    }

    if (available.empty()) {
        setStatusMessage("No available speakers to assign.");
        setNonCanonicalMode(true);
        return false;
    }

    std::cout << "Select speakers by number (comma separated):\n";
    for (const auto& entry : available) {
        std::cout << "  " << entry.index << ") " << entry.name
                  << "  [" << entry.ip << "]" << std::endl;
    }
    std::cout << "> " << std::flush;
    std::string selectionLine;
    std::getline(std::cin, selectionLine);

    std::vector<std::string> chosenIds;
    if (!selectionLine.empty()) {
        std::istringstream iss(selectionLine);
        std::string token;
        while (std::getline(iss, token, ',')) {
            token.erase(std::remove_if(token.begin(), token.end(), ::isspace), token.end());
            if (token.empty()) continue;
            try {
                int idx = std::stoi(token);
                auto it = std::find_if(available.begin(), available.end(),
                                       [idx](const SelectableSpeaker& entry) { return entry.index == idx; });
                if (it != available.end()) {
                    chosenIds.push_back(it->id);
                }
            } catch (const std::exception&) {
                // ignore invalid token
            }
        }
    }

    if (chosenIds.empty()) {
        setStatusMessage("No speakers selected.");
        setNonCanonicalMode(true);
        return false;
    }

    // Step 3: Choose parent network interface for this group's AirPlay 2 instance
    std::vector<std::string> interfaces;
    {
        FILE* fp = popen("ip -o link show | awk -F': ' '($2!=\"lo\") {print $2}'", "r");
        if (fp) {
            char buf[256];
            while (fgets(buf, sizeof(buf), fp)) {
                std::string name(buf);
                // strip trailing newline
                if (!name.empty() && name.back() == '\n') name.pop_back();
                if (!name.empty()) interfaces.push_back(name);
            }
            pclose(fp);
        }
    }

    if (interfaces.empty()) {
        setStatusMessage("No network interfaces available for AirPlay 2.");
        setNonCanonicalMode(true);
        return false;
    }

    std::cout << "Select parent network interface for AirPlay 2 instance:\n";
    for (size_t i = 0; i < interfaces.size(); ++i) {
        std::cout << "  [" << i << "] " << interfaces[i] << "\n";
    }
    std::cout << "> " << std::flush;

    std::string ifaceChoiceLine;
    std::getline(std::cin, ifaceChoiceLine);
    if (ifaceChoiceLine.empty()) {
        setStatusMessage("No interface selected.");
        setNonCanonicalMode(true);
        return false;
    }
    size_t ifaceIndex = static_cast<size_t>(-1);
    try {
        ifaceIndex = static_cast<size_t>(std::stoul(ifaceChoiceLine));
    } catch (const std::exception&) {
        ifaceIndex = static_cast<size_t>(-1);
    }
    if (ifaceIndex >= interfaces.size()) {
        setStatusMessage("Invalid interface selection.");
        setNonCanonicalMode(true);
        return false;
    }

    std::string parentInterface = interfaces[ifaceIndex];

    {
        std::lock_guard<std::mutex> lock(stateMutex);
        GroupInfo info;
        info.name = name;
        int port = allocatePortLocked();
        if (port < 0) {
            setStatusMessage("No free ports available.");
            setNonCanonicalMode(true);
            return false;
        }
        info.port = port;
        info.parentInterface = parentInterface;
        info.speakerIds = chosenIds;
        
        // Connect hostages for speakers in this group
        for (const auto& id : chosenIds) {
            auto it = speakerStates.find(id);
            if (it != speakerStates.end() && !it->second.hostage) {
                const auto& speaker = it->second.info;
                if (!speaker.ip.empty() && speaker.ip != "0.0.0.0" && speaker.port > 0) {
                    it->second.hostage = std::make_unique<RaopHostage>(
                        speaker.ip, speaker.port, speaker.id, speaker.et, speaker.requiresAuth);
                    if (it->second.hostage->connect()) {
                        raopLog.add("Connected: " + speaker.id + " (group: " + name + ")");
                    } else {
                        raopLog.add("Failed to connect: " + speaker.id + " (group: " + name + ")");
                    }
                }
            }
        }

        auto process = std::make_unique<Shairport>(name, info.port, info.parentInterface);
        process->setCallback([groupName = name](const uint8_t* data, size_t size) {
            if (size == 0) return;
            std::lock_guard<std::mutex> lock(stateMutex);
            auto groupIt = groups.find(groupName);
            if (groupIt == groups.end()) return;
            auto& pending = groupIt->second.pendingBytes;
            auto& queue = groupIt->second.chunkQueue;
            pending.insert(pending.end(), data, data + size);
            while (pending.size() >= kChunkBytes) {
                std::vector<uint8_t> chunk(kChunkBytes);
                std::copy_n(pending.begin(), kChunkBytes, chunk.begin());
                queue.emplace_back(std::move(chunk));
                pending.erase(pending.begin(), pending.begin() + kChunkBytes);
                if (queue.size() > kMaxQueuedChunks) {
                    queue.pop_front();
                }
            }
            // Reset silence counter when we receive real audio
            groupIt->second.consecutiveSilenceChunks = 0;
        });

        process->start();
        info.process = std::move(process);
        info.streamerRunning = true;
        groups[name] = std::move(info);
        groups[name].streamerThread = std::thread(groupStreamerLoop, name);
        for (const auto& id : chosenIds) {
            speakerStates[id].reserved = true;
        }
    }

    setStatusMessage("Group '" + name + "' created.");
    setNonCanonicalMode(true);
    return true;
}

void deleteGroupFlow() {
    setNonCanonicalMode(false);
    int rows, cols;
    getTerminalSize(rows, cols);
    setCursor(rows - 4, 1);
    std::cout << "\033[J";
    std::cout << "Enter group name to delete: " << std::flush;
    std::string name;
    std::getline(std::cin, name);
    if (name.empty()) {
        setStatusMessage("Deletion cancelled.");
        setNonCanonicalMode(true);
        return;
    }

    std::thread streamer;
    std::unique_ptr<Shairport> processToStop;
    std::vector<std::string> speakers;
    {
        std::lock_guard<std::mutex> lock(stateMutex);
        auto it = groups.find(name);
        if (it == groups.end()) {
            setStatusMessage("Group not found.");
            setNonCanonicalMode(true);
            return;
        }
        it->second.streamerRunning = false;
        if (it->second.streamerThread.joinable()) {
            streamer = std::move(it->second.streamerThread);
        }
        if (it->second.process) {
            processToStop = std::move(it->second.process);
        }
        speakers = it->second.speakerIds;
    }

    if (streamer.joinable()) {
        streamer.join();
    }
    if (processToStop) {
        processToStop->stop();
    }

    {
        std::lock_guard<std::mutex> lock(stateMutex);
        auto it = groups.find(name);
        if (it != groups.end()) {
            for (const auto& id : speakers) {
                auto sit = speakerStates.find(id);
                if (sit != speakerStates.end()) {
                    sit->second.reserved = false;
                    if (sit->second.hostage) {
                        sit->second.hostage.reset();
                        raopLog.add("Disconnected (group deleted): " + id);
                    }
                }
            }
            groups.erase(it);
        }
    }

    setStatusMessage("Group '" + name + "' deleted.");
    setNonCanonicalMode(true);
}

void inputLoop() {
    int pulseCounter = 0;
    while (running) {
        if (kbhit() > 0) {
            int ch = getch();
            if (ch == 'q' || ch == 'Q') {
                running = false;
                break;
            }
            if (ch == 'c' || ch == 'C') {
                createGroupFlow();
            }
            if (ch == 'd' || ch == 'D') {
                deleteGroupFlow();
            }
            uiDirty = true;
        }
        renderUI();
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        
        // Pulse hostages with adaptive frequency based on silence duration
        pulseCounter++;
        bool shouldPulse = false;
        {
            std::lock_guard<std::mutex> lock(stateMutex);
            // Check if any group has been silent for a while
            bool longSilence = false;
            for (const auto& [name, group] : groups) {
                if (group.consecutiveSilenceChunks > 500) {  // ~0.5 seconds of silence
                    longSilence = true;
                    break;
                }
            }
            // Pulse every 3 seconds normally, every 1 second during long silence
            shouldPulse = pulseCounter >= (longSilence ? 10 : 30);
        }

        if (shouldPulse) {
            pulseCounter = 0;
            std::lock_guard<std::mutex> lock(stateMutex);
            for (auto& [id, state] : speakerStates) {
                if (state.hostage) {
                    state.hostage->pulse();
                    uiDirty = true; // Update UI to reflect potential connection status changes
                }
            }
        }
    }
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

    setNonCanonicalMode(true);
    std::cout << "\033[?25l"; // hide cursor

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
                    raopLog.add("Disconnected (offline): " + id);
                }
            }
        }
        uiDirty = true;
    });

    // Check if discovery actually started
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
    if (!discovery.isRunning()) {
        setNonCanonicalMode(false);
        std::cout << "\033[?25h";
        std::cerr << RED << "Fatal: Discovery failed to start (mDNS init failed)." << RESET << std::endl;
        return 1;
    }

    setStatusMessage("Ready.");
    uiDirty = true;

    inputLoop();

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

    setNonCanonicalMode(false);
    std::cout << "\033[?25h"; // show cursor
    clearScreen();
    std::cout << "Goodbye!\n";
    return 0;
}