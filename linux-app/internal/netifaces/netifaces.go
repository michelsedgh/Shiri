package netifaces

import (
    "net"
)

// Interface describes a network interface with IPv4 addresses.
type Interface struct {
    Name string
    IPv4 []string
}

// List enumerates system interfaces and collects IPv4 addresses.
func List() []Interface {
    var out []Interface
    ifs, err := net.Interfaces()
    if err != nil {
        return out
    }
    for _, ni := range ifs {
        addrs, _ := ni.Addrs()
        var v4s []string
        for _, a := range addrs {
            if ipn, ok := a.(*net.IPNet); ok {
                ip := ipn.IP.To4()
                if ip != nil {
                    v4s = append(v4s, ip.String())
                }
            }
        }
        if len(v4s) > 0 {
            out = append(out, Interface{Name: ni.Name, IPv4: v4s})
        }
    }
    return out
}

// FirstIPv4 returns the first IPv4 of a named interface.
func FirstIPv4(name string) (string, bool) {
    ifs := List()
    for _, i := range ifs {
        if i.Name == name && len(i.IPv4) > 0 {
            return i.IPv4[0], true
        }
    }
    return "", false
}


