#include "RaopHostage.h"
// Include C++ standard headers BEFORE raop_client.h to prevent macro collisions (min/max)
#include <iostream>
#include <sstream>
#include <string>
#include <memory>
#include <cctype>
#include <chrono>
#include <thread>
#include "Tui.h"

extern "C" {
#include "raop_client.h"
}
#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

namespace {
bool etSupportsToken(const std::string& et, char token) {
    return et.find(token) != std::string::npos;
}

bool etSupportsClear(const std::string& et) {
    return etSupportsToken(et, '0');
}

bool etSupportsRSA(const std::string& et) {
    return etSupportsToken(et, '1') || etSupportsToken(et, '3') || etSupportsToken(et, '4');
}

bool etSupportsFairPlay(const std::string& et) {
    return etSupportsToken(et, '4');
}
} // namespace

RaopHostage::RaopHostage(const std::string& ip,
                         int port,
                         const std::string& id,
                         std::string etCapabilities,
                         bool preferAuth)
    : ip_(ip),
      port_(port),
      id_(id),
      etCapabilities_(sanitizeEt(etCapabilities)),
      preferAuth_(preferAuth) {}

RaopHostage::~RaopHostage() {
    disconnect();
}

bool RaopHostage::connect() {
    if (connected_) return true;

    const bool attemptOrder[2] = { preferAuth_, !preferAuth_ };
    for (int i = 0; i < 2; ++i) {
        bool authFlag = attemptOrder[i];
        if (i == 1 && attemptOrder[1] == attemptOrder[0]) break;

        struct in_addr host {};
        if (!ensureReachable(host)) {
            return false;
        }

        if (attemptConnect(host, authFlag, etCapabilities_)) {
            return true;
        }

        std::ostringstream oss;
        oss << "[RaopHostage] RAOP connect failed for " << id_
            << " in auth mode " << (authFlag ? "ON" : "OFF");
        Tui::AppendRaopLog(oss.str());
    }

    Tui::AppendRaopLog("[RaopHostage] Exhausted all connection strategies for " + id_);
    return false;
}

void RaopHostage::disconnect() {
    if (raop_) {
        if (connected_) {
            raopcl_disconnect(raop_);
        }
        raopcl_destroy(raop_);
        raop_ = nullptr;
    }
    connected_ = false;
}

void RaopHostage::pulse() {
    if (connected_ && raop_) {
        if (!raopcl_keepalive(raop_)) {
            disconnect();
            connect();
        }
    }
}

bool RaopHostage::isConnected() const {
    return connected_;
}

bool RaopHostage::acceptFrames() {
    if (!connected_ || !raop_) return false;
    return raopcl_accept_frames(raop_);
}

bool RaopHostage::sendAudioChunk(const uint8_t* data, size_t size) {
    if (!connected_ || !raop_) return false;

    int frames = size / 4; // 16-bit stereo PCM
    if (frames <= 0) return false;

    return raopcl_send_chunk(raop_, const_cast<uint8_t*>(data), frames, &playtime_);
}

bool RaopHostage::waitForFramesReady(int maxAttempts, int delayMillis) {
    if (!connected_ || !raop_) return false;
    for (int attempt = 0; attempt < maxAttempts; ++attempt) {
        if (acceptFrames()) {
            return true;
        }
        if (delayMillis > 0) {
            std::this_thread::sleep_for(std::chrono::milliseconds(delayMillis));
        }
    }
    return false;
}

