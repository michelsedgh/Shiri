#include <iostream>
#include <thread>
#include <chrono>
#include "Bridge.h"
#include "Config.h"
#include "httplib.h"

int main(int argc, char* argv[]) {
    std::cout << "Shiri Bridge Starting..." << std::endl;

    // Load Config
    std::string configPath = "config.json";
    if (argc > 1) configPath = argv[1];
    
    AppConfig config = Config::load(configPath);
    
    // Start Bridge
    Bridge bridge(config);
    bridge.start();

    // Start HTTP API
    httplib::Server svr;
    
    svr.Get("/status", [&](const httplib::Request&, httplib::Response& res) {
        json j;
        j["status"] = "running";
        j["speakers_count"] = config.speakers.size();
        res.set_content(j.dump(), "application/json");
    });

    svr.Post("/api/speak", [&](const httplib::Request& req, httplib::Response& res) {
        // TODO: Implement TTS injection
        res.set_content("{\"status\":\"ok\"}", "application/json");
    });

    svr.Post("/api/volume", [&](const httplib::Request& req, httplib::Response& res) {
        try {
            json j = json::parse(req.body);
            if (j.contains("volume")) {
                float vol = j["volume"];
                bridge.setVolume(vol);
                res.set_content("{\"status\":\"ok\"}", "application/json");
            } else {
                res.status = 400;
                res.set_content("{\"error\":\"missing volume\"}", "application/json");
            }
        } catch (...) {
            res.status = 400;
            res.set_content("{\"error\":\"invalid json\"}", "application/json");
        }
    });

    std::cout << "API listening on port " << config.apiPort << std::endl;
    svr.listen("0.0.0.0", config.apiPort);

    return 0;
}
