# Live refactor checkpoint — October 4, 2026

The user authorized pushing the reviewed refactor and restarting the live
installation for testing. Source commit `1d24454e09ecbe0d11409e46fbb8fe34d8dadf02`
is pushed on `codex/shiri-rebuild`. The Ubuntu API/runtime and native Mac model
worker now use the same verified package. Native audio binaries, service units,
credentials, model cache and saved room configuration were preserved.

## Installed artifacts and recovery

The 61-file application wheel SHA-256 is
`5592e3d2dbf7f00c5968f3bd18b3909d92fdd4e81737e7ab3302090b5980fa38`.
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

- VM: `/var/lib/shiri-refactor-checkpoint-20261004-131900`
- Mac: `~/Library/Application Support/Shiri/TTS/checkpoint-refactor-20261004-pl_jvcdi`

Both retain the previous verified wheel, SHA-256
`d11563a487786772c957139b32501dad4b8c7d1f3211a555d1ea9583f369d3e9`.
The VM checkpoint also contains the stopped-state SQLite backup, configuration
observations, ownership manifests, install log and quiet smoke receipts. These
private artifacts and credentials are not committed to Git.

Rollback is an orderly service stop, exact resource retirement, reinstall of
the retained previous wheel and service restart. Keep current schema4 data and
user intent; no database downgrade is required. Do not restore the stopped
backup over later room edits merely to roll back application code.

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

The Mac LaunchAgent retained its original token, cache and explicit Qwen preload.
The new worker loaded and warmed `qwen3-0.6b-customvoice`, saved the successful
selection in a private mode-0600 `selected-model.json`, and restored that selection
after a second graceful restart. Final status is ready, idle, with automatic
recovery enabled.

## Verification

[GitHub CI for the deployed source](https://github.com/michelsedgh/Shiri/actions/runs/37223668512)
passed: 4,373 Python tests, 17 explicit/platform skips, native checks and the
frontend/browser suite. The earlier Mac and isolated ARM Linux review results
remain in [the repository review](REPO_REVIEW_2026-10-04.md).

Live checks passed:

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
