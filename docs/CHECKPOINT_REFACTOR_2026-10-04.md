# Live refactor checkpoint — October 4, 2026

The refactor and subsequent speech-delivery repair are installed. Source commit
`cd931f538fc60319ec1e13a0235c17a669dcbf95` is pushed on `codex/shiri-rebuild`.
The Ubuntu API/runtime and native Mac model worker use the same verified package.
Native audio binaries, model credentials/cache and saved room configuration
were preserved. The private TTS connection now uses the VM management link.

## Installed artifacts and recovery

The 61-file application wheel SHA-256 is
`a2d46a084c17073cbfa4cbdec8bb5c42c24aa6e3fb3c3fdd7b51ed0056a3d61b`.
Every installed package file matches the wheel on both hosts; the removed
`runtime/local_output.py` is absent. Dependency checks pass and dependency
requirements are unchanged. Installation used the existing environments without
downloading or replacing their dependencies.

OwnTone remains `outputclock1-duck1-warm1`, SHA-256
`3d31ad79d937a0661222f32a10945bccc958f4f8318e7a7f7a46fdd57c2b7ba4`.
Shairport remains `timed3-startup1-volume2`, SHA-256
`4e051902f8ea7095385367ca4260372bda4813b8e6a84bd50abe08b8eb2fb5d6`.
The backend manifest remains
`b22311c39e3a5f3cae2e0aa7d2fd68dddd74a0a5cf24a870efe365e4d3d5d387`.

Private rollback checkpoints:

- VM: `/var/lib/shiri-delivery-checkpoint-20261004-02`
- Mac: `~/Library/Application Support/Shiri/TTS/checkpoint-delivery-20261004-ek7w3yxn`

Both retain the previous verified wheel, SHA-256
`5592e3d2dbf7f00c5968f3bd18b3909d92fdd4e81737e7ab3302090b5980fa38`.
The VM checkpoint also contains the stopped-state SQLite backup, configuration
observations, ownership manifests, install log and quiet smoke receipts. These
private artifacts and credentials are not committed to Git.

Rollback is an orderly service stop, exact resource retirement, reinstall of
the retained previous wheel and service restart. Keep current schema4 data and
user intent; no database downgrade is required. Do not restore the stopped
backup over later room edits merely to roll back application code.
Keep the new loopback TTS route when rolling back application code: restoring
the old bridged route would restore the demonstrated network fault.

## Delivery fault and transport repair

The reported `Speech missed its admitted sample calendar` job delivered only
99.333 ms of speech. Three neighboring 4.64-second jobs completed. A constant
five-second native stream passed, and Mac-local model output remained faster
than playback. The Linux-to-Mac HTTP capture then reproduced a 230.991 ms gap
inside the second generated burst, sufficient to miss the existing 150 ms
calendar guard. That guard predates this refactor.

Guest packet headers showed incorrect checksums, selective acknowledgments and
retransmission of the missing data. Three bridged requests increased TCP checksum
error counters by 54, 51 and 55. Both HTTP endpoints already disabled Nagle;
the measured failure was packet corruption/recovery, not slow inference or an
application flush timer. Post-connect MSS-option trials did not fix it. A proposed
pre-connect trial was never run.

The Mac reboot report at October 4, 13:46:34 records a kernel panic in
`in_finalize_cksum`, with an invalid TCP checksum offset. It occurred during this
investigation; the exact triggering request is not established. The reboot left
UTM stopped until the user signed back in and the test VM was started. Do not
repeat the bridged/MSS experiments on this host.

The VM now uses `SHIRI_TTS_WORKER_URL=http://127.0.0.1:8091`.
Mac LaunchAgent `org.shiri.tts-link` holds an SSH reverse forward from VM loopback
8091 to Mac loopback 8091, through the existing localhost management port 22096.
It bypasses the faulty bridged path, pins the existing SSH host key, binds only
loopback, retains worker bearer authentication, and reconnects automatically.
Its private key/known-hosts files are under `TTS/link` in Application Support;
LaunchAgent access to Documents is not assumed. Speaker and phone LAN routes
are unchanged. This is a live-test VM workaround, not a new deployment dependency
for ordinary Linux installations.

Three quiet 4.64-second requests on the replacement link completed naturally.
Maximum PCM gaps were 40.508, 39.732 and 39.044 ms, with zero TCP checksum errors,
retransmissions or playback-calendar deficits. Evidence is retained privately
in `Shiri-Live-Test/management/tts-safe-link-cadence-20261004.json`.

