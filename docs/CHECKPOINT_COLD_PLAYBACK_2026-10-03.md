# Cold playback repair and live rollout — October 3, 2026

The previously qualified speech-startup checkpoint was still uninstalled when
the user reported another failed first music start and truncated idle speech.
The live VM was running the earlier `idle1` backend and paced-delivery package.
The requested conditional Git rollback therefore did not apply. The user also
confirmed that the missing speech opening occurred while the room was idle.

The permanent `outputclock1` package and native backend are now installed in
**Shiri Speaker Test**, at **http://shiri-speaker-test.local:8080/**. Living Room
has a saved, stable-ID NTP timing preference, verified against the running
AirPlay 2 output after a normal room restart. Refresh the browser for the timing
selector and streaming progress display.

The earlier `coldmusic1` rollout still truncated idle speech: the user heard
only `in your room`, while speech over music was complete. A controlled NTP
comparison subsequently preserved the whole phrase, confirmed by the user.
The final installed build passed incremental-delivery and cleanup checks;
its new listening result is pending. Acoustic onset and whole-house alignment
have not been measured.

## Controlled clock comparison

The cold PTP diagnostic reproduced the truncated phrase. First voice output
was scheduled about 1.47 seconds before the Sonos receiver's first PTP clock
exchange. The sender's clock identity, timestamp mapping and sync delivery were
consistent; receiver servo lock and acoustic onset were not measured.

Changing only this output's existing OwnTone timing selection to NTP produced
the entire sentence, confirmed by the user. The selected output remained
AirPlay 2 with encrypted ALAC, and five NTP D4 anchors were captured with no
PTP/D7 anchors or capture drops. The temporary launcher and normal room settings
were restored afterward. This is evidence for this specific SYMFONISK speaker,
not a universal Sonos preference or a measured mixed-output synchronization claim.

The successful cold comparison dispatched first PCM at 1,036 ms and had
already delivered 179 ms by 1,217 ms, while synthesis took 2,715 ms for 4.64
seconds of audio. All generated audio was delivered and cleanup completed.
The coordinator was therefore streaming during generation. Total job time
(5,985 ms) includes paced delivery and retirement, rather than a wait before
first sound. Actual first audible latency still needs a microphone measurement.

The production timing selector is installed. It persists by stable speaker
identity, defaults to Automatic, and uses a narrowly scoped native ID override
so renaming or duplicate names cannot redirect the setting. Explicit NTP keeps
AirPlay 2; explicit PTP requires receiver support. Clock changes restart the
normal room runtime and invalidate prior calibration for that configuration.
NTP was saved only for the physically tested output, through the normal API
with a revision guard. The native output reports NTP, and the generated config
contains the exact stable-ID override without a name-based setting or temporary
service launcher. No blanket NTP policy or fixed startup silence was added.

The final installed idle phrase dispatched first PCM at 1,051.7 ms. At
1,222.2 ms it had delivered 179.3 ms of speech; generation took 2,709.1 ms for
4.64 seconds. All generated speech was delivered, with no dropped speech
frames and confirmed worker/room cleanup. Six NTP anchors were captured with
zero capture drops and no PTP events. This confirms streaming during generation
in the installed build, without measuring first audible latency.

## Reproduced music failure

The October 3 live log showed a 288,384-byte read deficit at 20:20:57 UTC and
an `anchor_late` rejection at 20:21:03. The first timed FIFO write had already
carried roughly 716 ms of future output lead, with no Python ingress fault or
dropped bytes. OwnTone subsequently stopped the native timer and waited for
its file-style six-second capacity refill. That wait aged the original timing
markers beyond their valid admission deadline.

The additive `coldmusic1` patch clears file-style read debt only for a genuine
empty read from the exact armed framed source operation. It preserves the
existing admitted timer and every supplied timestamp. Initial and FLUSH
admission guards, EOF, errors, controls and ordinary file underruns keep their
existing behavior. An abandoned proposal that rearmed cold admission after an
empty read failed a valid continuation-jitter case; that failure is retained.

A separate sender defect appeared at the encoded output boundary. AirPlay's
periodic sync advances by committed audio samples, so after a long empty gap
the receiver could get almost one second of resumed payload using its old clock
mapping. The repair detects an original raw-block presentation discontinuity
greater than one source sample and sends the existing sync before the next
payload. It preserves converter state and partial ALAC samples, resetting the
periodic cadence without inserting silence, flushing or changing timestamps.
This gap-sync correction is qualified for AirPlay 2; equivalent RAOP and Cast
gap behavior remains unverified.

## Speech repairs installed together

