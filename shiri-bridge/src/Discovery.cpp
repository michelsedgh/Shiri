#ifndef __APPLE__
#include <avahi-common/malloc.h>
#include <avahi-common/strlst.h>
#endif
#include "Discovery.h"

#include <chrono>
#include <iostream>
#include <thread>
#include <cstring>
#include <cctype>

#ifdef __APPLE__
#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/select.h>
#include <sys/time.h>
#include <dns_sd.h> // Use system header
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netdb.h>
#endif

Discovery::Discovery()
    : running_(false)
#ifdef __APPLE__
    , browseRef_(nullptr)
#else
    , poll_(nullptr)
    , client_(nullptr)
    , browser_(nullptr)
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
#ifndef __APPLE__
    if (poll_) {
        avahi_simple_poll_quit(poll_);
    }
#endif
    if (thread_.joinable()) {
        thread_.join();
    }
}

bool Discovery::isRunning() const {
    return running_;
}

#ifdef __APPLE__
// Apple implementation unchanged...
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
    if (errorCode != kDNSServiceErr_NoError) {
        std::cerr << "[Discovery] resolve callback error: " << errorCode << std::endl;
        return;
    }

    auto* self = static_cast<Discovery*>(context);
    std::string host = hosttarget ? hosttarget : "";
    std::string ip = resolveHostToIp(host);
    auto txtMap = parseTxtRecord(txtRecord, txtLen);
    self->handleResolved(fullname ? fullname : "",
                         host,
                         ip.empty() ? host : ip,
                         ntohs(port),
                         txtMap);
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
                               const std::string& ip,
                               uint16_t port,
                               const std::map<std::string, std::string>& txtRecords) {
    Speaker speaker;
    speaker.id = fullname.empty() ? hosttarget : fullname;
    speaker.name = fullname.empty() ? hosttarget : fullname;
    speaker.hostname = hosttarget;
    speaker.port = static_cast<int>(port);
    speaker.ip = ip.empty() ? hosttarget : ip;
    applyTxtMetadata(speaker, txtRecords);

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

std::map<std::string, std::string> Discovery::parseTxtRecord(const unsigned char* txtRecord,
                                                             uint16_t txtLen) {
#ifdef __APPLE__
    std::map<std::string, std::string> result;
    if (!txtRecord || txtLen == 0) {
        return result;
    }

    uint16_t count = TXTRecordGetCount(txtLen, txtRecord);
    for (uint16_t idx = 0; idx < count; ++idx) {
        char key[256] = {0};
        uint8_t valueLen = 0;
        const void* valuePtr = nullptr;
        if (TXTRecordGetItemAtIndex(txtLen,
                                    txtRecord,
                                    idx,
                                    key,
                                    &valueLen,
                                    &valuePtr) == kDNSServiceErr_NoError) {
            std::string value;
            if (valuePtr && valueLen > 0) {
                value.assign(static_cast<const char*>(valuePtr), valueLen);
            }
            result[key] = value;
        }
    }
    return result;
#else
    (void)txtRecord;
    (void)txtLen;
    return {};
#endif
}

#ifndef __APPLE__
std::map<std::string, std::string> Discovery::parseTxtRecord(AvahiStringList* txt) {
    std::map<std::string, std::string> result;
    for (AvahiStringList* entry = txt; entry != nullptr; entry = avahi_string_list_get_next(entry)) {
        char* key = nullptr;
        char* value = nullptr;
        avahi_string_list_get_pair(entry, &key, &value, nullptr);
        if (key) {
            result[key] = value ? value : "";
            avahi_free(key);
        }
        if (value) {
            avahi_free(value);
        }
    }
    return result;
}
#endif

void Discovery::applyTxtMetadata(Speaker& speaker,
                                 const std::map<std::string, std::string>& txtRecords) {
    speaker.txtRecords = txtRecords;

    auto sanitize = [](const std::string& in) {
        std::string out;
        out.reserve(in.size());
        for (char ch : in) {
            if (!std::isspace(static_cast<unsigned char>(ch))) {
                out.push_back(ch);
            }
        }
        return out;
    };

    auto etIt = txtRecords.find("et");
    if (etIt != txtRecords.end()) {
        speaker.et = sanitize(etIt->second);
    } else {
        speaker.et.clear();
    }

    auto pwIt = txtRecords.find("pw");
    speaker.passwordRequired = (pwIt != txtRecords.end() && pwIt->second == "1");

    speaker.requiresAuth = speaker.passwordRequired;

    std::cerr << "[Discovery] Speaker '" << speaker.name << "' et="
              << (speaker.et.empty() ? "n/a" : speaker.et)
              << " auth_required=" << (speaker.requiresAuth ? "yes" : "no")
              << std::endl;
}

std::string Discovery::makeFullName(const std::string& serviceName,
                                    const std::string& regtype,
                                    const std::string& domain) {
    return serviceName + "." + regtype + domain;
}

#else  // __APPLE__

namespace {
std::string addressToString(const AvahiAddress* address, const char* host_name) {
    if (address) {
        char addr[AVAHI_ADDRESS_STR_MAX] = {0};
        avahi_address_snprint(addr, sizeof(addr), address);
        if (addr[0] != '\0')
            return std::string(addr);
    }
    return host_name ? std::string(host_name) : std::string{};
}
} // namespace

void Discovery::cleanupAvahi() {
    if (browser_) {
        avahi_service_browser_free(browser_);
        browser_ = nullptr;
    }
    if (client_) {
        avahi_client_free(client_);
        client_ = nullptr;
    }
    if (poll_) {
        avahi_simple_poll_free(poll_);
        poll_ = nullptr;
    }
}

void Discovery::clientCallback(AvahiClient* c, AvahiClientState state, void* userdata) {
    auto* self = static_cast<Discovery*>(userdata);
    if (!self) return;

    if (state == AVAHI_CLIENT_FAILURE) {
        std::cerr << "[Discovery] Avahi client failure: " << avahi_strerror(avahi_client_errno(c)) << std::endl;
        self->running_ = false;
        if (self->poll_) {
            avahi_simple_poll_quit(self->poll_);
        }
    }
}

void Discovery::browseCallback(AvahiServiceBrowser* b,
                               AvahiIfIndex interface,
                               AvahiProtocol protocol,
                               AvahiBrowserEvent event,
                               const char* name,
                               const char* type,
                               const char* domain,
                               AvahiLookupResultFlags flags,
                               void* userdata) {
    (void)b;
    (void)flags;
    auto* self = static_cast<Discovery*>(userdata);
    if (!self) return;

    switch (event) {
    case AVAHI_BROWSER_NEW:
        if (self->client_) {
            if (!avahi_service_resolver_new(self->client_,
                                            interface,
                                            protocol,
                                            name,
                                            type,
                                            domain,
                                            AVAHI_PROTO_UNSPEC,
                                            (AvahiLookupFlags)0,
                                            resolveCallback,
                                            self)) {
                std::cerr << "[Discovery] Failed to resolve service " << (name ? name : "?") << std::endl;
            }
        }
        break;
    case AVAHI_BROWSER_REMOVE: {
        auto fullname = makeFullName(name, type, domain);
        self->removeService(fullname);
        break;
    }
    case AVAHI_BROWSER_FAILURE:
        std::cerr << "[Discovery] Avahi browser failure: " << avahi_strerror(avahi_client_errno(self->client_)) << std::endl;
        self->running_ = false;
        if (self->poll_) {
            avahi_simple_poll_quit(self->poll_);
        }
        break;
    default:
        break;
    }
}

void Discovery::resolveCallback(AvahiServiceResolver* r,
                                AvahiIfIndex,
                                AvahiProtocol,
                                AvahiResolverEvent event,
                                const char* name,
                                const char* type,
                                const char* domain,
                                const char* host_name,
                                const AvahiAddress* address,
                                uint16_t port,
                                AvahiStringList* txt,
                                AvahiLookupResultFlags,
                                void* userdata) {
    (void)txt;
    auto* self = static_cast<Discovery*>(userdata);
    if (!self) {
        if (r) avahi_service_resolver_free(r);
        return;
    }

    if (event == AVAHI_RESOLVER_FOUND) {
        auto fullname = makeFullName(name, type, domain);
        auto ip = addressToString(address, host_name);
        auto txtMap = parseTxtRecord(txt);
        self->handleResolved(fullname,
                             host_name ? host_name : "",
                             ip,
                             port,
                             txtMap);
    }

    if (r) avahi_service_resolver_free(r);
}

void Discovery::run() {
    poll_ = avahi_simple_poll_new();
    if (!poll_) {
        std::cerr << "[Discovery] Failed to create Avahi poll." << std::endl;
        running_ = false;
        return;
    }

    int error = 0;
    client_ = avahi_client_new(avahi_simple_poll_get(poll_),
                               AVAHI_CLIENT_NO_FAIL,
                               clientCallback,
                               this,
                               &error);
    if (!client_) {
        std::cerr << "[Discovery] Failed to create Avahi client: " << avahi_strerror(error) << std::endl;
        cleanupAvahi();
        running_ = false;
        return;
    }

    browser_ = avahi_service_browser_new(client_,
                                         AVAHI_IF_UNSPEC,
                                         AVAHI_PROTO_UNSPEC,
                                         "_raop._tcp",
                                         "local",
                                         (AvahiLookupFlags)0,
                                         browseCallback,
                                         this);
    if (!browser_) {
        std::cerr << "[Discovery] Failed to create Avahi service browser." << std::endl;
        cleanupAvahi();
        running_ = false;
        return;
    }

    while (running_) {
        int ret = avahi_simple_poll_iterate(poll_, 100);
        if (ret == AVAHI_ERR_DISCONNECTED) {
            std::cerr << "[Discovery] Avahi disconnected." << std::endl;
            running_ = false;
            break;
        } else if (ret < 0 && ret != AVAHI_ERR_TIMEOUT) {
            std::cerr << "[Discovery] Avahi poll error: " << ret << std::endl;
            running_ = false;
            break;
        }
    }

    cleanupAvahi();
}

void Discovery::handleResolved(const std::string& fullname,
                               const std::string& hosttarget,
                               const std::string& ip,
                               uint16_t port,
                               const std::map<std::string, std::string>& txtRecords) {
    if (ip.empty() || ip == "0.0.0.0") {
        return;
    }

    Speaker speaker;
    speaker.id = fullname.empty() ? hosttarget : fullname;
    speaker.name = fullname.empty() ? hosttarget : fullname;
    speaker.hostname = hosttarget;
    speaker.port = static_cast<int>(port);
    speaker.ip = ip;
    applyTxtMetadata(speaker, txtRecords);

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

std::string Discovery::makeFullName(const char* serviceName,
                                    const char* regtype,
                                    const char* domain) {
    std::string result;
    if (serviceName && serviceName[0]) result += serviceName;
    result += ".";
    if (regtype && regtype[0]) result += regtype;
    if (domain && domain[0]) result += domain;
    return result;
}

#endif  // __APPLE__

std::map<std::string, std::string> Discovery::parseTxtRecord(const unsigned char* txtRecord,
                                                             uint16_t txtLen) {
    std::map<std::string, std::string> result;
#ifdef __APPLE__
    if (!txtRecord || txtLen == 0) {
        return result;
    }

    uint16_t count = TXTRecordGetCount(txtLen, txtRecord);
    for (uint16_t idx = 0; idx < count; ++idx) {
        char key[256] = {0};
        uint8_t valueLen = 0;
        const void* valuePtr = nullptr;
        if (TXTRecordGetItemAtIndex(txtLen,
                                    txtRecord,
                                    idx,
                                    key,
                                    &valueLen,
                                    &valuePtr) == kDNSServiceErr_NoError) {
            std::string value;
            if (valuePtr && valueLen > 0) {
                value.assign(static_cast<const char*>(valuePtr), valueLen);
            }
            result[key] = value;
        }
    }
#else
    (void)txtRecord;
    (void)txtLen;
#endif
    return result;
}

#ifndef __APPLE__
std::map<std::string, std::string> Discovery::parseTxtRecord(AvahiStringList* txt) {
    std::map<std::string, std::string> result;
    for (AvahiStringList* entry = txt; entry != nullptr; entry = avahi_string_list_get_next(entry)) {
        char* key = nullptr;
        char* value = nullptr;
        avahi_string_list_get_pair(entry, &key, &value, nullptr);
        if (key) {
            result[key] = value ? value : "";
            avahi_free(key);
        }
        if (value) {
            avahi_free(value);
        }
    }
    return result;
}
#endif

void Discovery::applyTxtMetadata(Speaker& speaker,
                                 const std::map<std::string, std::string>& txtRecords) {
    speaker.txtRecords = txtRecords;

    auto sanitize = [](const std::string& in) {
        std::string out;
        out.reserve(in.size());
        for (char ch : in) {
            if (!std::isspace(static_cast<unsigned char>(ch))) {
                out.push_back(ch);
            }
        }
        return out;
    };

    auto etIt = txtRecords.find("et");
    if (etIt != txtRecords.end()) {
        speaker.et = sanitize(etIt->second);
    } else {
        speaker.et.clear();
    }

    auto pwIt = txtRecords.find("pw");
    speaker.passwordRequired = (pwIt != txtRecords.end() && pwIt->second == "1");

    speaker.requiresAuth = speaker.passwordRequired;
    if (!speaker.et.empty() && speaker.et.find('4') != std::string::npos) {
        speaker.requiresAuth = true;
    }

    std::cerr << "[Discovery] Speaker '" << speaker.name << "' et="
              << (speaker.et.empty() ? "n/a" : speaker.et)
              << " auth_required=" << (speaker.requiresAuth ? "yes" : "no")
              << std::endl;
}

void Discovery::notifyListeners() {
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
}