The worker also now uses its existing 100 ms packet-end allowance, sending full
20 ms packets up to 80 ms before their nominal start. This adds no first-packet
wait. Native-code replay verifies every sample in order through modeled 40–100 ms
delivery pauses, with the same 20 ms native onset; a 120 ms pause deliberately
exposes a 20 ms gap. Bounds, expiry, exact ownership and the late-calendar guard
remain enforced. These tests do not claim immunity to arbitrary network outages.
Job receipts now retain the largest worker-record gap and room PCM call duration.

## Restart and preservation

The API stopped before the broker. The broker's exact recorded units retired,
its owned network namespaces disappeared, and both ownership collections were
empty before installation. The installation identity remained unchanged.
The package was installed offline with its required hash, then the runtime
started before the API. Both services are active and enabled for normal boot.

Living Room Test retains revision 84, master volume 26, Sonos output
`92539824408726`, balance 100%, offset 0 ms and AirPlay NTP timing. Saved room,
speaker, profile, balance and timing rows are unchanged. The room is running
without an error and advertises **Shiri Test Living Room**.

Readiness now reports mode `ready` and an acknowledged connected hold. Its
renewed deadline advanced while the same room launch and prepared connection
remained in use. The software route retains B500/H600. No new speaker power or
acoustic-readiness measurement is implied.

The Mac LaunchAgent retained its original token, cache, selected-model state and
explicit Qwen preload. The worker loaded and warmed `qwen3-0.6b-customvoice` after
the update. Final status is ready, idle, with automatic recovery enabled. Both
Mac LaunchAgents run after user login; the Shiri Speaker Test VM must be running.

## Verification

The repair passed 265 focused tests: 94 coordinator tests, 73 pacing/stream/PCM
tests, and 98 audio/startup/ownership/deadline tests, plus Ruff and diff checks.
[CI for this source](https://github.com/michelsedgh/Shiri/actions/runs/37257414094)
passed: 4,381 Python tests, 17 explicit/platform skips, native checks, and 76
frontend/browser checks with no failures. The original refactor's successful
full-CI and isolated Linux results remain in
[the repository review](REPO_REVIEW_2026-10-04.md).

The repaired live installation passed three complete model/coordinator/native
checks with outgoing samples replaced by zeros immediately before room IPC:
364,800, 353,280 and 387,840 samples (23.04 seconds total). All 1,105,920 generated
samples were admitted and mixed, with exact FINISH receipts, zero expired or
refused samples, zero native underflows, empty final queues and retired sessions.
Maximum dispatch gap was 41.49 ms. All database tables and room revision/volume
were unchanged. These are digital delivery checks, not acoustic recordings.
The VM checkpoint and its Mac management copy retain `silent-integration.json`.

A controlled idle termination of the SSH link produced a new managed process
and restored model access automatically; the API again reported the model ready.
`link-reconnect.json` retains that observation. Final HTTP access to the live UI
returns 200, and the model, room and retained speaker connection are ready.

The initial refactor's live checks passed:

- Readiness endpoint returns ready with simulation disabled.
- Exact binary stream admission through both Unix-socket hops accepts three
  silent 20 ms PCM frames, acknowledges all 2,880 samples, finishes and releases
  the voice. Saved room state and running processes remain healthy.
- A quiet benchmark submitted through the Linux API completes naturally through
  the Mac worker and confirms decoder cleanup; it sends no generated speech to
  the room. The worker returns to ready.
- A separate quiet Mac request completes naturally with 5.12 seconds of generated
  audio. First HTTP PCM was about 71.8 ms in that request; this is not acoustic
  onset or a guaranteed request latency.

No audible test phrase, new phone regression, microphone measurement or
multi-room acoustic qualification was performed during this deployment.

## Ready for listening

Open [Shiri](http://shiri-speaker-test.local:8080/), refresh the page to load the
new UI, and use **Speak** on Living Room Test. The fallback address is
[192.168.1.200:8080](http://192.168.1.200:8080/). Qwen is selected and ready.
Use the phone's **Shiri Test Living Room** AirPlay destination for music.
Different configured rooms now have independent speech delivery; one room
queues two additional replies and replaces an active reply only when explicitly
requested. Actual listening remains the next acceptance step.
