These two 15,360-byte files retain four final stereo S16LE 48 kHz buffers from the isolated software stress28 run. They contain generated test tones, not a user recording. A is the untouched 440 Hz program; B includes the legal native music duck ramp and its own 1320 Hz Opus onset. Historical transmitted Opus payloads were not retained, so the same-settings independent replay diagnoses the old detector; it does not establish exact payload admission for that old run.

- A SHA256: `3562a897ec7acae0cb7ce5b647c35e59273a367f6da8367836eea69d4f327ccb`
- B SHA256: `4ff63afbb1db8b707a49c27dfe219bc916435b9439b9eab44350beb4af678780`

New prototype tests separately verify public AVPacket publication through a real local WebRTC connection against an independent continuous libopus decode. Neither these vectors nor the local connection asserts phone, speaker, Bluetooth, Cast, or acoustic behavior.

The two same-settings historical replay tests are optional diagnostics enabled with `SHIRI_HISTORICAL_REPLAY=1`. Re-encoding the source on a different platform can produce different Opus packets, so this replay cannot serve as an exact-payload acceptance gate. The original CI replay failures are retained; the PCM files, hashes, and strict residual checks remain unchanged. Current exact-payload reference tests and the actual native finite-speech release gate remain required.
