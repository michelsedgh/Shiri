package upnp

import (
    "bytes"
    "fmt"
    "io"
    "net/http"
    "strings"
    "time"
)

// SetAVTransportURI sets the playback URL and metadata on a UPnP renderer.
func SetAVTransportURI(controlURL string, url string, meta string) error {
    body := soapEnvelope("SetAVTransportURI", fmt.Sprintf(`
<InstanceID>0</InstanceID>
<CurrentURI>%s</CurrentURI>
<CurrentURIMetaData>%s</CurrentURIMetaData>
`, xmlEscape(url), xmlEscape(meta)))
    _, err := post(controlURL, "urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI", body)
    return err
}

// Play starts playback.
func Play(controlURL string) error {
    body := soapEnvelope("Play", `<InstanceID>0</InstanceID><Speed>1</Speed>`)
    _, err := post(controlURL, "urn:schemas-upnp-org:service:AVTransport:1#Play", body)
    return err
}

func post(url string, action string, xml string) ([]byte, error) {
    req, _ := http.NewRequest("POST", url, bytes.NewBufferString(xml))
    req.Header.Set("Content-Type", "text/xml; charset=\"utf-8\"")
    req.Header.Set("SOAPACTION", fmt.Sprintf("\"%s\"", action))
    client := &http.Client{Timeout: 5 * time.Second}
    resp, err := client.Do(req)
    if err != nil { return nil, err }
    defer resp.Body.Close()
    b, _ := io.ReadAll(resp.Body)
    if resp.StatusCode >= 300 {
        return nil, fmt.Errorf("upnp error %d: %s", resp.StatusCode, string(b))
    }
    return b, nil
}

func soapEnvelope(action string, inner string) string {
    return fmt.Sprintf(`<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:%s xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
      %s
    </u:%s>
  </s:Body>
</s:Envelope>`, action, inner, action)
}

func xmlEscape(s string) string {
    r := strings.NewReplacer(
        "&", "&amp;",
        "<", "&lt;",
        ">", "&gt;",
        "\"", "&quot;",
        "'", "&apos;",
    )
    return r.Replace(s)
}


