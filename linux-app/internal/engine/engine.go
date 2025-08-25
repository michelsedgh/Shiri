package engine

import (
    "fmt"
    "os"
)

// EngineKind indicates the container runtime.
type EngineKind int

const (
    EngineNone EngineKind = iota
    EngineDocker
    EnginePodman
)

func (e EngineKind) String() string {
    switch e {
    case EngineDocker:
        return "docker"
    case EnginePodman:
        return "podman"
    default:
        return "none"
    }
}

// Detect checks environment for Docker/Podman client availability.
func Detect() EngineKind {
    if _, err := os.Stat("/var/run/docker.sock"); err == nil {
        return EngineDocker
    }
    if _, err := os.Stat("/run/podman/podman.sock"); err == nil {
        return EnginePodman
    }
    // Fallback: check client binaries in PATH
    if _, err := lookup("docker"); err == nil {
        return EngineDocker
    }
    if _, err := lookup("podman"); err == nil {
        return EnginePodman
    }
    return EngineNone
}

func lookup(bin string) (string, error) {
    for _, dir := range filepathList() {
        p := dir + string(os.PathSeparator) + bin
        if st, err := os.Stat(p); err == nil && !st.IsDir() {
            return p, nil
        }
    }
    return "", fmt.Errorf("%s not found", bin)
}

func filepathList() []string {
    path := os.Getenv("PATH")
    if path == "" {
        return nil
    }
    sep := ':'
    // PATH is ':'-separated on Linux
    var dirs []string
    start := 0
    for i := 0; i <= len(path); i++ {
        if i == len(path) || path[i] == byte(sep) {
            if i > start {
                dirs = append(dirs, path[start:i])
            }
            start = i + 1
        }
    }
    return dirs
}


