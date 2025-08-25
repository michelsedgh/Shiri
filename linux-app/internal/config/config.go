package config

import (
    "encoding/json"
    "errors"
    "fmt"
    "os"
    "path/filepath"
)

const (
    appDirName  = "shiri-linux"
    configName  = "config.json"
    configPerm  = 0o644
    configDirPerm = 0o755
)

// Config is the persisted application configuration.
type Config struct {
    Rooms []RoomConfig `json:"rooms"`
}

// RoomConfig describes a per-room AirPlay endpoint and its targets.
type RoomConfig struct {
    Name                  string   `json:"name"`
    AirplayName           string   `json:"airplayName"`
    BindInterfaceAirplay  string   `json:"bindInterfaceAirplay"`
    BindInterfaceSpeakers string   `json:"bindInterfaceSpeakers"`
    TargetDeviceIDs       []string `json:"targetDeviceIds"`
}

// Load reads the configuration from disk or returns a default config if missing.
func Load() (*Config, error) {
    path, err := path()
    if err != nil {
        return defaultConfig(), err
    }
    b, err := os.ReadFile(path)
    if err != nil {
        if errors.Is(err, os.ErrNotExist) {
            // Ensure directory exists and write default config
            if err := ensureDir(); err != nil {
                return defaultConfig(), err
            }
            cfg := defaultConfig()
            _ = Save(cfg)
            return cfg, nil
        }
        return defaultConfig(), err
    }
    var cfg Config
    if err := json.Unmarshal(b, &cfg); err != nil {
        return defaultConfig(), fmt.Errorf("invalid config json: %w", err)
    }
    return &cfg, nil
}

// Save writes the configuration to disk.
func Save(cfg *Config) error {
    if err := ensureDir(); err != nil {
        return err
    }
    b, err := json.MarshalIndent(cfg, "", "  ")
    if err != nil {
        return err
    }
    p, err := path()
    if err != nil {
        return err
    }
    return os.WriteFile(p, b, configPerm)
}

func ensureDir() error {
    dir, err := dir()
    if err != nil {
        return err
    }
    return os.MkdirAll(dir, configDirPerm)
}

func dir() (string, error) {
    base, err := os.UserConfigDir()
    if err != nil {
        return "", err
    }
    return filepath.Join(base, appDirName), nil
}

func path() (string, error) {
    d, err := dir()
    if err != nil {
        return "", err
    }
    return filepath.Join(d, configName), nil
}

func defaultConfig() *Config {
    return &Config{Rooms: []RoomConfig{}}
}


