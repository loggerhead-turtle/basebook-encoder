#!/usr/bin/env bash
# PlayCall Encoder — manual YouTube push (debug / rescue use).
#
# The normal path is the python service (playcall-encoder-youtube runs
# encoder/youtube_push.py, which the cloud link can repoint live). This
# script is the same push leg as a standalone one-shot, for when you want
# to run it by hand and watch ffmpeg's output directly:
#
#   sudo systemctl stop playcall-encoder-youtube
#   bash scripts/youtube_push.sh
#
# Reads /etc/playcall-encoder/config.json for the ingest key and YouTube
# target. Video is always -c copy; audio is probed over loopback RTSP the
# same way the service does it (Opus phone audio → transcode to AAC,
# because classic RTMP cannot carry Opus at all).

set -u

CONFIG="${PLAYCALL_ENCODER_DIR:-/etc/playcall-encoder}/config.json"

if [[ ! -f "$CONFIG" ]]; then
    echo "No config at $CONFIG — run the encoder setup first." >&2
    exit 1
fi

read -r INGEST_KEY YT_URL YT_KEY < <(python3 - "$CONFIG" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1]))
yt = cfg.get('youtube') or {}
print(cfg.get('local_ingest_key', ''), yt.get('url', ''), yt.get('key', ''))
PY
)

if [[ -z "$INGEST_KEY" || -z "$YT_URL" || -z "$YT_KEY" ]]; then
    echo "config.json is missing local_ingest_key or youtube url/key" >&2
    exit 1
fi

RTMP_IN="rtmp://127.0.0.1:1935/live/${INGEST_KEY}"
RTSP_IN="rtsp://127.0.0.1:8554/live/${INGEST_KEY}"
PUSH_URL="${YT_URL%/}/${YT_KEY}"

while true; do
    # Probed fresh every attempt — the source can change between sessions
    # (Mevo one game, a phone the next). RTSP sees the true track list;
    # MediaMTX's local RTMP read leg silently DROPS what RTMP can't carry
    # (Opus audio, H.265 video — 'skipping track (H265)'), so RTMP is
    # used only when BOTH tracks are RTMP-safe (H.264 + AAC).
    ACODEC="$(ffprobe -v error -rtsp_transport tcp -select_streams a:0 \
        -show_entries stream=codec_name -of csv=p=0 \
        "${RTSP_IN}" 2>/dev/null)"
    VCODEC="$(ffprobe -v error -rtsp_transport tcp -select_streams v:0 \
        -show_entries stream=codec_name -of csv=p=0 \
        "${RTSP_IN}" 2>/dev/null)"
    if [[ -z "$ACODEC" || "$ACODEC" == "aac" ]]; then
        AUDIO_ARGS=(-c:a copy)
    else
        AUDIO_ARGS=(-c:a aac -b:a 128k -ar 48000)
    fi
    if [[ ( -z "$ACODEC" || "$ACODEC" == "aac" ) \
          && ( -z "$VCODEC" || "$VCODEC" == "h264" ) ]]; then
        INPUT_ARGS=(-rw_timeout 10000000 -i "$RTMP_IN")
    else
        INPUT_ARGS=(-rtsp_transport tcp -i "$RTSP_IN")
    fi

    ffmpeg -hide_banner -loglevel warning \
        "${INPUT_ARGS[@]}" \
        -c:v copy "${AUDIO_ARGS[@]}" \
        -f flv "${PUSH_URL}"
    echo "youtube push ended — reconnecting in 3s"
    sleep 3
done
