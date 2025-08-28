package ssdp

import (
    "context"
    "net"
    "strings"
    "time"

    "github.com/grandcat/zeroconf"
)

// Device represents a simple SSDP discovery result.
type Device struct {
    Location string
    Server   string
    ST       string
    USN      string
    Addr     string
    Port     int
    Friendly string
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

// DiscoverRAOP discovers RAOP/AirPlay receivers (_raop._tcp / _airplay._tcp) via mDNS.
// It binds the query to the provided interface IP so we only see devices on that LAN.
func DiscoverRAOP(bindIP string, timeout time.Duration) ([]Device, error) {
    // RAOP service carries the MAC and service name in instance; we mostly need IP:port
    ctx, cancel := context.WithTimeout(context.Background(), timeout)
    defer cancel()
    r, err := zeroconf.NewResolver(nil)
    if err != nil { return nil, err }
    entries := make(chan *zeroconf.ServiceEntry)
    var out []Device
    subnet := ipNetForIP(bindIP)
    go func() {
        for e := range entries {
            if len(e.AddrIPv4) > 0 {
                ip := e.AddrIPv4[0]
                if subnet != nil && !subnet.Contains(ip) { continue }
                friendly := friendlyFromInstance(e.Instance)
                out = append(out, Device{Addr: ip.String(), Port: e.Port, ST: "_raop._tcp", USN: e.Instance, Friendly: friendly, Location: e.Instance})
            }
        }
    }()
    // Browse RAOP; we could also browse _airplay._tcp for AP2 but RAOP is our sender path
    if err := r.Browse(ctx, "_raop._tcp", "local.", entries); err != nil {
        // Do not close(entries) here; nothing has been started yet, but avoid double-close patterns.
        return nil, err
    }
    <-ctx.Done()
    // Allow the producer side to close the channel; avoid explicit close to prevent panic on double close
    return out, nil
}

func friendlyFromInstance(instance string) string {
    // RAOP instance is typically "<MAC>@<Device Name>"
    if idx := strings.LastIndex(instance, "@"); idx != -1 && idx+1 < len(instance) {
        return strings.TrimSpace(instance[idx+1:])
    }
    return instance
}

func interfaceNameForIP(ip string) string {
    ifaces, _ := net.Interfaces()
    for _, ifi := range ifaces {
        addrs, _ := ifi.Addrs()
        for _, a := range addrs {
            if ipn, ok := a.(*net.IPNet); ok && ipn.IP.To4() != nil {
                if ipn.IP.String() == ip { return ifi.Name }
            }
        }
    }
    return ""
}

func ipNetForIP(ip string) *net.IPNet {
    ifaces, _ := net.Interfaces()
    for _, ifi := range ifaces {
        addrs, _ := ifi.Addrs()
        for _, a := range addrs {
            if ipn, ok := a.(*net.IPNet); ok && ipn.IP.To4() != nil {
                if ipn.IP.String() == ip { return &net.IPNet{IP: ipn.IP.Mask(ipn.Mask), Mask: ipn.Mask} }
            }
        }
    }
    return nil
}


