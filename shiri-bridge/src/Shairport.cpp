// Shairport.cpp
// Launch one AirPlay 2-capable shairport-sync instance per group inside its own
// network namespace with a macvlan on parentInterface_. Inside the namespace we
// also create a private /run, run dbus-daemon, avahi-daemon, nqptp and finally
// shairport-sync with the stdout pipe backend so that PCM can be streamed to
// RAOP speakers as before.

#include "Shairport.h"
#include "Tui.h"

#include <chrono>
#include <iostream>
#include <string>
#include <vector>

#include <unistd.h>
#include <sys/wait.h>
#include <signal.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <sys/mount.h>

// For setns / unshare / clone flags
#include <sched.h>

// Some toolchains may not expose these clone flags by default; define if missing.
#ifndef CLONE_NEWNET
#define CLONE_NEWNET 0x40000000
#endif
#ifndef CLONE_NEWNS
#define CLONE_NEWNS 0x00020000
#endif

#ifndef MS_REC
#define MS_REC 16384
#endif
#ifndef MS_PRIVATE
#define MS_PRIVATE (1 << 18)
#endif

extern "C" int setns(int fd, int nstype);
extern "C" int unshare(int flags);

// For network namespace management from C++, we shell out to `ip` commands, so
// there is no direct netlink dependency here.

Shairport::Shairport(const std::string& groupName, int port, const std::string& parentInterface)
    : groupName_(groupName),
      port_(port),
      parentInterface_(parentInterface),
      running_(false),
      pipe_(nullptr),
      pid_(-1) {}

Shairport::~Shairport() {
    stop();
}

void Shairport::start() {
    if (running_) return;
    running_ = true;
    thread_ = std::thread(&Shairport::run, this);
}

void Shairport::stop() {
    running_ = false;
    if (pid_ > 0) {
        kill(pid_, SIGTERM);
        waitpid(pid_, nullptr, 0);
        pid_ = -1;
    }
    if (thread_.joinable()) {
        thread_.join();
    }
}

void Shairport::setCallback(AudioCallback callback) {
    callback_ = std::move(callback);
}

uint64_t Shairport::bytesReceived() const {
    return bytesReceived_.load();
}

uint64_t Shairport::lastChunkBytes() const {
    return lastChunkBytes_.load();
}

int64_t Shairport::millisSinceLastChunk() const {
    auto last = lastChunkMillis_.load();
    if (last == 0) {
        return -1;
    }
    // To maintain const correctness we can't call the non-const nowMillis().
    // But nowMillis() doesn't modify state, it just reads the clock.
    // So let's manually get the time here or make nowMillis static/const.
    using namespace std::chrono;
    int64_t now = duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
    return now - last;
}

int64_t Shairport::nowMillis() {
    using namespace std::chrono;
    return duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
}

