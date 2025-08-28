package rooms

import (
    "bufio"
    "fmt"
    "io"
    "log"
    "net"
    "os"
    "os/exec"
    "path/filepath"
    "strconv"
    "strings"
    "sync"
    "time"

    "shiri-linux/internal/containers"
    "shiri-linux/internal/encode"
    "shiri-linux/internal/engine"
    "shiri-linux/internal/fifo"
    "shiri-linux/internal/stream"
    "shiri-linux/internal/raopbin"
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
    Broadcaster   *stream.Broadcaster
    MP3Broadcaster *stream.Broadcaster
    RAOPS         []*raopSender
    RAOPLogs      *raopLogBuffer
}

type raopSender struct {
    Target string
    Cmd    *exec.Cmd
    Stdin  io.WriteCloser
}

type raopLogBuffer struct {
    mu    sync.Mutex
    lines []string
    max   int
}

func newRAOPLogBuffer(max int) *raopLogBuffer {
    return &raopLogBuffer{max: max}
}

func (b *raopLogBuffer) appendLine(line string) {
    b.mu.Lock()
    if line == "" { b.mu.Unlock(); return }
    b.lines = append(b.lines, line)
    if len(b.lines) > b.max {
        // drop oldest to keep at most max
        b.lines = b.lines[len(b.lines)-b.max:]
    }
    b.mu.Unlock()
}

func (b *raopLogBuffer) tail(n int) string {
    b.mu.Lock()
    defer b.mu.Unlock()
    if n <= 0 || n > len(b.lines) { n = len(b.lines) }
    start := len(b.lines) - n
    if start < 0 { start = 0 }
    return strings.Join(b.lines[start:], "\n")
}

func NewSupervisor(kind engine.EngineKind) *Supervisor {
    return &Supervisor{mgr: containers.NewManager(kind), procs: make(map[string]*roomProc)}
}

