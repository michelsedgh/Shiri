package fifo

import (
    "os"
    "syscall"
)

// Ensure creates a directory and two FIFOs (audio, metadata) with 0666 perms.
func Ensure(dir string) error {
    if err := os.MkdirAll(dir, 0o755); err != nil {
        return err
    }
    if err := mkfifo(dir+"/audio"); err != nil && !os.IsExist(err) {
        return err
    }
    if err := mkfifo(dir+"/metadata"); err != nil && !os.IsExist(err) {
        return err
    }
    _ = os.Chmod(dir+"/audio", 0o666)
    _ = os.Chmod(dir+"/metadata", 0o666)
    return nil
}

func mkfifo(path string) error {
    return syscall.Mkfifo(path, 0o666)
}


