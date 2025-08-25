package ssdp

import (
    "net"
    "strings"
    "time"
)

// Device represents a simple SSDP discovery result.
type Device struct {
    Location string
    Server   string
    ST       string
    USN      string
    Addr     string
}

// Discover sends M-SEARCH on the given interface IPv4 and returns responses.
func Discover(bindIP string, st string, timeout time.Duration) ([]Device, error) {
    // UDP socket bound to the chosen interface IP
    laddr, err := net.ResolveUDPAddr("udp4", bindIP+":0")
    if err != nil { return nil, err }
    conn, err := net.ListenUDP("udp4", laddr)
    if err != nil { return nil, err }
    defer conn.Close()

    // SSDP multicast address
    raddr, _ := net.ResolveUDPAddr("udp4", "239.255.255.250:1900")
    req := "M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 1\r\nST: "+st+"\r\n\r\n"
    _, _ = conn.WriteToUDP([]byte(req), raddr)

    deadline := time.Now().Add(timeout)
    _ = conn.SetReadDeadline(deadline)
    buf := make([]byte, 2048)
    var out []Device
    for {
        n, addr, err := conn.ReadFromUDP(buf)
        if err != nil { break }
        resp := string(buf[:n])
        dev := Device{Addr: addr.IP.String()}
        for _, line := range strings.Split(resp, "\r\n") {
            low := strings.ToLower(line)
            if strings.HasPrefix(low, "location:") {
                dev.Location = strings.TrimSpace(line[len("location:"):])
            } else if strings.HasPrefix(low, "server:") {
                dev.Server = strings.TrimSpace(line[len("server:"):])
            } else if strings.HasPrefix(low, "st:") {
                dev.ST = strings.TrimSpace(line[len("st:"):])
            } else if strings.HasPrefix(low, "usn:") {
                dev.USN = strings.TrimSpace(line[len("usn:"):])
            }
        }
        out = append(out, dev)
    }
    return out, nil
}


