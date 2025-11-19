#pragma once

#include <string>
#include <thread>
#include <atomic>
#include <functional>
#include <vector>
#include <iostream>
#include <unistd.h>
#include <fcntl.h>
#include <chrono>

class PipeReader {
public:
    using DataCallback = std::function<void(const uint8_t* data, size_t size)>;

    PipeReader(const std::string& path, size_t chunkSize = 4096) 
        : path_(path), chunkSize_(chunkSize), running_(false) {}

    ~PipeReader() {
        stop();
    }

    void start(DataCallback cb) {
        if (running_) return;
        callback_ = cb;
        running_ = true;
        worker_ = std::thread(&PipeReader::run, this);
    }

    void stop() {
        running_ = false;
        if (worker_.joinable()) worker_.join();
    }

private:
    void run() {
        std::cout << "PipeReader: Waiting for pipe at " << path_ << std::endl;
        
        while (running_) {
            // Open blocks until a writer connects (unless O_NONBLOCK, but we want blocking wait)
            // However, if the file doesn't exist, open fails immediately.
            int fd = open(path_.c_str(), O_RDONLY);
            if (fd < 0) {
                std::this_thread::sleep_for(std::chrono::seconds(1));
                continue;
            }

            std::cout << "PipeReader: Pipe connected." << std::endl;
            std::vector<uint8_t> buffer(chunkSize_);
            
            while (running_) {
                ssize_t bytesRead = read(fd, buffer.data(), chunkSize_);
                if (bytesRead > 0) {
                    if (callback_) callback_(buffer.data(), bytesRead);
                } else if (bytesRead == 0) {
                    // EOF: Writer closed the pipe.
                    // Loop back to open() to wait for new connection.
                    std::cout << "PipeReader: EOF (Writer disconnected)." << std::endl;
                    break;
                } else {
                    // Error
                    perror("Pipe read error");
                    std::this_thread::sleep_for(std::chrono::milliseconds(10));
                }
            }
            close(fd);
        }
        std::cout << "PipeReader: Thread exiting." << std::endl;
    }

    std::string path_;
    size_t chunkSize_;
    std::atomic<bool> running_;
    std::thread worker_;
    DataCallback callback_;
};

