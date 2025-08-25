package encode

import (
    "io"
    "os"
    "os/exec"
)

// FFMpegEncoder starts an ffmpeg process to encode PCM (s16le 44.1k stereo) to MP3 or AAC.
type FFMpegEncoder struct {
    Cmd       *exec.Cmd
    Stdin     io.WriteCloser
    Stdout    io.ReadCloser
}

// StartMP3 spawns ffmpeg reading PCM from in and returning its stdout reader.
func StartMP3() (*FFMpegEncoder, error) {
    cmd := exec.Command("ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-f", "s16le", "-ar", "44100", "-ac", "2", "-i", "pipe:0",
        "-f", "mp3", "-b:a", "320k", "-")
    stdin, err := cmd.StdinPipe()
    if err != nil { return nil, err }
    stdout, err := cmd.StdoutPipe()
    if err != nil { return nil, err }
    cmd.Stderr = os.Stderr
    if err := cmd.Start(); err != nil { return nil, err }
    return &FFMpegEncoder{Cmd: cmd, Stdin: stdin, Stdout: stdout}, nil
}


