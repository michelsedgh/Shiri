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

#include <sys/ioctl.h>
#include <sys/select.h>
#include <termios.h>
#include <unistd.h>

#include "Discovery.h"
#include "Shairport.h"

struct SpeakerState {
    Speaker info;
    bool connected = false;
    bool reserved = false;
};

struct GroupInfo {
    std::string name;
    int port = 0;
    std::vector<std::string> speakerIds;
    std::unique_ptr<Shairport> process;
};

std::map<std::string, SpeakerState> speakerStates;
std::map<std::string, GroupInfo> groups;
std::mutex stateMutex;

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

void getTerminalSize(int& rows, int& cols) {
    struct winsize w { };
    ioctl(STDOUT_FILENO, TIOCGWINSZ, &w);
    rows = w.ws_row;
    cols = w.ws_col;
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
    int groupPanelHeight = (rows - 10) / 2;
    int speakerPanelHeight = rows - 10 - groupPanelHeight;

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
            setCursor(speakerY++, 6);
            std::cout << sp.ip << ':' << sp.port
                      << (sp.reserved ? "  (locked)" : "  (free)");
            speakerY++;
        }
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
        info.speakerIds = chosenIds;
        info.process = std::make_unique<Shairport>(name, info.port);
        info.process->start();
        groups[name] = std::move(info);
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

    {
        std::lock_guard<std::mutex> lock(stateMutex);
        auto it = groups.find(name);
        if (it == groups.end()) {
            setStatusMessage("Group not found.");
            setNonCanonicalMode(true);
            return;
        }
        if (it->second.process) {
            it->second.process->stop();
        }
        for (const auto& id : it->second.speakerIds) {
            auto sit = speakerStates.find(id);
            if (sit != speakerStates.end()) {
                sit->second.reserved = false;
            }
        }
        groups.erase(it);
    }

    setStatusMessage("Group '" + name + "' deleted.");
    setNonCanonicalMode(true);
}

void inputLoop() {
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
    }
}

int main() {
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
        // Mark speakers not in current snapshot as offline (they remain reserved if locked)
        for (auto& [id, state] : speakerStates) {
            if (std::find(seen.begin(), seen.end(), id) == seen.end()) {
                state.connected = false;
            }
        }
        uiDirty = true;
    });

    setStatusMessage("Ready.");
    uiDirty = true;

    inputLoop();

    running = false;
    discovery.stop();

    {
        std::lock_guard<std::mutex> lock(stateMutex);
        for (auto& [name, group] : groups) {
            if (group.process) {
                group.process->stop();
            }
        }
    }

    setNonCanonicalMode(false);
    std::cout << "\033[?25h"; // show cursor
    clearScreen();
    std::cout << "Goodbye!\n";
    return 0;
}
