#pragma once

#include <atomic>
#include <functional>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#ifdef __APPLE__
#include <dns_sd.h>
#else
extern "C" {
#if defined(__has_include)
#  if __has_include(<mdnssd/mdnssd.h>)
#    include <mdnssd/mdnssd.h>
#  elif __has_include("mdnssd.h")
#    include "mdnssd.h"
#  else
#    error "mdnssd headers not found – install libmdns or vendor mdnssd.h"
#  endif
#else
#  include <mdnssd/mdnssd.h>
#endif
}
#endif

struct Speaker {
    std::string name;
    std::string ip;
    int port = 0;
    std::string id;
    std::string hostname;
};

class Discovery {
public:
    using Callback = std::function<void(const std::vector<Speaker>&)>;

    Discovery();
    ~Discovery();

    void start(Callback callback);
    void stop();

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
                        uint16_t port,
                        const unsigned char* txtRecord,
                        uint16_t txtLen);

    void removeService(const std::string& fullname);
    static std::string makeFullName(const std::string& serviceName,
                                    const std::string& regtype,
                                    const std::string& domain);
    std::map<std::string, Speaker> speakers_;
#else
    mdnssd_handle_s* handle_;
#endif

    std::atomic<bool> running_;
    std::thread thread_;
    Callback callback_;
    std::mutex mutex_;
};
