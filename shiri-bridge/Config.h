#pragma once

#include <string>
#include <vector>
#include <fstream>
#include <iostream>
#include "json.hpp"

using json = nlohmann::json;

struct Speaker {
    std::string ip;
    std::string name;
    int port = 5000;
};

struct AppConfig {
    std::vector<Speaker> speakers;
    std::string pipePath = "/tmp/shiri_audio_pipe";
    int apiPort = 8080;
    int bufferDurationMs = 2000; // Default 2s sync buffer
};

class Config {
public:
    static AppConfig load(const std::string& path) {
        AppConfig config;
        std::ifstream file(path);
        if (!file.is_open()) {
            std::cerr << "Config file not found: " << path << ". Using defaults." << std::endl;
            return config;
        }

        try {
            json j;
            file >> j;

            if (j.contains("pipe_path")) config.pipePath = j["pipe_path"];
            if (j.contains("api_port")) config.apiPort = j["api_port"];
            if (j.contains("buffer_duration_ms")) config.bufferDurationMs = j["buffer_duration_ms"];

            if (j.contains("speakers") && j["speakers"].is_array()) {
                for (const auto& item : j["speakers"]) {
                    Speaker s;
                    if (item.contains("ip")) s.ip = item["ip"];
                    if (item.contains("name")) s.name = item["name"];
                    if (item.contains("port")) s.port = item["port"];
                    config.speakers.push_back(s);
                }
            }
        } catch (const std::exception& e) {
            std::cerr << "Error parsing config: " << e.what() << std::endl;
        }
        return config;
    }
};

