package netsetup

import (
    "fmt"
    "net"
    "os"
    "strings"
    "time"

    "shiri-linux/internal/engine"
    "shiri-linux/internal/runner"
)

// NetworkName returns a deterministic name for a macvlan network on an interface.
func NetworkName(iface string) string { return "shiri-macvlan-" + iface }

// EnsureMacvlanNetwork ensures a macvlan network exists for the given parent interface.
// It derives the subnet from the interface's IPv4 address. Returns the network name.
func EnsureMacvlanNetwork(kind engine.EngineKind, parentIface string) (string, error) {
    ipnet, err := firstIPv4(parentIface)
    if err != nil { return "", err }
    subnet := cidrFromIPNet(ipnet)
    name := NetworkName(parentIface)
    bin := "docker"
    if kind == engine.EnginePodman { bin = "podman" }

    // Check if exists
    if kind == engine.EnginePodman {
        res := runner.Run(5*time.Second, bin, "network", "ls", "--format", "json")
        if res.Err == nil && strings.Contains(string(res.Stdout), "\"name\":\""+name+"\"") {
            return name, nil
        }
    } else {
        res := runner.Run(5*time.Second, bin, "network", "ls", "--format", "{{.Name}}")
        if res.Err == nil {
            for _, ln := range strings.Split(strings.TrimSpace(string(res.Stdout)), "\n") {
                if strings.TrimSpace(ln) == name { return name, nil }
            }
        }
    }

    // Try to determine gateway for the parent interface (optional but recommended)
    gw := defaultGatewayForInterface(parentIface)

    // Create
    if kind == engine.EnginePodman {
        args := []string{"network", "create", "--driver", "macvlan", "-o", "parent="+parentIface, "--subnet", subnet}
        if gw != "" {
            args = append(args, "--gateway", gw)
        }
        args = append(args, name)
        if r := runner.Run(10*time.Second, bin, args...); r.Err != nil {
            return "", fmt.Errorf("podman network create failed: %v: %s", r.Err, string(r.Stderr))
        }
    } else {
        args := []string{"network", "create", "-d", "macvlan", "-o", "parent="+parentIface, "--subnet", subnet}
        if gw != "" {
            args = append(args, "--gateway", gw)
        }
        args = append(args, name)
        if r := runner.Run(10*time.Second, bin, args...); r.Err != nil {
            return "", fmt.Errorf("docker network create failed: %v: %s", r.Err, string(r.Stderr))
        }
    }
    return name, nil
}

func firstIPv4(iface string) (*net.IPNet, error) {
    ni, err := net.InterfaceByName(iface)
    if err != nil { return nil, err }
    addrs, err := ni.Addrs()
    if err != nil { return nil, err }
    for _, a := range addrs {
        if ipn, ok := a.(*net.IPNet); ok {
            if v4 := ipn.IP.To4(); v4 != nil { return &net.IPNet{IP: v4, Mask: ipn.Mask}, nil }
        }
    }
    return nil, fmt.Errorf("no IPv4 on %s", iface)
}

func cidrFromIPNet(ipnet *net.IPNet) string {
    masked := ipnet.IP.Mask(ipnet.Mask)
    ones, _ := ipnet.Mask.Size()
    return fmt.Sprintf("%s/%d", masked.String(), ones)
}

// defaultGatewayForInterface returns the default gateway IP for the given interface,
// or an empty string if it can not be determined. It shells out to `ip route` to avoid
// adding extra dependencies.
func defaultGatewayForInterface(iface string) string {
    r := runner.Run(2*time.Second, "ip", "route", "show", "dev", iface)
    if r.Err != nil { return "" }
    for _, ln := range strings.Split(string(r.Stdout), "\n") {
        s := strings.TrimSpace(ln)
        if strings.HasPrefix(s, "default ") {
            f := strings.Fields(s)
            for i := 0; i < len(f)-1; i++ {
                if f[i] == "via" {
                    return f[i+1]
                }
            }
        }
    }
    return ""
}

// IsWireless reports whether the interface is a Wi‑Fi interface.
// On Linux, wireless interfaces have /sys/class/net/<iface>/wireless.
func IsWireless(iface string) bool {
    if iface == "" { return false }
    if _, err := os.Stat("/sys/class/net/" + iface + "/wireless"); err == nil {
        return true
    }
    return false
}


