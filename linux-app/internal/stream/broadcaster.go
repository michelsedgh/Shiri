package stream

import (
    "io"
    "log"
    "sync"
)

// Broadcaster fans out a single input stream to many consumers.
type Broadcaster struct {
    mu       sync.Mutex
    chans    map[chan []byte]struct{}
    closed   bool
}

func NewBroadcaster() *Broadcaster {
    return &Broadcaster{chans: make(map[chan []byte]struct{})}
}

// Attach starts reading from r and broadcasting to clients until EOF or error.
func (b *Broadcaster) Attach(r io.Reader) {
    go func() {
        buf := make([]byte, 32*1024)
        for {
            n, err := r.Read(buf)
            if n > 0 {
                b.mu.Lock()
                for ch := range b.chans {
                    // non-blocking send: drop if receiver is slow
                    select {
                    case ch <- append([]byte(nil), buf[:n]...):
                    default:
                    }
                }
                b.mu.Unlock()
            }
            if err != nil {
                if err != io.EOF { log.Printf("broadcast read error: %v", err) }
                b.mu.Lock()
                for ch := range b.chans { close(ch) }
                b.chans = make(map[chan []byte]struct{})
                b.closed = true
                b.mu.Unlock()
                return
            }
        }
    }()
}

// Subscribe returns a channel receiving byte chunks.
func (b *Broadcaster) Subscribe() <-chan []byte {
    ch := make(chan []byte, 16)
    b.mu.Lock()
    if b.closed {
        close(ch)
    } else {
        b.chans[ch] = struct{}{}
    }
    b.mu.Unlock()
    return ch
}

// Unsubscribe removes a channel.
func (b *Broadcaster) Unsubscribe(ch chan []byte) {
    b.mu.Lock()
    delete(b.chans, ch)
    close(ch)
    b.mu.Unlock()
}

// Feed reads from r and writes to a writer function, useful for bridging to process stdin
func (b *Broadcaster) Feed(write func([]byte) error) {
    go func() {
        ch := b.Subscribe()
        for buf := range ch {
            if err := write(buf); err != nil { return }
        }
    }()
}