// StartRoom ensures FIFOs, starts container, and encoder.
// If raopPort > 0, it will be passed to shairport-sync with -p to set RTSP port.
func (s *Supervisor) StartRoom(roomID, airplayName, networkName, httpBind string, raopPort int) error {
    s.mu.Lock()
    defer s.mu.Unlock()
    if _, ok := s.procs[roomID]; ok { return nil }

    // FIFOs under /tmp/shiri-rooms/<roomID>
    base := filepath.Join("/tmp", "shiri-rooms", roomID)
    if err := fifo.Ensure(base); err != nil { return err }

    // Start container
    cname := "sps-" + roomID
    var extra []string
    if raopPort > 0 {
        extra = append(extra, "-p", strconv.Itoa(raopPort))
    }
    if _, err := s.mgr.RunShairportRoom(cname, airplayName, base, networkName, extra); err != nil {
        return fmt.Errorf("start shairport: %w", err)
    }

    // Broadcaster reads raw PCM from FIFO and fans it out to encoder and RAOP senders
    b := stream.NewBroadcaster()
    go func() {
        f, err := os.Open(filepath.Join(base, "audio"))
        if err != nil { log.Printf("open fifo: %v", err); return }
        defer f.Close()
        b.Attach(f)
    }()

    // Start encoder (mp3) fed from broadcaster
    enc, err := encode.StartMP3()
    if err != nil { return err }
    go func() {
        ch := b.Subscribe()
        for buf := range ch {
            if _, err := enc.Stdin.Write(buf); err != nil { break }
        }
        _ = enc.Stdin.Close()
    }()

    // MP3 broadcaster for HTTP fan-out (fix concurrent reader issue)
    mp3b := stream.NewBroadcaster()
    go func() {
        mp3b.Attach(enc.Stdout)
    }()
    // Start HTTP streamer bound to selected NIC/port
    hs := stream.NewHTTPStreamer(httpBind, mp3b)
    go func() {
        if err := hs.Start(); err != nil { log.Printf("http streamer: %v", err) }
    }()

    s.procs[roomID] = &roomProc{ContainerName: cname, FIFOBase: base, Encoder: enc, HTTP: hs, Broadcaster: b, MP3Broadcaster: mp3b, RAOPLogs: newRAOPLogBuffer(400)}
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
    s.stopRAOPLocked(rp)
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

// IsRunning reports whether a room pipeline is currently active.
func (s *Supervisor) IsRunning(roomID string) bool {
    s.mu.Lock()
    defer s.mu.Unlock()
    _, ok := s.procs[roomID]
    return ok
}

// StartRAOP launches one raop_play sender per target IP and wires them to the
// room's broadcaster for synchronized playback. Targets must be IPv4/IPv6
// addresses (optionally with :port). bindIP is the local IP to bind.
func (s *Supervisor) StartRAOP(roomID, bindIP string, targets []string) error {
    s.mu.Lock()
    rp, ok := s.procs[roomID]
    s.mu.Unlock()
    if !ok { return fmt.Errorf("room not running") }

    // Prepare a common NTP reference file for group start.
    ntpPath := filepath.Join(rp.FIFOBase, "ntp")
    // Resolve RAOP binary (raop_play/clipraop/bundled)
    raopPath, err := raopbin.Resolve()
    if err != nil {
        return fmt.Errorf("no RAOP binary: %w", err)
    }
    _ = exec.Command(raopPath, "-ntp", ntpPath).Run()

    var senders []*raopSender
    for _, t := range targets {
        host, port, err := splitHostPortDefault(t, "5000")
        if err != nil { log.Printf("raop target skip %s: %v", t, err); continue }
        // Build command: raop_play -i <bindIP> -p <port> -nf <ntp-file> -w 1000 <host> -
        args := []string{"-i", bindIP, "-p", port, "-nf", ntpPath, "-w", "1000", host, "-"}
        cmd := exec.Command(raopPath, args...)
        stdout, _ := cmd.StdoutPipe()
        stderr, _ := cmd.StderrPipe()
        stdin, err := cmd.StdinPipe()
        if err != nil { log.Printf("raop stdin: %v", err); continue }
        if err := cmd.Start(); err != nil { log.Printf("raop start: %v", err); _ = stdin.Close(); continue }
        rs := &raopSender{Target: t, Cmd: cmd, Stdin: stdin}
        senders = append(senders, rs)
        // Capture logs
        go pipeLines(stdout, rp.RAOPLogs)
        go pipeLines(stderr, rp.RAOPLogs)
        // Feed from broadcaster
        go func(w io.WriteCloser) {
            ch := rp.Broadcaster.Subscribe()
            for buf := range ch {
                if _, err := w.Write(buf); err != nil { break }
            }
            _ = w.Close()
        }(stdin)
    }

    if len(senders) == 0 {
        return fmt.Errorf("no RAOP senders started")
    }
    // Give them a moment to buffer before start reference time
    time.Sleep(200 * time.Millisecond)
    s.mu.Lock()
    rp.RAOPS = append(rp.RAOPS, senders...)
    s.mu.Unlock()
    return nil
}

// StopRAOP terminates any RAOP sender processes for the room.
func (s *Supervisor) StopRAOP(roomID string) error {
    s.mu.Lock()
    rp, ok := s.procs[roomID]
    s.mu.Unlock()
    if !ok { return fmt.Errorf("room not running") }
    s.mu.Lock()
    defer s.mu.Unlock()
    s.stopRAOPLocked(rp)
    return nil
}

func (s *Supervisor) stopRAOPLocked(rp *roomProc) {
    for _, r := range rp.RAOPS {
        if r != nil && r.Cmd != nil && r.Cmd.Process != nil {
            _ = r.Cmd.Process.Kill()
        }
    }
    rp.RAOPS = nil
}

// RAOPLogs returns recent logs from RAOP senders for a room.
func (s *Supervisor) RAOPLogs(roomID string, tail int) (string, error) {
    s.mu.Lock()
    rp, ok := s.procs[roomID]
    s.mu.Unlock()
    if !ok { return "", fmt.Errorf("room not running") }
    return rp.RAOPLogs.tail(tail), nil
}

func pipeLines(r io.Reader, buf *raopLogBuffer) {
    if r == nil || buf == nil { return }
    br := bufio.NewScanner(r)
    for br.Scan() {
        buf.appendLine(br.Text())
    }
}

func splitHostPortDefault(addr, defPort string) (host, port string, err error) {
    // Accept legacy formats like "Name|IP" by taking the substring after the last '|'
    addr = strings.TrimSpace(addr)
    if idx := strings.LastIndex(addr, "|"); idx != -1 {
        addr = strings.TrimSpace(addr[idx+1:])
    }
    if strings.Contains(addr, ":") {
        h, p, e := net.SplitHostPort(addr)
        if e == nil { return h, p, nil }
        // Maybe it's IPv6 without brackets or plain host: fallback below
    }
    if net.ParseIP(addr) != nil { return addr, defPort, nil }
    return "", "", fmt.Errorf("invalid address: %s", addr)
}


