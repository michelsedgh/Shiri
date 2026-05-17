#!/bin/bash
exec python3 "%%MIXER_SCRIPT%%" --capture-dev "%%CAPTURE_DEV%%" --grp-dir "%%GRP_DIR%%" --tts-ws-port "%%TTS_WS_PORT%%"