The earlier `drain1` and `startupmeta1` layers are installed with this music
repair. They drain final speech datagrams before natural FINISH, flush the
bounded converter tail, and require acknowledgement of initial AirPlay DMAP
metadata on the output-only speech route before reporting CONNECTED. A metadata
ACK proves protocol acceptance, not speaker clock lock or rendered samples.

Real HTTP and private room captures already established incremental delivery
before generation completion and retained the entire generated phrase. The
updated interface now exposes received and delivered audio while a job runs.
Delivery/cleanup duration includes paced playback of the waveform. The reported
5.52-second job duration is therefore not a measured first-sound delay, and
first model PCM is not a speaker-onset measurement.

## Qualification and installed artifacts

- Final portable regressions: 4,269 passed, 85 explicit/platform skips;
  59 browser/client checks passed. Live-device opt-ins were off.
- Native timing selection: 202 actual configuration, discovery, feature,
  output-JSON and SETUP assertions across 20 device cases, plus 12 actual
  libconfuse parser cases passed. Invalid IDs, duplicate overrides and
  unsupported PTP fail closed. The full Linux candidate compile/link passed.
- Source checkpoint `a72463df4f1d496c954841c8f41e2edbca831cd5` passed
  [GitHub CI run 37157793956](https://github.com/michelsedgh/Shiri/actions/runs/37157793956).
  The earlier integration failures and corrected runs are retained in evidence.
- Exact native input/player fixtures: both POSIX and Linux timerfd branches
  passed 676,378 ASan/UBSan checks each. The preceding source reproduced the
  six-second refill and late-anchor failure in both branches.
- Actual Linux resampling, ALAC encoding and independent decoding: 1,183
  packets and 1,665,664 decoded bytes matched exactly. A 65-second gap retained
  the same decoded prefix/tail as contiguous input; the fresh mapping preceded
  resumed payload. Fractional sample durations and ±100 ns mapped perturbations
  retained normal cadence; real forward/backward gaps received a fresh mapping.
- Full Linux compile/link, independent source review, strict historical patch
  inversion, Ruff, shell syntax and whitespace checks passed.

The native checks used an actively attested isolated unit with private network,
devices and temporary storage, inaccessible house runtime/state/installation,
and finite CPU, memory and time limits. Neither the fixtures nor the build
opened a speaker or audio device. A fixture GCC warning and an observer's
incorrect capitalized-banner expectation are retained alongside corrected runs.

Installed VM wheel SHA-256:
`34e74f28837c76a3e64df2f6b03dd7917de1d279e7d19dc11a6b60f630906dde`.
All 60 package files match the checkout byte for byte. Installed and running
OwnTone binary SHA-256:
`6ab30cd382aa8d1e1f7f260d929e4a2cf6e36b0251f6a663970696b97b9a5942`.
Its complete required marker ends `idle1-drain1-startupmeta1-coldmusic1-outputclock1`.

The coordinated installation normally stopped both services, retired all
recorded actors and released owned networks before swapping verified bytes.
It preserved the shutdown-boundary room configuration: master volume **26**,
revision **83**, Sonos output `92539824408726`, offset **0 ms**, balance **100%**.
The normal timing API then saved NTP and advanced the room to revision **84**;
all other settings were verified unchanged, and old room actors were retired.
Database schema 3 migrated to 4 with existing room state intact. No database
or ownership ledger was restored or edited manually. The database and any
pending phone-volume journal were backed up privately at shutdown.
Both services are healthy and remain enabled for VM boot. Shairport, service
units, access settings and the Mac model worker were unchanged.

The new rollback checkpoint is
`/var/lib/shiri-output-clock-checkpoint-20261003` in the guest. It
contains the actual preceding wheel/backend and coordinated recovery artifacts;
it does not substitute an older pre-idle backend. Recovery first preserves
failed-state database files, and refuses database rollback if new user writes
cannot be excluded. The compatible Mac generation worker intentionally retains
its previous package and loaded Qwen model.

## Remaining physical acceptance

The final installed idle phrase has run; its listening confirmation is pending.
Then test fresh iPhone playback without web-volume adjustments, phone/web
volume agreement and a 40-second pause/resume. Speech over music, ducking,
cancellation, standby and mixed-protocol whole-house timing still need physical
checks. If the opening remains truncated, correlate receiver PTP exchanges,
metadata ACK and first PCM with microphone onset; no render-ready ACK has been
established that could justify a guessed startup wait.

New receipts, failed proposals, build inputs, exact package and rollout proof
are being prepared for additive `batch-0005` in
`/Users/homr/Documents/Shiri-Validation/release-2026-10-03`. Earlier sealed
evidence and the historical uninstalled checkpoint remain unchanged.
