package raopbin

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
)

// Resolve returns the path to a RAOP sender binary to use.
// Preference order:
//  1) raop_play in PATH
//  2) clipraop in PATH
//  3) Bundled binary next to the executable or in common subdirectories
//     (e.g., ./clipraop-linux-aarch64, ./bin/clipraop-linux-aarch64, ./linux-app/clipraop-linux-aarch64,
//      ./linux-app/bin/clipraop-linux-aarch64)
// It attempts to set execute permissions on discovered bundled binaries when needed.
func Resolve() (string, error) {
	if p, err := exec.LookPath("raop_play"); err == nil {
		return p, nil
	}
	if p, err := exec.LookPath("clipraop"); err == nil {
		return p, nil
	}

	exe, err := os.Executable()
	if err != nil {
		return "", errors.New("cannot determine executable path")
	}
	exeDir := filepath.Dir(exe)

	// Only known bundled artifact we expect right now
	bundledName := bundledFilename()
	if bundledName == "" {
		return "", errors.New("no supported RAOP binary for this platform")
	}

	candidates := []string{
		filepath.Join(exeDir, bundledName),
		filepath.Join(exeDir, "bin", bundledName),
		filepath.Join(exeDir, "linux-app", bundledName),
		filepath.Join(exeDir, "linux-app", "bin", bundledName),
	}
	for _, c := range candidates {
		if st, err := os.Stat(c); err == nil && !st.IsDir() {
			_ = ensureExec(c)
			return c, nil
		}
	}
	return "", errors.New("RAOP sender binary not found (raop_play/clipraop)")
}

func bundledFilename() string {
	if runtime.GOOS == "linux" && runtime.GOARCH == "arm64" {
		return "clipraop-linux-aarch64"
	}
	// Add more mappings as additional artifacts are bundled (e.g., amd64)
	return ""
}

func ensureExec(path string) error {
	st, err := os.Stat(path)
	if err != nil { return err }
	mode := st.Mode()
	if mode&0o111 != 0 {
		return nil
	}
	return os.Chmod(path, mode|0o111)
}


