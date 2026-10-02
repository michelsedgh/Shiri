# Actual live startup/pause repair snapshot

These three additive patches preserve the actual diagnostic OwnTone changes that passed the live iPhone fresh-start and requested 40-second pause/resume test. Apply them **in the listed order**, after the ordinary pinned OwnTone patches through `owntone-29.3-speech-owner.patch`, and before subsequent gain/transition changes. Their identities are in `manifest.json`. They intentionally preserve the old feature suffix; they are diagnostic snapshots, not a qualified default build.

1. `owntone-29.3-native-transition-timer.patch`: suspend the existing timer before sealing input and awaiting physical FLUSH; rearm only after exact generation arming.
2. `owntone-29.3-native-transition2-live.patch`: complete selected-output START before nonidle source admission; preserve a sealed generation and test malformed PCM separately from valid empty input.
3. `owntone-29.3-native-transition3-live.patch`: wait for fresh PCM after an accepted pause without a new cold-start timeout; retire playback on END and clean failed rearming.

The retained C programs contain the exact extracted original/fixed functions with regression scaffolding, including the timerfd and portable timer branches. They were compiled with address/undefined-behavior sanitizers. Original failures are expected negative controls. These programs do not replace a linked backend or network test. The live Linux build and test summary is in `docs/LIVE_PLAYBACK_REVIEW.md`.

The bounded atomic successor transition, delayed physical release and paused-phone speech bed are still being integrated. Do not silently widen deadlines, rebase presentation timestamps or treat this diagnostic snapshot as release qualification.
