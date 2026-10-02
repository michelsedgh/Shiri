# Actual OwnTone resampling check

This manual Linux check compiles complete configured `outputs.c` and
`transcode.c`, the maintained framed output, and the actual player source-seal
function against real FFmpeg, libevent and libconfuse. It opens no output socket,
PCM device or network connection and installs nothing.

Provide an isolated configured checkout with the current maintained patches:

```sh
python tests/native/check_framed_resampler.py --source /path/to/configured/owntone
```

The checkout must include `config.h` and the resampler reset/framed layers.
`pkg-config` must resolve libavcodec, libavformat, libavfilter, libavutil,
libswresample, libevent, libconfuse and libcurl. Stage
`check_source_transition.py` beside this checker because it extracts the actual
player function. Compilation and execution have separate bounded deadlines;
ASan and UBSan remain enabled by default. `--observe` records failures for
diagnosis and is explicitly not an acceptance result.

The fixture checks real conversion from stereo S16 at 48 kHz to four final
signed PCM formats, both channel counts and both 48/44.1 kHz rates, including
one-frame converter warmup. It retains the original native presentation origin,
checks final gain at volumes 100 and 50 on actual converted queued samples, and
verifies that final copies leave their source unchanged.

The mixed-quality case places a zero-output 44.1 kHz subscription before a
nonempty 48 kHz/S32 subscription. It requires that the later output remains
visible, all buffers drain completely, and the next zero-valued frame cannot
replay retained samples. The same-quality source transition invokes the real
player admission/seal/reset function with controlled input and output ACKs and
then requires no retired FIR tail in successor zeros. Its unreset real-FFmpeg
control must retain a nonzero tail to establish measurement sensitivity.

The controlled ACKs do not establish device flush completion. Full production
build, actual transport/pacing, native phone grouping and physical speakers
remain separate checks. No speech overlay invokes the source-reset seam.
