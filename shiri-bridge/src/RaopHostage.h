#pragma once

#include <string>
#include <memory>
#include <vector>
#include <mutex>

// Forward declaration
struct raopcl_s;
struct in_addr;

class RaopHostage {
public:
    RaopHostage(const std::string& ip,
                int port,
                const std::string& id,
                std::string etCapabilities,
                bool preferAuth);
    ~RaopHostage();

    bool connect();
    void disconnect();
    void pulse();
    bool isConnected() const;
    
    // Audio pipeline
    bool acceptFrames();
    bool sendAudioChunk(const uint8_t* data, size_t size);
    bool waitForFramesReady(int maxAttempts = 200, int delayMillis = 1);
    
    const std::string& id() const { return id_; }

private:
    std::string ip_;
    int port_;
    std::string id_;
    std::string etCapabilities_;
    bool preferAuth_ = false;
    bool lastAuthUsed_ = false;
    struct raopcl_s* raop_ = nullptr;
    bool connected_ = false;
    uint64_t playtime_ = 0;

    bool attemptConnect(const struct in_addr& host, bool authFlag, const std::string& etOverride);
    bool ensureReachable(struct in_addr& host);
    static std::string sanitizeEt(const std::string& raw);
};
