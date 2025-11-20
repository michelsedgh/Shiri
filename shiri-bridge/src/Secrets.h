#pragma once

#include <string>
#include <map>
#include <mutex>
#include <fstream>
#include <iostream>
#include "json.hpp"

using json = nlohmann::json;

class SecretsManager {
public:
    static SecretsManager& instance() {
        static SecretsManager instance;
        return instance;
    }

    void setSecret(const std::string& deviceId, const std::string& secret) {
        std::lock_guard<std::mutex> lock(mutex_);
        secrets_[deviceId] = secret;
        save();
    }

    std::string getSecret(const std::string& deviceId) {
        std::lock_guard<std::mutex> lock(mutex_);
        auto it = secrets_.find(deviceId);
        if (it != secrets_.end()) {
            return it->second;
        }
        return "";
    }

private:
    SecretsManager() {
        load();
    }

    void load() {
        std::ifstream file("secrets.json");
        if (file.is_open()) {
            try {
                json j;
                file >> j;
                for (auto& element : j.items()) {
                    secrets_[element.key()] = element.value().get<std::string>();
                }
            } catch (...) {
                // ignore errors
            }
        }
    }

    void save() {
        json j;
        for (const auto& pair : secrets_) {
            j[pair.first] = pair.second;
        }
        std::ofstream file("secrets.json");
        file << j.dump(4);
    }

    std::map<std::string, std::string> secrets_;
    std::mutex mutex_;
};

