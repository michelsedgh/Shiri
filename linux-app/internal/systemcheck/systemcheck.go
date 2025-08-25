package systemcheck

import (
    "fmt"
    "os/exec"
)

type Result struct {
    OK      bool
    Details []string
}

// Run performs basic environment checks required by the app.
func Run() Result {
    var details []string
    ok := true

    if _, err := exec.LookPath("ffmpeg"); err != nil {
        details = append(details, "ffmpeg: NOT FOUND (install: sudo apt install ffmpeg)")
        ok = false
    } else {
        details = append(details, "ffmpeg: ok")
    }

    if _, err := exec.LookPath("docker"); err != nil {
        if _, err2 := exec.LookPath("podman"); err2 != nil {
            details = append(details, "engine: docker/podman NOT FOUND (install Docker or Podman)")
            ok = false
        } else {
            details = append(details, "engine: podman found")
        }
    } else {
        details = append(details, "engine: docker found")
    }

    details = append(details, fmt.Sprintf("permissions: ensure access to container socket (docker group or root)"))
    return Result{OK: ok, Details: details}
}


