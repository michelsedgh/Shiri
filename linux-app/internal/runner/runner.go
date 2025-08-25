package runner

import (
    "bytes"
    "context"
    "os/exec"
    "time"
)

// Result holds the outputs of a command execution.
type Result struct {
    Stdout []byte
    Stderr []byte
    Err    error
}

// Run executes a command with a timeout and returns captured stdout/stderr.
func Run(timeout time.Duration, name string, args ...string) Result {
    ctx, cancel := context.WithTimeout(context.Background(), timeout)
    defer cancel()
    cmd := exec.CommandContext(ctx, name, args...)
    var outBuf, errBuf bytes.Buffer
    cmd.Stdout = &outBuf
    cmd.Stderr = &errBuf
    err := cmd.Run()
    return Result{Stdout: outBuf.Bytes(), Stderr: errBuf.Bytes(), Err: err}
}


