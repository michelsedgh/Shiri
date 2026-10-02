# Thirty-minute digital music qualification

`tests/linux/run_native_music_soak.py` is an explicit clean-VM fixture. It uses
existing isolated two-zone setup, original ownership and cleanup checks, and
actual final kernel Loopback PCM. It has not qualified physical speakers, phone
interoperability, or a new production buffer policy.

The operator must supply `--soak-buffer-ms B --soak-horizon-ms H` after selecting
the smallest route plan qualified by the preceding actual short gates. There is
no implicit buffer or horizon default. The fixture preserves the ALSA40 ms floor
and at least100 ms of timer lead, freezes the common horizon before either BEGIN,
and rejects a changed policy after admission. It does not retime audio for TTS.
The source is exactly two disposable zero-offset local ALSA routes; other route
floors and mixed physical devices remain separate qualification work.

Only this mode allows a finite1830-second native producer. Its120 ms amplitude
code uses the original deterministic bit mixer throughout the run. Existing
producer durations, their20-second coded prefix and constant speech-test tail,
and all existing waveform/timing guards remain unchanged.

Each output must contain one unique exact first-second reference. Every later
stereoS16 frame must equal the declared source frame calendar, including gain,
channels and content. Original caps, sample-offset/PTS continuity, clock base,
300 ms callback gap, zero-drop, actor identity, source incarnation, selection,
volume and advancing NPT checks continue during analysis. The healthy observation
cycle remains60 ms. There is no added speech or crash exercise here: separate
stress and fault gates carry those results.

The run requires at least1800 seconds of both guest monotonic and wall-clock
elapsed time and at least86,400,000 actual verified music frames per zone. A fast
unit-test loop cannot meet this software gate. Every30 seconds, an immutable
8-second PCM window checks the original full/early/late relative2 ms guard,
calendar against independent capture uncertainty, and drift from the first
window within2 ms. All windows, including a failed measurement, are retained as
bounded numeric evidence.

Live storage retains roughly12 seconds with hard ceilings of4 MiB and1024
blocks per output, plus the existing bounded pending queue. A block can be
removed only after both original per-buffer and exact waveform validation.
Lifetime PCM/metadata SHA256 digests and frame/block counts are separate from
window-local continuity receipts; no retained window represents full-run PCM.
Final and rejected-window artifacts include actual PCM, timestamps, hashes,
retained index range and lifetime counters, with their scope stated explicitly.

Analysis and the healthy observer are owned tasks and are joined before caller
cancellation propagates. The existing independent finally block still retires
producers, captures, rooms, API, broker and isolated network and verifies empty
ownership and original host state. A new standalone supervisor reuses the
existing namespace/host guards, adds explicit `-B` for the inner Python process,
and admits a2000-second inner deadline under an external2100-second watchdog.
The root operator owns VM launch and retirement. This fixture performs no IRL
speaker tests and changes no production package defaults.
