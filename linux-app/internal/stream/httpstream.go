package stream

import (
    "bufio"
    "io"
    "log"
    "net"
    "net/http"
    "sync"
)

// HTTPStreamer serves a raw (or encoded) audio stream per room to many clients.
type HTTPStreamer struct {
    mu      sync.Mutex
    conns   map[net.Conn]struct{}
    srv     *http.Server
    inputR  io.Reader
}

// NewHTTPStreamer creates a streamer bound to host:port.
func NewHTTPStreamer(addr string, input io.Reader) *HTTPStreamer {
    hs := &HTTPStreamer{conns: make(map[net.Conn]struct{}), inputR: input}
    mux := http.NewServeMux()
    mux.HandleFunc("/stream", hs.handleStream)
    mux.HandleFunc("/stream.mp3", hs.handleStreamChunked)
    hs.srv = &http.Server{Addr: addr, Handler: mux}
    return hs
}

// Start begins serving; it does not return.
func (h *HTTPStreamer) Start() error {
    return h.srv.ListenAndServe()
}

func (h *HTTPStreamer) handleStream(w http.ResponseWriter, r *http.Request) {
    w.Header().Set("Content-Type", "audio/mpeg")
    w.WriteHeader(200)
    hj, ok := w.(http.Hijacker)
    if !ok {
        http.Error(w, "hijack not supported", http.StatusInternalServerError)
        return
    }
    conn, _, err := hj.Hijack()
    if err != nil {
        return
    }
    h.mu.Lock()
    h.conns[conn] = struct{}{}
    h.mu.Unlock()
    // Pump input when available (this simplistic version reads shared input)
    go func() {
        defer func() {
            h.mu.Lock()
            delete(h.conns, conn)
            h.mu.Unlock()
            _ = conn.Close()
        }()
        rd := bufio.NewReader(h.inputR)
        wr := bufio.NewWriter(conn)
        buf := make([]byte, 16384)
        for {
            n, err := rd.Read(buf)
            if n > 0 {
                if _, werr := wr.Write(buf[:n]); werr != nil { return }
                if err := wr.Flush(); err != nil { return }
            }
            if err != nil {
                if err != io.EOF { log.Printf("stream read err: %v", err) }
                return
            }
        }
    }()
}

// handleStreamChunked serves the stream using standard chunked transfer encoding
// (no hijacking). This increases compatibility with some UPnP renderers.
func (h *HTTPStreamer) handleStreamChunked(w http.ResponseWriter, r *http.Request) {
    w.Header().Set("Content-Type", "audio/mpeg")
    // Let net/http choose chunked encoding automatically for HTTP/1.1
    // by not setting Content-Length and not hijacking.
    buf := make([]byte, 16384)
    rd := bufio.NewReader(h.inputR)
    for {
        n, err := rd.Read(buf)
        if n > 0 {
            if _, werr := w.Write(buf[:n]); werr != nil { return }
            if f, ok := w.(http.Flusher); ok { f.Flush() }
        }
        if err != nil {
            if err != io.EOF { log.Printf("stream chunked read err: %v", err) }
            return
        }
    }
}


