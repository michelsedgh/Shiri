#pragma once

#include <atomic>
#include <functional>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#include <map>

#ifdef __APPLE__
#include <dns_sd.h>
#else
#include <avahi-client/client.h>
#include <avahi-client/lookup.h>
#include <avahi-common/simple-watch.h>
#include <avahi-common/error.h>
#include <avahi-common/address.h>
#endif

struct Speaker {
    std::string name;
    std::string ip;
    int port = 0;
    std::string id;
    std::string hostname;
    std::map<std::string, std::string> txtRecords;
    std::string et;
    bool requiresAuth = false;
    bool passwordRequired = false;
};

class Discovery {
public:
    using Callback = std::function<void(const std::vector<Speaker>&)>;

    Discovery();
    ~Discovery();

    void start(Callback callback);
    void stop();
    bool isRunning() const;

private:
    void run();
    void notifyListeners();

#ifdef __APPLE__
    DNSServiceRef browseRef_;

    static void DNSSD_API browseCallback(DNSServiceRef service,
                                         DNSServiceFlags flags,
                                         uint32_t interfaceIndex,
                                         DNSServiceErrorType errorCode,
                                         const char* serviceName,
                                         const char* regtype,
                                         const char* replyDomain,
                                         void* context);

    static void DNSSD_API resolveCallback(DNSServiceRef service,
                                          DNSServiceFlags flags,
                                          uint32_t interfaceIndex,
                                          DNSServiceErrorType errorCode,
                                          const char* fullname,
                                          const char* hosttarget,
                                          uint16_t port,
                                          uint16_t txtLen,
                                          const unsigned char* txtRecord,
                                          void* context);

    void resolveService(const std::string& serviceName,
                        const std::string& regtype,
                        const std::string& domain,
                        uint32_t interfaceIndex);

    void handleResolved(const std::string& fullname,
                        const std::string& hosttarget,
                        const std::string& ip,
                        uint16_t port,
                        const std::map<std::string, std::string>& txtRecords);

    void removeService(const std::string& fullname);
    static std::string makeFullName(const std::string& serviceName,
                                    const std::string& regtype,
                                    const std::string& domain);
#else
    AvahiSimplePoll* poll_;
    AvahiClient* client_;
    AvahiServiceBrowser* browser_;

    static void clientCallback(AvahiClient* c, AvahiClientState state, void* userdata);
    static void browseCallback(AvahiServiceBrowser* b,
                               AvahiIfIndex interface,
                               AvahiProtocol protocol,
                               AvahiBrowserEvent event,
                               const char* name,
                               const char* type,
                               const char* domain,
                               AvahiLookupResultFlags flags,
                               void* userdata);
    static void resolveCallback(AvahiServiceResolver* r,
                                AvahiIfIndex interface,
                                AvahiProtocol protocol,
                                AvahiResolverEvent event,
                                const char* name,
                                const char* type,
                                const char* domain,
                                const char* host_name,
                                const AvahiAddress* address,
                                uint16_t port,
                                AvahiStringList* txt,
                                AvahiLookupResultFlags flags,
                                void* userdata);

    void handleResolved(const std::string& fullname,
                        const std::string& hosttarget,
                        const std::string& ip,
                        uint16_t port,
                        const std::map<std::string, std::string>& txtRecords);
    void removeService(const std::string& fullname);
    static std::string makeFullName(const char* serviceName,
                                    const char* regtype,
                                    const char* domain);
    void cleanupAvahi();
#endif

    static std::map<std::string, std::string> parseTxtRecord(const unsigned char* txtRecord,
                                                             uint16_t txtLen);
#ifndef __APPLE__
    static std::map<std::string, std::string> parseTxtRecord(AvahiStringList* txt);
#endif
    static void applyTxtMetadata(Speaker& speaker,
                                 const std::map<std::string, std::string>& txtRecords);

    std::map<std::string, Speaker> speakers_;
    std::atomic<bool> running_;
    std::thread thread_;
    Callback callback_;
    std::mutex mutex_;
};
