// Package deprecated: UPnP control removed for RAOP-only build.
package upnp

import (
    "encoding/xml"
    "fmt"
    "io"
    "net/http"
    "net/url"
    "path"
    "strings"
    "time"
)

type deviceDesc struct {
    XMLName xml.Name `xml:"root"`
    Device  struct {
        FriendlyName string `xml:"friendlyName"`
        Services     []struct {
            ServiceType string `xml:"serviceType"`
            ControlURL  string `xml:"controlURL"`
        } `xml:"serviceList>service"`
    } `xml:"device"`
}

// ResolveAVTransportControlURL returns the absolute control URL for AVTransport and a friendly name.
func ResolveAVTransportControlURL(locationURL string) (controlURL string, friendly string, err error) {
    client := &http.Client{Timeout: 3 * time.Second}
    resp, err := client.Get(locationURL)
    if err != nil { return "", "", err }
    defer resp.Body.Close()
    if resp.StatusCode >= 300 { b, _ := io.ReadAll(resp.Body); return "", "", fmt.Errorf("desc http %d: %s", resp.StatusCode, string(b)) }
    var dd deviceDesc
    if err := xml.NewDecoder(resp.Body).Decode(&dd); err != nil { return "", "", err }
    base, err := url.Parse(locationURL)
    if err != nil { return "", "", err }
    for _, s := range dd.Device.Services {
        if strings.HasPrefix(s.ServiceType, "urn:schemas-upnp-org:service:AVTransport:") {
            // Build absolute URL
            cu := s.ControlURL
            if strings.HasPrefix(cu, "http://") || strings.HasPrefix(cu, "https://") {
                return cu, dd.Device.FriendlyName, nil
            }
            // Relative
            // Some devices use absolute path, others relative; join with base path
            u := *base
            if strings.HasPrefix(cu, "/") {
                u.Path = cu
            } else {
                u.Path = path.Join(path.Dir(base.Path), cu)
            }
            return u.String(), dd.Device.FriendlyName, nil
        }
    }
    return "", dd.Device.FriendlyName, fmt.Errorf("AVTransport not found")
}


