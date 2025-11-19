#include "Discovery.h"

#include <chrono>
#include <iostream>
#include <thread>

#ifdef __APPLE__
#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/select.h>
#include <sys/time.h>
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#endif

#ifndef __APPLE__
#include <cstring>
#endif

Discovery::Discovery()
    : running_(false)
#ifdef __APPLE__
    , browseRef_(nullptr)
#else
    , handle_(nullptr)
#endif
{}

Discovery::~Discovery() {
    stop();
}

void Discovery::start(Callback callback) {
    if (running_) {
        return;
    }
    callback_ = std::move(callback);
    running_ = true;
    thread_ = std::thread(&Discovery::run, this);
}

void Discovery::stop() {
    running_ = false;
    if (thread_.joinable()) {
        thread_.join();
    }
}

#ifdef __APPLE__

void Discovery::run() {
    DNSServiceErrorType err = DNSServiceBrowse(&browseRef_, 0, 0, "_raop._tcp", "local.", &Discovery::browseCallback, this);
    if (err != kDNSServiceErr_NoError) {
        std::cerr << "[Discovery] DNSServiceBrowse failed: " << err << std::endl;
        running_ = false;
        return;
    }

    int fd = DNSServiceRefSockFD(browseRef_);
    if (fd == -1) {
        std::cerr << "[Discovery] Invalid DNSService socket." << std::endl;
        running_ = false;
        return;
    }

    while (running_) {
        fd_set readfds;
        FD_ZERO(&readfds);
        FD_SET(fd, &readfds);
        timeval tv;
        tv.tv_sec = 1;
        tv.tv_usec = 0;
        int result = select(fd + 1, &readfds, nullptr, nullptr, &tv);
        if (result > 0 && FD_ISSET(fd, &readfds)) {
            DNSServiceProcessResult(browseRef_);
        } else if (result < 0) {
            std::cerr << "[Discovery] select() error." << std::endl;
            break;
        }
    }

    if (browseRef_) {
        DNSServiceRefDeallocate(browseRef_);
        browseRef_ = nullptr;
    }
}

void DNSSD_API Discovery::browseCallback(DNSServiceRef service,
                                         DNSServiceFlags flags,
                                         uint32_t interfaceIndex,
                                         DNSServiceErrorType errorCode,
                                         const char* serviceName,
                                         const char* regtype,
                                         const char* replyDomain,
                                         void* context) {
    (void)service;
    if (errorCode != kDNSServiceErr_NoError) {
        std::cerr << "[Discovery] browse callback error: " << errorCode << std::endl;
        return;
    }

    auto* self = static_cast<Discovery*>(context);
    const bool isAdd = (flags & kDNSServiceFlagsAdd) != 0;

    if (isAdd) {
        self->resolveService(serviceName ? serviceName : "",
                             regtype ? regtype : "",
                             replyDomain ? replyDomain : "",
                             interfaceIndex);
    } else {
        auto fullname = makeFullName(serviceName ? serviceName : "",
                                     regtype ? regtype : "",
                                     replyDomain ? replyDomain : "");
        self->removeService(fullname);
    }
}

void Discovery::resolveService(const std::string& serviceName,
                               const std::string& regtype,
                               const std::string& domain,
                               uint32_t interfaceIndex) {
    DNSServiceRef resolveRef = nullptr;
    DNSServiceErrorType err = DNSServiceResolve(&resolveRef,
                                                0,
                                                interfaceIndex,
                                                serviceName.c_str(),
                                                regtype.c_str(),
                                                domain.c_str(),
                                                &Discovery::resolveCallback,
                                                this);
    if (err != kDNSServiceErr_NoError) {
        std::cerr << "[Discovery] DNSServiceResolve failed: " << err << std::endl;
        return;
    }

    DNSServiceProcessResult(resolveRef);
    DNSServiceRefDeallocate(resolveRef);
}

void DNSSD_API Discovery::resolveCallback(DNSServiceRef service,
                                          DNSServiceFlags flags,
                                          uint32_t interfaceIndex,
                                          DNSServiceErrorType errorCode,
                                          const char* fullname,
                                          const char* hosttarget,
                                          uint16_t port,
                                          uint16_t txtLen,
                                          const unsigned char* txtRecord,
                                          void* context) {
    (void)service;
    (void)flags;
    (void)interfaceIndex;
    (void)txtLen;
    (void)txtRecord;

    if (errorCode != kDNSServiceErr_NoError) {
        std::cerr << "[Discovery] resolve callback error: " << errorCode << std::endl;
        return;
    }

    auto* self = static_cast<Discovery*>(context);
    self->handleResolved(fullname ? fullname : "",
                         hosttarget ? hosttarget : "",
                         ntohs(port),
                         txtRecord,
                         txtLen);
}

