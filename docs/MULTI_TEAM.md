# Multi-team assignment hopping

One encoder can serve **many teams** at the same field — the Warriors game
at 3, the Sidewinders at 5 — with zero touches on the box. The cloud tells
it who it belongs to right now; the encoder repoints itself.

## How it works

When `cloud.api_key` is set — via one-click pairing (PlayCall's Score Bug
Studio mints a key and opens this box's `/pair` page; PIN + confirm) or a
preconfig file on the boot partition (`preconfig/README.md`) —
`encoder/cloud_link.py` runs two loops:

### Assignment poll — every 5 s

```
GET {base_url}/api/encoder/assignment
X-Api-Key: <api_key>

200 →
{
  "assigned": true,
  "team_id": "team_123",
  "team_name": "Warriors",
  "bug_feed_url": "https://playsigns.net/api/sk/bug/bug_abc123.json",
  "youtube_rtmp_url": "rtmps://a.rtmps.youtube.com/live2/xxxx-xxxx",
  "game_id": "game_456"
}
```

Unassigned: `{"assigned": false, "team_id": null, "team_name": null,
"bug_feed_url": null, "youtube_rtmp_url": null, "game_id": null}`.

On any change to `(assigned, team_id, bug_feed_url, youtube_rtmp_url,
game_id)`:

1. **Feed URL recorded live** — `config.cloud.feed_url` is swapped
   in-process, no restart (the box itself draws no bug; the URL is kept
   for anything local that wants the new team's feed).
2. **YouTube target rewritten** — `config.youtube` is updated from
   `youtube_rtmp_url` and `playcall-encoder-youtube` is restarted
   (`systemctl restart`), so the push reconnects to the new broadcast.
   When the response says unassigned, the key is cleared and the push
   idles.

The camera never notices any of this: it keeps streaming to the same local
RTMP URL the whole time. Only the *outputs* hop.

### Heartbeat — every 15 s

```
POST {base_url}/api/encoder/heartbeat
X-Api-Key: <api_key>

{
  "state": "pushing",                      // "idle" | "receiving" | "pushing"
  "ingest": {"connected": true, "kbps": 3100},
  "push":   {"connected": true, "kbps": 3050, "reconnects_5m": 0},
  "cpu": 12.5,
  "temp": 54.3,                            // °C, null off-Pi
  "version": "1.0.0",
  "log_tail": ["...last 20 service log lines..."]
}
```

* `ingest` comes from the MediaMTX control API
  (`GET http://127.0.0.1:9997/v3/paths/list` — `ready` +
  `bytesReceived` delta → kbps).
* `push` comes from the status file the YouTube push service maintains
  (ffmpeg `-progress` parsing → kbps; reconnect timestamps → `reconnects_5m`).

Every cloud failure is a silent retry. The encoder **never** stops
encoding because the cloud is down.

## A typical double-header

| Time | Cloud state | What the encoder does |
|---|---|---|
| 2:45 | Admin assigns encoder → Warriors game | Bug feed → Warriors, YouTube push → Warriors broadcast |
| 3:00–4:30 | Warriors game | Streams + overlays, heartbeats |
| 4:45 | Admin reassigns → Sidewinders game | Bug feed swaps live, push restarts at the new broadcast |
| 5:00 | Sidewinders game | Same camera, same box, new stream |
| 7:00 | Unassigned | Push idles; ingest + recording keep working |
