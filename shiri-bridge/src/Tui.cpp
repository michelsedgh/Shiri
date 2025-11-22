#include "Tui.h"
#include "AppState.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstddef>
#include <cstdio>
#include <cstring>
#include <deque>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <set>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include <sys/ioctl.h>
#include <sys/select.h>
#include <termios.h>
#include <unistd.h>

extern "C" {
#include "../../libraop/crosstools/src/cross_log.h"
}

namespace {

// Basic ANSI styling helpers local to the UI implementation.
const std::string RESET   = "\033[0m";
const std::string BOLD    = "\033[1m";
const std::string RED     = "\033[31m";
const std::string GREEN   = "\033[32m";
const std::string YELLOW  = "\033[33m";
const std::string BLUE    = "\033[34m";
const std::string CYAN    = "\033[36m";
const std::string REVERSE = "\033[7m";

// Simple in-memory ring buffer for log text.
struct LogWindow {
    std::deque<std::string> lines;
    std::mutex mutex;
    std::size_t capacity;

    explicit LogWindow(std::size_t cap) : capacity(cap) {}

    void add(const std::string& msg) {
        std::lock_guard<std::mutex> lock(mutex);
        lines.push_back(msg);
        while (lines.size() > capacity) {
            lines.pop_front();
        }
    }

