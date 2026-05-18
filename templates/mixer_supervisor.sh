#!/bin/bash
exec python3 "%%MIXER_SCRIPT%%" \
  --capture-dev "%%CAPTURE_DEV%%" \
  --grp-dir "%%GRP_DIR%%" \
  --tts-rtp-port "%%TTS_RTP_PORT%%" \
  --tts-payload-type "%%TTS_RTP_PAYLOAD_TYPE%%" \
  --tts-rate "%%TTS_RTP_RATE%%" \
  --tts-channels "%%TTS_RTP_CHANNELS%%" \
  --rtp-jitter-ms "%%TTS_RTP_JITTER_MS%%"
