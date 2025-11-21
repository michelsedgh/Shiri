#pragma once

#include <string>
#include <atomic>
#include <thread>
#include <functional>

class Shairport {
public:
    using AudioCallback = std::function<void(const uint8_t* data, size_t size)>;

    Shairport(const std::string& groupName, int port, const std::string& parentInterface);
    ~Shairport();

    void start();
    void stop();
    void setCallback(AudioCallback callback);

    uint64_t bytesReceived() const;
    uint64_t lastChunkBytes() const;
    int64_t millisSinceLastChunk() const;

private:
    void run();
    int64_t nowMillis();

    std::string groupName_;
    int port_;
    std::string parentInterface_;
    std::atomic<bool> running_;
    std::thread thread_;
    FILE* pipe_;
    pid_t pid_;

    std::atomic<uint64_t> bytesReceived_{0};
    std::atomic<uint64_t> lastChunkBytes_{0};
    std::atomic<int64_t> lastChunkMillis_{0};

    AudioCallback callback_;
};
