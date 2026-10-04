# Manual Linux qualification

These harnesses exercise actual Linux namespaces, managed processes and audio
endpoints. They are not part of ordinary pytest execution. Use a disposable,
independently provisioned Linux lab; the production installer does not run them.

## Explicit lab admission

The native grouping, speech, music and Bluetooth supervisors require
`SHIRI_NATIVE_LAB_PROFILE` pointing to an admitted root-owned profile beneath
`/etc/shiri-rehearsal`. `native_lab.py` checks the lab marker, machine and boot
identity, source and binary digests, daemon accounts, empty ownership manifest,
private directories and protected endpoints before creating resources. Profiles
must describe the exact staged checkout and binaries; refresh their digests after
changing either. Importing pure observers without a profile remains supported.

Provisioning is external to the harnesses. They do not adopt an old installation
ID, discover a former house process or repair conflicting ownership. Admission
is checked again after the child finishes and after parent cleanup. Preserve the
ownership manifest and private failure artifacts if exact cleanup fails.

Digital output qualification uses explicitly enrolled snd-aloop endpoints and
GStreamer capture instruments. Install those dependencies only in the lab;
Shiri's production receiver and mixer use the native framed transport. The
capture clock, queue-origin calibration, PCM continuity and declared presentation
calendar remain independent observations, not physical speaker measurements.

## Maintained supervisors

Run each supervisor through its own CLI in the admitted lab, with an external
watchdog and no concurrent qualification run. The child checkers and producers
are implementation details of those supervisors.

| Supervisor | Evidence |
| --- | --- |
| `run_native_grouping.py` | Two exact native input owners, shared presentation calendar, final digital alignment, room-specific speech and source retirement. |
| `run_native_zone_faults.py` | Exact-room backend failure and recovery while the other room retains its original source and PCM. |
| `run_native_speech_stress.py` | Repeated bounded speech sessions with retained decoded references, stream continuity and exact cleanup. |
| `run_native_latency_probe.py` | Fresh per-offset idle/native epochs; each epoch freezes and verifies its actual production route plan before playback. |
| `run_native_speech_finite.py --offset-ms {-2000,0,2000}` | Finite Opus prefix/body/tail evidence for the selected fresh epoch. |
| `run_native_music_minimum.py` | Cold music startup on the current local B40/H140 plan without rebasing the source calendar. |
| `run_native_music_soak.py --soak-buffer-ms B --soak-horizon-ms H` | Bounded thirty-minute digital music observation with an explicitly frozen route budget. |
| `run_native_bluetooth_route.py --binary PATH --expected-sha256 HASH` | Admitted private BlueALSA/SBC route through framed output, before radio delivery. |

Grouping, fault and stress supervisors also accept `--minimum-policy` to retain
the additional current-policy qualification receipt. All generated production
configs use the same `latency_plan`: local ALSA B40/H140, Cast/Pulse B250/H350,
and AirPlay B500/H600 at zero offset. Negative saved corrections enlarge the
required buffer; grouped rooms share the maximum buffer plus 100 ms. These
software budgets do not establish acoustic onset or receiver compatibility.

The retired H750/B500 music experiment, H1000/B500 grouping override, 3/4-second
latency matrix and alternate GStreamer runtime are no longer selectable. Their
historical measurements are not evidence for the current checkout.

## Network receivers and supporting instruments

`check_native_network.py` has a separate explicit network profile. Its
`--print-profile` output describes the required inputs; the profile identifies
the admitted source, binaries, accounts, work tree and network target. It checks
actual receiver/output behavior and has a separate `--fault-case` option.

`loopback_capture_probe.py`, `isolated_group_lan.py`, `native_lab_audio.py`,
`native_lab_observation.py` and `group_failure_evidence.py` provide the shared
capture, private LAN, PCM and failure evidence machinery. `native_latency_epochs.py`,
`native_speech_reference.py` and the music/speech observer modules enforce the
current experiment contracts. They are not alternate production audio backends.

Private Bluetooth descriptor and parser checks are described in
[README_bluealsa_private.md](README_bluealsa_private.md). Installation, DHCP,
socket policy and installed-service checks are separate manual boundary proofs;
inspect their current source and provisioning requirements before running them.
Receiver ownership, PCM launch and exact unit recovery retain focused automated
regressions; the former installation-specific manual executables are removed.

A pass requires the relevant signal checks and exact teardown of owned peers,
captures, API processes, daemon units, network namespaces and endpoint leases.
Reports retain private diagnostics on failure. No digital-lab pass establishes
stock-phone grouping, physical speaker synchronization, Bluetooth radio timing
or microphone-measured latency.
