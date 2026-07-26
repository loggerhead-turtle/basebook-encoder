# Pointing your camera app at the encoder

The encoder ingests a standard RTMP stream on your local network. Anything
that can push "Custom RTMP" works: **Mevo (Mevo Camera app)**, **Larix
Broadcaster**, **OBS**, GoPro + apps, drones, etc.

Your personal ingest URL is shown on the final setup screen and any time
on the settings page (`http://playcall-encoder.local:8080`):

```
rtmp://playcall-encoder.local:1935/live/<your-key>     ← preferred
rtmp://<encoder-ip>:1935/live/<your-key>               ← fallback
```

If your camera app can't resolve `.local` names (some Android builds),
use the IP form. The `<your-key>` part is generated once per device; you
can rotate it from the settings page.

## Mevo (Mevo Camera app)

1. Camera + phone + encoder on the **same Wi-Fi network**.
2. Mevo app → Options → **Streaming** → **RTMP** → Custom.
3. URL: `rtmp://playcall-encoder.local:1935/live`
   Stream key: `<your-key>` (some versions take the full URL in one field —
   that works too).
4. Go live in the app. The encoder's status page shows "Receiving" within
   a couple of seconds.

## Larix Broadcaster (iPhone / Android)

1. Settings → Connections → **New connection**.
2. URL: `rtmp://playcall-encoder.local:1935/live/<your-key>`
3. Recommended: H.264, 1080p30, 3–4.5 Mbps, keyframe interval 2 s.
4. Tap record — the big red button streams to the encoder.

## OBS (laptop)

1. Settings → Stream → Service: **Custom…**
2. Server: `rtmp://playcall-encoder.local:1935/live`
   Stream Key: `<your-key>`
3. Start Streaming.

## SRT cameras (e.g. Mevo Core)

The encoder also listens on SRT:

```
srt://<encoder-ip>:8890?streamid=publish:live/<your-key>
```

## What happens next

* The encoder pushes the stream to **YouTube Live** in copy mode
  (byte-identical, no re-encode, ~0 CPU) and auto-reconnects if the
  uplink hiccups.
* A **rolling 12-hour local recording** is always kept on the SD card —
  an uplink drop never loses footage.
* The **scorebug** is published separately as an NDI source
  ("PlayCall Bug") plus `http://playcall-encoder.local:8765/bug.png` /
  `/bug.mjpg` — composite it in Mevo Studio / OBS / vMix *before* the
  video reaches the encoder, and it's burned into the stream, the
  recording, and every future clip.

## Encoder settings recommendations

| Uplink | Camera bitrate | Notes |
|---|---|---|
| Good (10+ Mbps up) | 4.5–6 Mbps 1080p30 | YouTube gets the full picture |
| Field LTE / hotspot | 2.5–3.5 Mbps 720p30 | Copy mode means what you send is what YouTube gets — size it for the *uplink*, not the LAN |
