#!/bin/bash
exec chrt -f 45 python3 "%%MIXER_SCRIPT%%" \
  --capture-dev "%%CAPTURE_DEV%%" \
  --grp-dir "%%GRP_DIR%%" \
  --tts-webrtc-socket "%%TTS_WEBRTC_SOCKET%%"