bool RaopHostage::attemptConnect(const struct in_addr& host,
                                 bool authFlag,
                                 const std::string& etOverride) {
    disconnect(); // ensure clean slate

    struct in_addr local_host;
    local_host.s_addr = INADDR_ANY;

    std::string etValue = sanitizeEt(etOverride);
    if (etValue.empty()) {
        etValue = etCapabilities_;
    }

    const bool supportClear = etSupportsClear(etValue);
    const bool supportRSA = etSupportsRSA(etValue);
    const bool supportFairPlay = etSupportsFairPlay(etValue);

    bool enableAuth = authFlag && supportFairPlay;
    bool useRSA = (!supportClear && supportRSA) || enableAuth;
    raop_crypto_t cryptoMode = useRSA ? RAOP_RSA : RAOP_CLEAR;

    if (enableAuth && etValue.find('4') == std::string::npos) {
        if (!etValue.empty()) etValue += ",";
        etValue += "4";
    }

    const char* etPtr = etValue.empty() ? nullptr : etValue.c_str();

    {
        std::ostringstream oss;
        oss << "[RaopHostage] Creating RAOP client for " << id_
            << " (auth=" << (authFlag ? "ON" : "OFF")
            << ", crypto=" << (cryptoMode == RAOP_RSA ? "RSA" : "CLEAR")
            << ", et=" << (etPtr ? etPtr : "none") << ")";
        Tui::AppendRaopLog(oss.str());
    }

    raop_ = raopcl_create(local_host, 0, 0, NULL, NULL,
                          RAOP_ALAC, DEFAULT_FRAMES_PER_CHUNK, 22050,
                          cryptoMode, enableAuth, NULL, NULL,
                          const_cast<char*>(etPtr),
                          NULL,
                          44100, 16, 2, 0.0f);

    if (!raop_) {
        Tui::AppendRaopLog("[RaopHostage] raopcl_create failed for " + id_);
        return false;
    }

    {
        std::ostringstream oss;
        oss << "[RaopHostage] Attempting RAOP protocol connect to " << id_
            << " at " << ip_ << ":" << port_
            << " (auth=" << (authFlag ? "ON" : "OFF") << ")";
        Tui::AppendRaopLog(oss.str());
    }

    if (!raopcl_connect(raop_, host, static_cast<uint16_t>(port_), true)) {
        {
            std::ostringstream oss;
            oss << "[RaopHostage] RAOP protocol connect failed for " << id_
                << " (auth=" << (authFlag ? "ON" : "OFF") << ")";
            Tui::AppendRaopLog(oss.str());
        }
        raopcl_destroy(raop_);
        raop_ = nullptr;
        return false;
    }

    connected_ = true;
    lastAuthUsed_ = authFlag;
    {
        std::ostringstream oss;
        oss << "[RaopHostage] RAOP connect succeeded for " << id_
            << " (auth=" << (authFlag ? "ON" : "OFF") << ")";
        Tui::AppendRaopLog(oss.str());
    }
    return true;
}

bool RaopHostage::ensureReachable(struct in_addr& host) {
    if (inet_aton(ip_.c_str(), &host) == 0) {
        Tui::AppendRaopLog("Invalid IP: " + ip_);
        return false;
    }
    if (host.s_addr == INADDR_ANY) {
        Tui::AppendRaopLog("Skipping RAOP connect to INADDR_ANY for " + id_);
        return false;
    }

    Tui::AppendRaopLog("Testing TCP reachability for " + id_ + " at " + ip_ + ":" + std::to_string(port_));
    int test_sock = socket(AF_INET, SOCK_STREAM, 0);
    if (test_sock < 0) {
        Tui::AppendRaopLog("Failed to create test socket for " + id_);
        return false;
    }

    struct sockaddr_in test_addr = {};
    test_addr.sin_family = AF_INET;
    test_addr.sin_port = htons(port_);
    test_addr.sin_addr = host;

    struct timeval tv = {1, 0};
    setsockopt(test_sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(test_sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    int conn_result = ::connect(test_sock, (struct sockaddr*)&test_addr, sizeof(test_addr));
    close(test_sock);

    if (conn_result < 0) {
        Tui::AppendRaopLog("Cannot reach " + id_ + " at " + ip_ + ":" + std::to_string(port_) + " (network issue)");
        return false;
    }

    Tui::AppendRaopLog("Reachability test passed for " + id_);
    return true;
}

std::string RaopHostage::sanitizeEt(const std::string& raw) {
    std::string result;
    result.reserve(raw.size());
    for (char ch : raw) {
        if (!std::isspace(static_cast<unsigned char>(ch))) {
            result.push_back(ch);
        }
    }
    return result;
}
