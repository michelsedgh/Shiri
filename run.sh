#!/bin/bash

PIPE=/tmp/shiri_audio_pipe
BUILD_DIR=build

# Ensure build dir exists
if [ ! -d "$BUILD_DIR" ]; then
    echo "Build directory missing. Please run cmake and make."
    exit 1
fi

# Create named pipe
if [[ ! -p $PIPE ]]; then
    echo "Creating named pipe at $PIPE"
    mkfifo $PIPE
    chmod 666 $PIPE
fi

# Check if config exists
if [ ! -f "config.json" ]; then
    echo "Creating default config.json"
    echo '{
        "pipe_path": "/tmp/shiri_audio_pipe",
        "api_port": 8080,
        "speakers": [
            { "ip": "192.168.1.10", "port": 5000, "name": "Speaker 1" }
        ]
    }' > config.json
fi

echo "Starting Shiri Bridge..."
./$BUILD_DIR/shiri-bridge config.json

