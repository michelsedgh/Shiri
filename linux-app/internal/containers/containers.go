package containers

import (
    "encoding/json"
    "fmt"
    "strconv"
    "strings"
    "time"

    "shiri-linux/internal/engine"
    "shiri-linux/internal/runner"
)

// Manager issues container commands via docker/podman CLI.
type Manager struct {
    Engine engine.EngineKind
}

func NewManager(kind engine.EngineKind) *Manager {
    return &Manager{Engine: kind}
}

func (m *Manager) bin() string {
    switch m.Engine {
    case engine.EnginePodman:
        return "podman"
    default:
        return "docker"
    }
}

// RunShairportRoom launches a shairport-sync container for a room.
// volumeHost is the host dir with named pipes {audio,metadata}.
// If networkName is non-empty, attaches container to that network.
func (m *Manager) RunShairportRoom(name, airplayName, volumeHost, networkName string, extraArgs []string) (string, error) {
    bin := m.bin()
    args := []string{
        "run", "-d", "--restart=unless-stopped",
        "--name", name,
        // Capabilities improve timing (rtprio) and avoid permission errors in NQPTP/AP2
        "--cap-add", "SYS_NICE",
        "--cap-add", "NET_ADMIN",
        "--cap-add", "SYS_RESOURCE",
        "-v", fmt.Sprintf("%s:/tmp/shairport", volumeHost),
    }
    if networkName != "" {
        args = append(args, "--network", networkName)
    }
    // Image and shairport args (enable verbose logs and basic stats for easier debugging)
    shArgs := []string{"mikebrady/shairport-sync:latest", "-vv", "--statistics", "-a", airplayName, "-o", "pipe", "-M", "--metadata-pipename=/tmp/shairport/metadata", "--", "/tmp/shairport/audio"}
    if len(extraArgs) > 0 {
        shArgs = append(shArgs, extraArgs...)
    }
    args = append(args, shArgs...)
    res := runner.Run(15*time.Second, bin, args...)
    if res.Err != nil {
        return "", fmt.Errorf("run failed: %v: %s", res.Err, string(res.Stderr))
    }
    id := strings.TrimSpace(string(res.Stdout))
    return id, nil
}

func (m *Manager) Stop(name string) error {
    bin := m.bin()
    res := runner.Run(10*time.Second, bin, "stop", name)
    if res.Err != nil {
        return fmt.Errorf("stop failed: %v: %s", res.Err, string(res.Stderr))
    }
    _ = runner.Run(10*time.Second, bin, "rm", name)
    return nil
}

type ContainerInfo struct {
    ID    string `json:"ID"`
    Image string `json:"Image"`
    Names string `json:"Names"`
    State string `json:"State"`
    Status string `json:"Status"`
}

func (m *Manager) PS() ([]ContainerInfo, error) {
    bin := m.bin()
    res := runner.Run(10*time.Second, bin, "ps", "--format", "{{json .}}")
    if res.Err != nil {
        return nil, fmt.Errorf("ps failed: %v: %s", res.Err, string(res.Stderr))
    }
    lines := strings.Split(strings.TrimSpace(string(res.Stdout)), "\n")
    var out []ContainerInfo
    for _, ln := range lines {
        if strings.TrimSpace(ln) == "" { continue }
        var x ContainerInfo
        if err := json.Unmarshal([]byte(ln), &x); err == nil {
            out = append(out, x)
        }
    }
    return out, nil
}

// Logs returns last N lines of container logs.
func (m *Manager) Logs(name string, tail int) (string, error) {
    bin := m.bin()
    if tail <= 0 { tail = 200 }
    res := runner.Run(10*time.Second, bin, "logs", "--tail", strconv.Itoa(tail), name)
    if res.Err != nil {
        return "", fmt.Errorf("logs failed: %v: %s", res.Err, string(res.Stderr))
    }
    return string(res.Stdout), nil
}


