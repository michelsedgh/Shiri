package rooms

import (
    "fmt"
    "io"
    "log"
    "os"
    "path/filepath"
    "sync"

    "shiri-linux/internal/containers"
    "shiri-linux/internal/encode"
    "shiri-linux/internal/engine"
    "shiri-linux/internal/fifo"
    "shiri-linux/internal/stream"
)

// Supervisor manages per-room pipelines: containerized shairport -> ffmpeg -> HTTP
type Supervisor struct {
    mu   sync.Mutex
    mgr  *containers.Manager
    procs map[string]*roomProc
}

type roomProc struct {
    ContainerName string
    FIFOBase      string
    Encoder       *encode.FFMpegEncoder
    HTTP          *stream.HTTPStreamer
}

func NewSupervisor(kind engine.EngineKind) *Supervisor {
    return &Supervisor{mgr: containers.NewManager(kind), procs: make(map[string]*roomProc)}
}

// StartRoom ensures FIFOs, starts container, and encoder.
func (s *Supervisor) StartRoom(roomID, airplayName, networkName, httpBind string) error {
    s.mu.Lock()
    defer s.mu.Unlock()
    if _, ok := s.procs[roomID]; ok { return nil }

    // FIFOs under /tmp/shiri-rooms/<roomID>
    base := filepath.Join("/tmp", "shiri-rooms", roomID)
    if err := fifo.Ensure(base); err != nil { return err }

    // Start container
    cname := "sps-" + roomID
    if _, err := s.mgr.RunShairportRoom(cname, airplayName, base, networkName, nil); err != nil {
        return fmt.Errorf("start shairport: %w", err)
    }

    // Start encoder (mp3)
    enc, err := encode.StartMP3()
    if err != nil { return err }
    // Pump PCM from FIFO into encoder stdin
    go func() {
        f, err := os.Open(filepath.Join(base, "audio"))
        if err != nil { log.Printf("open fifo: %v", err); return }
        defer f.Close()
        _, _ = io.Copy(enc.Stdin, f)
        _ = enc.Stdin.Close()
    }()

    // Start HTTP streamer bound to selected NIC/port
    httpIn := enc.Stdout
    hs := stream.NewHTTPStreamer(httpBind, httpIn)
    go func() {
        if err := hs.Start(); err != nil { log.Printf("http streamer: %v", err) }
    }()

    s.procs[roomID] = &roomProc{ContainerName: cname, FIFOBase: base, Encoder: enc, HTTP: hs}
    return nil
}

func (s *Supervisor) StopRoom(roomID string) error {
    s.mu.Lock()
    defer s.mu.Unlock()
    rp, ok := s.procs[roomID]
    if !ok { return nil }
    _ = s.mgr.Stop(rp.ContainerName)
    if rp.Encoder != nil && rp.Encoder.Cmd != nil {
        _ = rp.Encoder.Cmd.Process.Kill()
    }
    delete(s.procs, roomID)
    return nil
}

func (s *Supervisor) Logs(roomID string, tail int) (string, error) {
    s.mu.Lock()
    rp, ok := s.procs[roomID]
    s.mu.Unlock()
    if !ok { return "", fmt.Errorf("room not running") }
    return s.mgr.Logs(rp.ContainerName, tail)
}


