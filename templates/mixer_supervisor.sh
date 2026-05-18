#!/bin/bash
exec chrt -f 45 python3 "%%MIXER_SCRIPT%%" \
  --capture-dev "%%CAPTURE_DEV%%" \
  --grp-dir "%%GRP_DIR%%" \
  --tts-pcm-pipe "%%TTS_PCM_PIPE%%" \
  --tts-rate "%%TTS_PCM_RATE%%" \
  --tts-channels "%%TTS_PCM_CHANNELS%%"
