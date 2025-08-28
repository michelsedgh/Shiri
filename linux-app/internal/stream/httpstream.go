package stream

import (
    "bufio"
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
    src     *Broadcaster
}

// NewHTTPStreamer creates a streamer bound to host:port.
func NewHTTPStreamer(addr string, src *Broadcaster) *HTTPStreamer {
    hs := &HTTPStreamer{conns: make(map[net.Conn]struct{}), src: src}
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
    // Pump from broadcaster subscription to the hijacked connection
    go func() {
        defer func() {
            h.mu.Lock()
            delete(h.conns, conn)
            h.mu.Unlock()
            _ = conn.Close()
        }()
        wr := bufio.NewWriter(conn)
        ch := h.src.Subscribe()
        for buf := range ch {
            if _, werr := wr.Write(buf); werr != nil { return }
            if err := wr.Flush(); err != nil { return }
        }
    }()
}

// handleStreamChunked serves the stream using standard chunked transfer encoding
// (no hijacking). This increases compatibility with some UPnP renderers.
func (h *HTTPStreamer) handleStreamChunked(w http.ResponseWriter, r *http.Request) {
    w.Header().Set("Content-Type", "audio/mpeg")
    // Let net/http choose chunked encoding automatically for HTTP/1.1
    // by not setting Content-Length and not hijacking.
    ch := h.src.Subscribe()
    for buf := range ch {
        if _, werr := w.Write(buf); werr != nil { return }
        if f, ok := w.(http.Flusher); ok { f.Flush() }
    }
}