    std::vector<std::string> snapshot() {
        std::lock_guard<std::mutex> lock(mutex);
        return std::vector<std::string>(lines.begin(), lines.end());
    }
};

LogWindow g_raopLog(256);
LogWindow g_shairportLog(256);
LogWindow g_libraopLog(512);

std::string g_statusMessage;
std::mutex g_statusMutex;
std::atomic<bool> g_uiDirty{true};

// Tab selection: 0 = Groups, 1 = RAOP, 2 = Shiri, 3 = Libraop
int g_selectedTab = 0;

// Selection indices for list-style tabs
std::size_t g_selectedGroupIndex = 0;
std::size_t g_selectedSpeakerIndex = 0;

// Creation state to show a spinner while creating a group
std::atomic<bool> g_creatingGroup{false};
int g_spinnerFrame = 0;

// Cached list of candidate network interfaces for AirPlay 2 instances.
std::vector<std::string> g_interfaces;
std::mutex g_interfacesMutex;

void getTerminalSize(int& rows, int& cols) {
    struct winsize w { };
    if (ioctl(STDOUT_FILENO, TIOCGWINSZ, &w) == -1) {
        const char* env_cols = std::getenv("COLUMNS");
        const char* env_lines = std::getenv("LINES");
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

struct OverviewStats {
    int totalSpeakers = 0;
    int onlineSpeakers = 0;
    int lockedSpeakers = 0;
    int totalGroups = 0;
    int activeGroups = 0;
};

std::string formatBytes(uint64_t bytes) {
    const char* units[] = {"B", "KB", "MB", "GB", "TB"};
    constexpr std::size_t unitCount = sizeof(units) / sizeof(units[0]);
    std::size_t idx = 0;
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

OverviewStats computeOverviewStats(const std::vector<GroupSnapshot>& groupData,
                                   const std::vector<SpeakerSnapshot>& speakerData) {
    OverviewStats stats;
    stats.totalGroups = static_cast<int>(groupData.size());
    for (const auto& grp : groupData) {
        if (grp.bytesReceived > 0) {
            ++stats.activeGroups;
        }
    }

    stats.totalSpeakers = static_cast<int>(speakerData.size());
    for (const auto& sp : speakerData) {
        if (sp.connected) ++stats.onlineSpeakers;
        if (sp.reserved) ++stats.lockedSpeakers;
    }
    return stats;
}

void buildSnapshots(std::vector<GroupSnapshot>& groupData,
                    std::vector<SpeakerSnapshot>& speakerData) {
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

void drawTabHeader(int cols) {
    setCursor(2, 1);
    for (int i = 0; i < cols; ++i) std::cout << ' ';

    auto drawTab = [&](int index, int col, const std::string& label) {
        setCursor(2, col);
        bool active = (g_selectedTab == index);
        if (active) std::cout << REVERSE << CYAN;
        std::cout << " [" << (index + 1) << "] " << label << ' ';
        if (active) std::cout << RESET;
    };

    int col = 2;
    drawTab(0, col, "Groups");
    col += 14;
    drawTab(1, col, "RAOP");
    col += 12;
    drawTab(2, col, "Shiri");
    col += 14;
    drawTab(3, col, "Libraop");
}

std::string spinner() {
    const char* chars[] = {"|", "/", "-", "\\"};
    return chars[g_spinnerFrame % 4];
}

void drawGroupsTab(const std::vector<GroupSnapshot>& groupData,
                   const std::vector<SpeakerSnapshot>& speakerData,
                   int rows, int cols) {
    int listWidth = cols / 3;
    int detailWidth = cols - listWidth - 6;

    int top = 5;
    int height = rows - 8;

    drawBox(top, 2, listWidth, height, "Groups");
    drawBox(top, 3 + listWidth, detailWidth / 2, height, "Group Details");
    drawBox(top, 3 + listWidth + detailWidth / 2, detailWidth / 2, height, "Speakers");

    // Left: group list with selection highlight
    int y = top + 1;
    if (groupData.empty()) {
        setCursor(y, 4);
        std::cout << YELLOW << "No groups defined. Press 'C' to create one." << RESET;
    } else {
        if (g_selectedGroupIndex >= groupData.size()) {
            g_selectedGroupIndex = groupData.size() - 1;
        }
        for (std::size_t i = 0; i < groupData.size() && y < top + height - 1; ++i) {
            const auto& grp = groupData[i];
            setCursor(y++, 4);
            bool active = (i == g_selectedGroupIndex);
            if (active) std::cout << REVERSE;
            std::string badge = grp.healthy ? (GREEN + "●" + RESET)
                                             : (YELLOW + "●" + RESET);
            std::cout << badge << ' ' << grp.name << "  (" << grp.port << ")";
            if (active) std::cout << RESET;
        }
    }

    // Right: details for selected group
    int dx = 3 + listWidth + 2;
    int dy = top + 1;

    if (g_creatingGroup.load()) {
        setCursor(dy++, dx);
        std::cout << CYAN << spinner() << " Creating group…" << RESET;
        setCursor(dy++, dx);
        std::cout << YELLOW << "Please wait." << RESET;
    } else if (groupData.empty()) {
        setCursor(dy++, dx);
        std::cout << YELLOW << "No groups." << RESET;
    } else {
        const auto& grp = groupData[g_selectedGroupIndex];
        setCursor(dy++, dx);
        std::cout << BOLD << grp.name << RESET << "  (port " << grp.port << ")";
        setCursor(dy++, dx);
        std::cout << "State: "
                  << (grp.healthy ? GREEN + std::string("ONLINE") + RESET
                                  : YELLOW + std::string("DEGRADED") + RESET);
        setCursor(dy++, dx);
        if (grp.bytesReceived > 0) {
            std::cout << "Bytes: " << formatBytes(grp.bytesReceived);
        } else {
            std::cout << "Bytes: waiting for audio…";
        }
        setCursor(dy++, dx);
        if (grp.lastChunkBytes > 0) {
            std::cout << "Last chunk: " << formatBytes(grp.lastChunkBytes)
                      << " (" << formatAge(grp.lastChunkAgeMs) << ")";
        } else {
            std::cout << "Last chunk: n/a";
        }

        dy++;
        setCursor(dy++, dx);
        std::cout << BOLD << "Members:" << RESET;
        for (const auto& member : grp.members) {
            if (dy >= top + height - 1) break;
            setCursor(dy++, dx + 2);
            std::cout << (member.second ? GREEN : RED)
                      << (member.second ? "* " : "x ")
                      << RESET << member.first;
        }
    }

    // Far right: speakers list plus a compact Interfaces section at the bottom.
    int sx = 3 + listWidth + detailWidth / 2 + 2;
    int sy = top + 1;
    for (const auto& sp : speakerData) {
        if (sy >= top + height - 4) break; // leave a few lines for interfaces
        setCursor(sy++, sx);
        std::string badge = sp.connected ? (GREEN + "[ON]" + RESET)
                                         : (RED + "[OFF]" + RESET);
        std::cout << badge << ' ' << sp.name;
        if (sp.hostage) {
            std::cout << RED << " [HOSTAGE]" << RESET;
        }
        setCursor(sy++, sx + 2);
        std::cout << sp.ip << ':' << sp.port
                  << (sp.reserved ? "  (locked)" : "  (free)");
        sy++;
    }

    // Compact interfaces list (out of the way at the bottom of the panel).
    std::vector<std::string> ifaceSnapshot;
    {
        std::lock_guard<std::mutex> lock(g_interfacesMutex);
        ifaceSnapshot = g_interfaces;
    }
    if (!ifaceSnapshot.empty()) {
        int ifaceMax = std::min<int>(static_cast<int>(ifaceSnapshot.size()), 4);
        int ifaceStartRow = top + height - (ifaceMax + 2);
        if (ifaceStartRow <= sy) ifaceStartRow = sy + 1;
        if (ifaceStartRow < top + 1) ifaceStartRow = top + 1;

        setCursor(ifaceStartRow, sx);
        std::cout << BOLD << "Interfaces:" << RESET;
        for (int i = 0; i < ifaceMax; ++i) {
            setCursor(ifaceStartRow + 1 + i, sx + 2);
            std::cout << "- " << ifaceSnapshot[static_cast<std::size_t>(i)];
        }
    }
}

void drawRaopLogTab(const std::vector<std::string>& raopLines,
                    int rows, int cols) {
    int panelTop = 5;
    int panelHeight = rows - panelTop - 4;
    int panelWidth = cols - 4;
    drawBox(panelTop, 2, panelWidth, panelHeight, "RAOP Logs");

    int y = panelTop + 1;
    int maxY = panelTop + panelHeight - 1;
    std::size_t visible = panelHeight - 2;
    std::size_t start = raopLines.size() > visible ? raopLines.size() - visible : 0;
    for (std::size_t i = start; i < raopLines.size() && y < maxY; ++i) {
        setCursor(y++, 4);
        std::cout << raopLines[i];
    }
}

void drawShiriLogTab(const std::vector<std::string>& shairportLines,
                     int rows, int cols) {
    int panelTop = 5;
    int panelHeight = rows - panelTop - 4;
    int panelWidth = cols - 4;
    drawBox(panelTop, 2, panelWidth, panelHeight, "Shiri Logs");

    int y = panelTop + 1;
    int maxY = panelTop + panelHeight - 1;
    std::size_t visible = panelHeight - 2;
    std::size_t start = shairportLines.size() > visible ? shairportLines.size() - visible : 0;
    for (std::size_t i = start; i < shairportLines.size() && y < maxY; ++i) {
        setCursor(y++, 4);
        std::cout << shairportLines[i];
    }
}

void drawLibraopLogTab(const std::vector<std::string>& libraopLines,
                       int rows, int cols) {
    int panelTop = 5;
    int panelHeight = rows - panelTop - 4;
    int panelWidth = cols - 4;
    drawBox(panelTop, 2, panelWidth, panelHeight, "Libraop Logs");

    int y = panelTop + 1;
    int maxY = panelTop + panelHeight - 1;
    std::size_t visible = panelHeight - 2;
    std::size_t start = libraopLines.size() > visible ? libraopLines.size() - visible : 0;
    for (std::size_t i = start; i < libraopLines.size() && y < maxY; ++i) {
        setCursor(y++, 4);
        std::cout << libraopLines[i];
    }
}

void render() {
    if (!g_uiDirty.load()) {
        return;
    }
    g_uiDirty = false;

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
    buildSnapshots(groupData, speakerData);

    std::string statusCopy;
    {
        std::lock_guard<std::mutex> lock(g_statusMutex);
        statusCopy = g_statusMessage;
    }

    auto raopLines      = g_raopLog.snapshot();
    auto shairportLines = g_shairportLog.snapshot();
    auto libraopLines   = g_libraopLog.snapshot();

    clearScreen();
    setCursor(1, 1);
    std::cout << BOLD << CYAN << "Shiri Bridge" << RESET << "  ·  Multi-Room AirPlay Controller";

    drawTabHeader(cols);

    switch (g_selectedTab) {
    case 0:
        drawGroupsTab(groupData, speakerData, rows, cols);
        break;
    case 1:
        drawRaopLogTab(raopLines, rows, cols);
        break;
    case 2:
        drawShiriLogTab(shairportLines, rows, cols);
        break;
    case 3:
        drawLibraopLogTab(libraopLines, rows, cols);
        break;
    default:
        break;
    }

    setCursor(rows - 3, 1);
    for (int i = 0; i < cols; ++i) std::cout << '=';
    setCursor(rows - 2, 2);
    std::cout << BOLD << "Keys:" << RESET
              << "  [1]Groups [2]RAOP [3]Shiri [4]Libraop"
              << "  Arrows: move  C:Create group  D:Delete group  Q:Quit";
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

constexpr int kBaseGroupPort = 6000;
constexpr int kMaxGroupPort = 20000;

// Refresh the cached list of candidate network interfaces for AirPlay 2.
void refreshInterfaces() {
    std::vector<std::string> interfaces;

    FILE* fp = popen("ip -o link show | awk -F': ' '($2!=\"lo\") {print $2}'", "r");
    if (fp) {
        char buf[256];
        while (fgets(buf, sizeof(buf), fp)) {
            std::string ifname(buf);
            if (!ifname.empty() && ifname.back() == '\n') ifname.pop_back();
            if (!ifname.empty()) interfaces.push_back(ifname);
        }
        pclose(fp);
    }

    std::lock_guard<std::mutex> lock(g_interfacesMutex);
    g_interfaces = std::move(interfaces);
}

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

// Simple interactive selector for speakers: arrows/j-k move, space toggles
// a checkbox, Enter confirms, Esc/Q cancels.
bool runSpeakerSelectionUI(const std::string& groupName,
                           const std::vector<SelectableSpeaker>& available,
                           std::vector<std::string>& chosenIds) {
    if (available.empty()) return false;

    int rows, cols;
    getTerminalSize(rows, cols);

    int current = 0;
    std::vector<bool> selected(available.size(), false); // default: none selected
    std::string error;

    while (true) {
        clearScreen();
        setCursor(1, 1);
        std::cout << BOLD << "Select speakers for group '" << groupName << "'" << RESET;
        setCursor(2, 1);
        std::cout << "Up/Down or j/k: move   Space: toggle   Enter: Done   Q/Esc: cancel";
        if (!error.empty()) {
            setCursor(3, 1);
            std::cout << RED << error << RESET;
        }

        int row = 5;
        for (std::size_t i = 0; i < available.size() && row < rows - 1; ++i) {
            const auto& sp = available[i];
            setCursor(row++, 4);
            bool active = (static_cast<int>(i) == current);
            if (active) std::cout << REVERSE;
            std::cout << '[' << (selected[i] ? '*' : ' ') << "] "
                      << sp.name << " [" << sp.ip << ']';
            if (active) std::cout << RESET;
        }
        std::cout.flush();

        int ch = getch();
        if (ch == -1) continue;

        if (ch == 27) { // ESC or arrows
            if (kbhit() > 0) {
                int c2 = getch();
                if (c2 == '[' && kbhit() > 0) {
                    int c3 = getch();
                    if (c3 == 'A') { // up
                        if (current > 0) --current;
                    } else if (c3 == 'B') { // down
                        if (current + 1 < static_cast<int>(available.size())) ++current;
                    }
                }
            } else {
                chosenIds.clear();
                return false; // bare ESC = cancel
            }
        } else if (ch == 'k' || ch == 'K') {
            if (current > 0) --current;
        } else if (ch == 'j' || ch == 'J') {
            if (current + 1 < static_cast<int>(available.size())) ++current;
        } else if (ch == ' ') {
            selected[static_cast<std::size_t>(current)] =
                !selected[static_cast<std::size_t>(current)];
        } else if (ch == '\n' || ch == '\r') {
            chosenIds.clear();
            for (std::size_t i = 0; i < available.size(); ++i) {
                if (selected[i]) {
                    chosenIds.push_back(available[i].id);
                }
            }
            if (chosenIds.empty()) {
                error = "Select at least one speaker.";
                continue;
            }
            return true;
        } else if (ch == 'q' || ch == 'Q') {
            chosenIds.clear();
            return false;
        }
    }
}

// Interactive selector for parent network interface: arrows/j-k move highlight,
// Enter selects the current entry, Q/Esc cancels.
bool runInterfaceSelectionUI(const std::vector<std::string>& interfaces,
                             std::size_t& ifaceIndex) {
    if (interfaces.empty()) return false;

    int rows, cols;
    getTerminalSize(rows, cols);

    int current = 0;
    std::string error;

    while (true) {
        clearScreen();
        setCursor(1, 1);
        std::cout << BOLD << "Select parent network interface for AirPlay 2" << RESET;
        setCursor(2, 1);
        std::cout << "Up/Down or j/k: move   Enter: Done   Q/Esc: cancel";
        if (!error.empty()) {
            setCursor(3, 1);
            std::cout << RED << error << RESET;
        }

        int row = 5;
        for (std::size_t i = 0; i < interfaces.size() && row < rows - 1; ++i) {
            setCursor(row++, 4);
            bool active = (static_cast<int>(i) == current);
            if (active) std::cout << REVERSE;
            std::cout << (active ? "[*] " : "[ ] ") << interfaces[i];
            if (active) std::cout << RESET;
        }
        std::cout.flush();

        int ch = getch();
        if (ch == -1) continue;

        if (ch == 27) { // ESC or arrows
            if (kbhit() > 0) {
                int c2 = getch();
                if (c2 == '[' && kbhit() > 0) {
                    int c3 = getch();
                    if (c3 == 'A') { // up
                        if (current > 0) --current;
                    } else if (c3 == 'B') { // down
                        if (current + 1 < static_cast<int>(interfaces.size())) ++current;
                    }
                }
            } else {
                return false; // bare ESC = cancel
            }
        } else if (ch == 'k' || ch == 'K') {
            if (current > 0) --current;
        } else if (ch == 'j' || ch == 'J') {
            if (current + 1 < static_cast<int>(interfaces.size())) ++current;
        } else if (ch == '\n' || ch == '\r') {
            ifaceIndex = static_cast<std::size_t>(current);
            return true;
        } else if (ch == 'q' || ch == 'Q') {
            return false;
        }
    }
}

bool createGroupFlow() {
    setNonCanonicalMode(false);
    g_creatingGroup = true;
    g_uiDirty = true;

    int rows, cols;
    getTerminalSize(rows, cols);
    setCursor(rows - 4, 1);
    std::cout << "\033[J";
    std::cout << "Enter new group name: " << std::flush;

    std::string name;
    std::getline(std::cin, name);
    if (name.empty()) {
        g_creatingGroup = false;
        g_uiDirty = true;
        Tui::SetStatus("Group creation cancelled.");
        setNonCanonicalMode(true);
        return false;
    }

    // From this point on, use non-canonical mode again so we can drive our
    // own interactive selection UIs with getch()/arrow keys.
    setNonCanonicalMode(true);

    std::vector<SelectableSpeaker> available;
    {
        std::lock_guard<std::mutex> lock(stateMutex);
        if (groups.count(name)) {
            g_creatingGroup = false;
            g_uiDirty = true;
            Tui::SetStatus("Group already exists.");
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
        g_creatingGroup = false;
        g_uiDirty = true;
        Tui::SetStatus("No available speakers to add.");
        setNonCanonicalMode(true);
        return false;
    }

    std::vector<std::string> chosenIds;
    if (!runSpeakerSelectionUI(name, available, chosenIds)) {
        g_creatingGroup = false;
        g_uiDirty = true;
        Tui::SetStatus("Group creation cancelled.");
        return false;
    }

    if (chosenIds.empty()) {
        g_creatingGroup = false;
        g_uiDirty = true;
        Tui::SetStatus("No speakers selected.");
        return false;
    }

    // Refresh and snapshot interfaces for both the Groups tab panel and this
    // selection flow.
    refreshInterfaces();
    std::vector<std::string> interfaces;
    {
        std::lock_guard<std::mutex> lock(g_interfacesMutex);
        interfaces = g_interfaces;
    }

    if (interfaces.empty()) {
        g_creatingGroup = false;
        g_uiDirty = true;
        Tui::SetStatus("No network interfaces available for AirPlay 2.");
        return false;
    }

    std::size_t ifaceIndex = 0;
    if (!runInterfaceSelectionUI(interfaces, ifaceIndex)) {
        g_creatingGroup = false;
        g_uiDirty = true;
        Tui::SetStatus("Group creation cancelled.");
        return false;
    }

    if (ifaceIndex >= interfaces.size()) {
        g_creatingGroup = false;
        g_uiDirty = true;
        Tui::SetStatus("Invalid interface selection.");
        return false;
    }

    std::string parentInterface = interfaces[ifaceIndex];

    // Allocate a port and insert a skeletal group so it appears in the UI
    // immediately, while the heavy work runs in the background.
    int port = -1;
    {
        std::lock_guard<std::mutex> lock(stateMutex);
        GroupInfo info;
        info.name = name;
        port = allocatePortLocked();
        if (port < 0) {
            g_creatingGroup = false;
            g_uiDirty = true;
            Tui::SetStatus("No free ports available.");
            setNonCanonicalMode(true);
            return false;
        }
        info.port = port;
        info.parentInterface = parentInterface;
        info.speakerIds = chosenIds;
        groups[name] = std::move(info);
    }

    // Launch background worker to connect RAOP hostages, start Shairport and
    // the streamer loop. This keeps the UI responsive; logs still go to their
    // RAOP/Shiri tabs.
    std::thread([name, parentInterface, chosenIds, port]() {
        {
            std::lock_guard<std::mutex> lock(stateMutex);
            auto git = groups.find(name);
            if (git == groups.end()) {
                return;
            }

            auto& group = git->second;

            // Connect hostages for speakers in this group
            for (const auto& id : chosenIds) {
                auto it = speakerStates.find(id);
                if (it != speakerStates.end() && !it->second.hostage) {
                    const auto& speaker = it->second.info;
                    if (!speaker.ip.empty() && speaker.ip != "0.0.0.0" && speaker.port > 0) {
                        it->second.hostage = std::make_unique<RaopHostage>(
                            speaker.ip, speaker.port, speaker.id, speaker.et, speaker.requiresAuth);
                        if (it->second.hostage->connect()) {
                            g_raopLog.add("Connected: " + speaker.id + " (group: " + name + ")");
                        } else {
                            g_raopLog.add("Failed to connect: " + speaker.id + " (group: " + name + ")");
                        }
                    }
                }
            }

            auto process = std::make_unique<Shairport>(name, port, parentInterface);
            process->setCallback([groupName = name](const uint8_t* data, std::size_t size) {
                if (size == 0) return;
                std::lock_guard<std::mutex> lock(stateMutex);
                auto groupIt = groups.find(groupName);
                if (groupIt == groups.end()) return;
                auto& pending = groupIt->second.pendingBytes;
                auto& queue   = groupIt->second.chunkQueue;
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
                groupIt->second.consecutiveSilenceChunks = 0;
            });

            process->start();
            group.process = std::move(process);
            group.streamerRunning = true;
            group.streamerThread = std::thread(groupStreamerLoop, name);
            for (const auto& id : chosenIds) {
                speakerStates[id].reserved = true;
            }
        }

        Tui::SetStatus("Group '" + name + "' created.");
        Tui::RequestRefresh();
    }).detach();

    // Return immediately to the main UI while the background worker finishes
    // starting the group.
    Tui::SetStatus("Group '" + name + "' starting up...");
    Tui::RequestRefresh();
    g_creatingGroup = false;
    g_uiDirty = true;
    setNonCanonicalMode(true);
    return true;
}

void deleteGroupFlow() {
    setNonCanonicalMode(false);

    // Resolve the currently selected group name from the shared state. This
    // keeps the UX aligned with the Groups tab list: up/down select, 'D'
    // deletes the highlighted entry.
    std::string name;
    {
        std::lock_guard<std::mutex> lock(stateMutex);
        if (groups.empty()) {
            Tui::SetStatus("No groups to delete.");
            setNonCanonicalMode(true);
            return;
        }
        if (g_selectedGroupIndex >= groups.size()) {
            g_selectedGroupIndex = groups.size() - 1;
        }
        auto it = groups.begin();
        std::advance(it, static_cast<long>(g_selectedGroupIndex));
        name = it->first;
    }

    std::thread streamer;
    std::unique_ptr<Shairport> processToStop;
    std::vector<std::string> speakers;
    {
        std::lock_guard<std::mutex> lock(stateMutex);
        auto it = groups.find(name);
        if (it == groups.end()) {
            Tui::SetStatus("Group not found.");
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
                        g_raopLog.add("Disconnected (group deleted): " + id);
                    }
                }
            }
            groups.erase(it);
        }
    }

    Tui::SetStatus("Group '" + name + "' deleted.");
    Tui::RequestRefresh();
    setNonCanonicalMode(true);
}

// Sink that libraop's cross_log can call into.
void LibraopLogSink(const char* line) {
    if (!line) return;
    Tui::AppendLibraopLog(line);
}

} // namespace (anonymous)

namespace Tui {

void Run() {
    // Install libraop log sink so that LOG_* from raop_lib appears in the UI.
    cross_log_set_sink(&LibraopLogSink);

    // Pre-populate cached interface list for the Groups tab NIC panel.
    refreshInterfaces();

    // Switch to the terminal's alternate screen buffer and hide the cursor so
    // the UI behaves like a classic full-screen TUI (htop/jetson-stats style)
    // without polluting the main scrollback with every refresh.
    std::cout << "\033[?1049h"  // use alternate screen buffer
              << "\033[H"       // move cursor to home
              << "\033[?25l";   // hide cursor
    std::cout.flush();

    setNonCanonicalMode(true);

    int pulseCounter = 0;

    while (running.load()) {
        if (kbhit() > 0) {
            int ch = getch();

            // Handle ANSI arrow keys (ESC [ A/B/C/D) for navigation.
            if (ch == 27) { // ESC
                if (kbhit() > 0) {
                    int c2 = getch();
                    if (c2 == '[' && kbhit() > 0) {
                        int c3 = getch();
                        if (c3 == 'A' || c3 == 'B') { // Up / Down
                            std::lock_guard<std::mutex> lock(stateMutex);
                            if (g_selectedTab == 0) { // Groups
                                std::size_t count = groups.size();
                                if (count > 0) {
                                    if (c3 == 'A') {
                                        if (g_selectedGroupIndex > 0) --g_selectedGroupIndex;
                                    } else {
                                        if (g_selectedGroupIndex + 1 < count) ++g_selectedGroupIndex;
                                    }
                                }
                            }
                            g_uiDirty = true;
                        }
                    }
                }
                continue;
            }

            if (ch == 'q' || ch == 'Q') {
                running = false;
                break;
            }

            // Tab switching
            if (ch == '1') { g_selectedTab = 0; g_uiDirty = true; }
            if (ch == '2') { g_selectedTab = 1; g_uiDirty = true; }
            if (ch == '3') { g_selectedTab = 2; g_uiDirty = true; }
            if (ch == '4') { g_selectedTab = 3; g_uiDirty = true; }

            // Vim-style navigation (only in Groups tab)
            if (ch == 'k' || ch == 'K') {
                if (g_selectedTab == 0 && g_selectedGroupIndex > 0) {
                    --g_selectedGroupIndex;
                    g_uiDirty = true;
                }
            }
            if (ch == 'j' || ch == 'J') {
                if (g_selectedTab == 0) {
                    std::lock_guard<std::mutex> lock(stateMutex);
                    std::size_t count = groups.size();
                    if (g_selectedGroupIndex + 1 < count) {
                        ++g_selectedGroupIndex;
                        g_uiDirty = true;
                    }
                }
            }

            // Group management (Groups tab only)
            if (ch == 'c' || ch == 'C') {
                if (g_selectedTab == 0) createGroupFlow();
            }
            if (ch == 'd' || ch == 'D') {
                if (g_selectedTab == 0) deleteGroupFlow();
            }
        }

        render();
        // Advance spinner while creating a group to give visual feedback
        if (g_creatingGroup.load()) {
            g_spinnerFrame++;
            g_uiDirty = true; // ensure spinner redraws
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));

        // Pulse hostages with adaptive frequency based on silence duration
        pulseCounter++;
        bool shouldPulse = false;
        {
            std::lock_guard<std::mutex> lock(stateMutex);
            bool longSilence = false;
            for (const auto& [name, group] : groups) {
                if (group.consecutiveSilenceChunks > 500) {
                    longSilence = true;
                    break;
                }
            }
            shouldPulse = pulseCounter >= (longSilence ? 10 : 30);
        }

        if (shouldPulse) {
            pulseCounter = 0;
            std::lock_guard<std::mutex> lock(stateMutex);
            for (auto& [id, state] : speakerStates) {
                if (state.hostage) {
                    state.hostage->pulse();
                    g_uiDirty = true;
                }
            }
        }
    }

    // Restore terminal state on exit.
    setNonCanonicalMode(false);
    // Show cursor and leave the alternate screen buffer, restoring whatever
    // the user had on their terminal before launching Shiri Bridge.
    std::cout << "\033[?25h"   // show cursor
              << "\033[?1049l"; // leave alternate screen buffer
    std::cout.flush();
}

void SetStatus(const std::string& message) {
    {
        std::lock_guard<std::mutex> lock(g_statusMutex);
        g_statusMessage = message;
    }
    g_uiDirty = true;
}

void RequestRefresh() {
    g_uiDirty = true;
}

void AppendRaopLog(const std::string& line) {
    g_raopLog.add(line);
    g_uiDirty = true;
}

void AppendShairportLog(const std::string& line) {
    g_shairportLog.add(line);
    g_uiDirty = true;
}

void AppendLibraopLog(const std::string& line) {
    g_libraopLog.add(line);
    g_uiDirty = true;
}

} // namespace Tui
