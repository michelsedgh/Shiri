#include "Shairport.h"
#include <chrono>
#include <iostream>
#include <vector>
#include <unistd.h>
#include <sys/wait.h>
#include <signal.h>

Shairport::Shairport(const std::string& groupName, int port)
    : groupName_(groupName), port_(port), running_(false), pipe_(nullptr), pid_(-1) {}

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
    return nowMillis() - last;
}

int64_t Shairport::nowMillis() {
    using namespace std::chrono;
    return duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count();
}

void Shairport::run() {
    int pipefd[2];
    if (pipe(pipefd) == -1) {
        perror("pipe");
        return;
    }

    pid_ = fork();
    if (pid_ == -1) {
        perror("fork");
        return;
    }

    if (pid_ == 0) {
        // Child
        close(pipefd[0]); // Close read end
        dup2(pipefd[1], STDOUT_FILENO); // Redirect stdout to pipe
        close(pipefd[1]);

        // Path to shairport-sync
        std::vector<std::string> possiblePaths = {
            "shiri-bridge/third_party/shairport-sync/shairport-sync", // From project root
            "../third_party/shairport-sync/shairport-sync",           // From build dir
            "third_party/shairport-sync/shairport-sync"               // From shiri-bridge dir
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
             exit(1);
        }

        std::string portStr = std::to_string(port_);
        
        // Arguments
        // -a Name -p Port -o stdout
        execl(path.c_str(), "shairport-sync", "-a", groupName_.c_str(), "-p", portStr.c_str(), "-o", "stdout", nullptr);
        
        perror("execl");
        exit(1);
    } else {
        // Parent
        close(pipefd[1]); // Close write end
        pipe_ = fdopen(pipefd[0], "r");
        
        // Read from pipe
        std::vector<char> buffer(4096);
        while (running_) {
            size_t n = fread(buffer.data(), 1, buffer.size(), pipe_);
            if (n == 0) break;
            // Process audio data
            // For now, just drop it or log size
            // std::cout << "Received " << n << " bytes from shairport" << std::endl;
            bytesReceived_ += n;
            lastChunkBytes_.store(n, std::memory_order_relaxed);
            lastChunkMillis_.store(nowMillis(), std::memory_order_relaxed);
        }
        
        fclose(pipe_);
    }
}