void Shairport::run() {
    // Generate short, unique IDs for namespace and macvlan to avoid IFNAMSIZ issues.
    using namespace std::chrono;
    auto now = duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
    unsigned int id = static_cast<unsigned int>(now & 0xffffffff);
    char id_hex[9] = {0};
    snprintf(id_hex, sizeof(id_hex), "%08x", id);

    std::string nsName = std::string("ap2n_") + id_hex;  // network namespace
    std::string mvName = std::string("ap2m_") + id_hex;  // macvlan interface

    // Create the namespace and macvlan using ip(8).
    auto runShell = [](const std::string& cmd) {
        int ret = system(cmd.c_str());
        if (ret != 0) {
            Tui::AppendShairportLog("Shairport netns: command failed: " + cmd + " (exit=" + std::to_string(ret) + ")");
        }
        return ret;
    };

    std::string cmd;

    cmd = "ip netns add " + nsName;
    if (runShell(cmd) != 0) {
        return;
    }

    // Ensure cleanup if something fails after this point. This is best-effort and
    // quiet, since at this stage we're just trying not to leak namespaces.
    auto cleanupNs = [&]() {
        std::string delCmd = "ip netns delete " + nsName + " >/dev/null 2>&1";
        system(delCmd.c_str());
        std::string delLink = "ip link delete " + mvName + " >/dev/null 2>&1";
        system(delLink.c_str());
    };

    cmd = "ip link add " + mvName + " link " + parentInterface_ + " type macvlan";
    if (runShell(cmd) != 0) {
        cleanupNs();
        return;
    }

    cmd = "ip link set " + mvName + " netns " + nsName;
    if (runShell(cmd) != 0) {
        cleanupNs();
        return;
    }

    // Pipe for PCM from shairport-sync stdout
    int pipefd[2];
    if (pipe(pipefd) == -1) {
        perror("pipe");
        Tui::AppendShairportLog("pipe failed for Shairport");
        cleanupNs();
        return;
    }

    pid_ = fork();
    if (pid_ == -1) {
        perror("fork");
        Tui::AppendShairportLog("fork failed for Shairport");
        return;
    }

    if (pid_ == 0) {
        // Child: join namespace, set up mounts and daemons, then exec shairport-sync.
        // We keep stdout on the terminal for setup commands, and only redirect it to
        // the PCM pipe right before launching shairport-sync so logs don't corrupt
        // the audio stream.
        close(pipefd[0]); // Close read end

        // Join the network namespace via setns on /run/netns/<nsName>.
        std::string nsPath = "/run/netns/" + nsName;
        int nsFd = open(nsPath.c_str(), O_RDONLY);
        if (nsFd == -1) {
            perror("open netns");
            _exit(1);
        }
        if (setns(nsFd, CLONE_NEWNET) == -1) {
            perror("setns");
            close(nsFd);
            _exit(1);
        }
        close(nsFd);

        // Bring up lo and the macvlan inside the joined namespace.
        std::string ipCmd = "ip link set lo up && ip link set " + mvName + " up";
        if (system(ipCmd.c_str()) != 0) {
            std::cerr << "Failed to bring up interfaces in netns" << std::endl;
            _exit(1);
        }

        // Acquire IP via DHCP inside the namespace. dhclient is very verbose
        // by default; redirect its stdout/stderr so it doesn't corrupt the
        // TUI screen. We still treat a non-zero exit code as a hard error.
        std::string dhcpCmd = "dhclient -v " + mvName + " >/dev/null 2>&1";
        if (system(dhcpCmd.c_str()) != 0) {
            std::cerr << "dhclient failed in netns" << std::endl;
            _exit(1);
        }

        // Create a private mount namespace for /run.
        if (unshare(CLONE_NEWNS) == -1) {
            perror("unshare(CLONE_NEWNS)");
            _exit(1);
        }

        if (mount("none", "/run", nullptr, MS_REC | MS_PRIVATE, nullptr) == -1) {
            perror("mount --make-rprivate /run");
            _exit(1);
        }

        // Mount /run as tmpfs.
        if (mount("tmpfs", "/run", "tmpfs", 0, nullptr) == -1) {
            perror("mount /run");
            _exit(1);
        }
        mkdir("/run/dbus", 0755);
        mkdir("/run/avahi-daemon", 0755);

        // Start dbus-daemon, avahi-daemon, nqptp similar to the shell script.
        // We use system() for now to avoid duplicating daemonisation logic.
        if (system("dbus-daemon --system --fork --nopidfile") != 0) {
            std::cerr << "Failed to start dbus-daemon" << std::endl;
            _exit(1);
        }
        sleep(1);

        if (system("avahi-daemon --daemonize --no-chroot --no-drop-root --file /etc/avahi/avahi-daemon.conf --no-rlimits") != 0) {
            std::cerr << "Failed to start avahi-daemon" << std::endl;
            _exit(1);
        }
        sleep(1);

        if (system("nqptp > /run/nqptp.log 2>&1 &") != 0) {
            std::cerr << "Failed to start nqptp" << std::endl;
            _exit(1);
        }
        sleep(1);

        // From this point on, we only want raw PCM from shairport-sync on stdout.
        if (dup2(pipefd[1], STDOUT_FILENO) == -1) {
            perror("dup2");
            _exit(1);
        }
        close(pipefd[1]);

        // Path to shairport-sync (AirPlay 2 + pipe backend).
        std::vector<std::string> possiblePaths = {
            "shiri-bridge/third_party/shairport-sync/shairport-sync", // From project root
            "../third_party/shairport-sync/shairport-sync",           // From build dir
            "third_party/shairport-sync/shairport-sync",              // From shiri-bridge dir
            "/usr/local/bin/shairport-sync"                           // System-wide install
        };

        std::string path;
        bool found = false;
        for (const auto& p : possiblePaths) {
            if (access(p.c_str(), X_OK) == 0) {
                path = p;
                found = true;
                break;
            }
        }

        if (!found) {
            std::cerr << "Error: shairport-sync binary not found in expected locations." << std::endl;
            Tui::AppendShairportLog("Error: shairport-sync binary not found in expected locations.");
            _exit(1);
        }

        std::string portStr = std::to_string(port_);

        execl(path.c_str(), "shairport-sync",
              "-a", groupName_.c_str(),
              "-p", portStr.c_str(),
              "-o", "stdout",
              (char*)nullptr);

        perror("execl shairport-sync");
        Tui::AppendShairportLog("execl shairport-sync failed");
        _exit(1);
    } else {
        // Parent: read PCM from pipe and feed callback.
        close(pipefd[1]); // Close write end
        pipe_ = fdopen(pipefd[0], "r");

        Tui::AppendShairportLog("[Shairport] Started for group '" + groupName_ + "' on port " + std::to_string(port_) + " with parent interface '" + parentInterface_ + "' (pid " + std::to_string(pid_) + ")");

        std::vector<uint8_t> buffer(4096);
        while (running_) {
            size_t n = fread(buffer.data(), 1, buffer.size(), pipe_);
            if (n == 0) break;

            if (callback_) {
                callback_(buffer.data(), n);
            }

            bytesReceived_ += n;
            lastChunkBytes_.store(n, std::memory_order_relaxed);
            lastChunkMillis_.store(nowMillis(), std::memory_order_relaxed);
        }

        fclose(pipe_);

        // Tear down namespace and macvlan when shairport-sync exits.
        cleanupNs();
    }
}
