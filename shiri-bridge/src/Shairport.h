#pragma once
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <string>
#include <thread>
#include <vector>

class Shairport {
public:
    Shairport(const std::string& groupName, int port);
    ~Shairport();

    void start();
    void stop();

    uint64_t bytesReceived() const;
    uint64_t lastChunkBytes() const;
    int64_t millisSinceLastChunk() const;

private:
    static int64_t nowMillis();
    void run();

    std::string groupName_;
    int port_;
    std::atomic<bool> running_;
    std::thread thread_;
    FILE* pipe_;
    pid_t pid_;
    std::atomic<uint64_t> bytesReceived_{0};
    std::atomic<uint64_t> lastChunkBytes_{0};
    std::atomic<int64_t> lastChunkMillis_{0};
};