static std::string resolveHostToIp(const std::string& hosttarget) {
    if (hosttarget.empty()) {
        return {};
    }

    addrinfo hints{};
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* result = nullptr;
    int err = getaddrinfo(hosttarget.c_str(), nullptr, &hints, &result);
    if (err != 0 || result == nullptr) {
        return {};
    }

    char buffer[INET_ADDRSTRLEN] = {0};
    for (addrinfo* ptr = result; ptr != nullptr; ptr = ptr->ai_next) {
        if (ptr->ai_family == AF_INET) {
            auto* addr = reinterpret_cast<sockaddr_in*>(ptr->ai_addr);
            if (inet_ntop(AF_INET, &addr->sin_addr, buffer, sizeof(buffer))) {
                break;
            }
        }
    }
    freeaddrinfo(result);
    return std::string(buffer);
}

void Discovery::handleResolved(const std::string& fullname,
                               const std::string& hosttarget,
                               uint16_t port,
                               const unsigned char* txtRecord,
                               uint16_t txtLen) {
    (void)txtRecord;
    (void)txtLen;

    Speaker speaker;
    speaker.id = fullname.empty() ? hosttarget : fullname;
    speaker.name = fullname.empty() ? hosttarget : fullname;
    speaker.hostname = hosttarget;
    speaker.port = static_cast<int>(port);
    std::string ip = resolveHostToIp(hosttarget);
    speaker.ip = ip.empty() ? hosttarget : ip;

    {
        std::lock_guard<std::mutex> lock(mutex_);
        speakers_[speaker.id] = speaker;
    }

    notifyListeners();
}

void Discovery::removeService(const std::string& fullname) {
    if (fullname.empty()) {
        return;
    }

    bool removed = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        removed = speakers_.erase(fullname) > 0;
    }

    if (removed) {
        notifyListeners();
    }
}

std::string Discovery::makeFullName(const std::string& serviceName,
                                    const std::string& regtype,
                                    const std::string& domain) {
    return serviceName + "." + regtype + domain;
}

#else  // __APPLE__

void Discovery::run() {
    struct in_addr host = {0};
    handle_ = mdnssd_init(0, host, false);
    if (!handle_) {
        std::cerr << "[Discovery] Failed to init mdnssd" << std::endl;
        running_ = false;
        return;
    }

    while (running_) {
        bool ok = mdnssd_query(handle_, "_raop._tcp.local", false, 1,
                               [](mdnssd_service_t* services, void* cookie, bool* stop) -> bool {
                                   (void)services;
                                   (void)cookie;
                                   (void)stop;
                                   return true;
                               },
                               nullptr);
        if (!ok) {
            std::cerr << "[Discovery] mdnssd_query failed" << std::endl;
        }

        mdnssd_service_t* list = mdnssd_get_list(handle_);
        std::vector<Speaker> speakers;
        for (mdnssd_service_t* curr = list; curr; curr = curr->next) {
            Speaker s;
            s.name = curr->name ? curr->name : "Unknown";
            s.ip = inet_ntoa(curr->addr);
            s.port = curr->port;
            s.id = s.name;
            speakers.push_back(std::move(s));
        }

        if (callback_) {
            callback_(speakers);
        }

        if (list) {
            mdnssd_free_list(list);
        }

        std::this_thread::sleep_for(std::chrono::seconds(2));
    }

    mdnssd_close(handle_);
    handle_ = nullptr;
}

#endif  // __APPLE__

void Discovery::notifyListeners() {
#ifdef __APPLE__
    if (!callback_) {
        return;
    }

    std::vector<Speaker> snapshot;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        snapshot.reserve(speakers_.size());
        for (const auto& kv : speakers_) {
            snapshot.push_back(kv.second);
        }
    }

    callback_(snapshot);
#else
    // Non-Apple path invokes callbacks directly in the run loop
    (void)callback_;
#endif
}
