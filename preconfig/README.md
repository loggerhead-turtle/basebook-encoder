# Zero-touch shipping (preconfig)

Drop a file named **`playcall-encoder.json`** on the SD card's boot
partition (the FAT partition any computer can see — mounted at
`/boot/firmware` on Bookworm) before first boot. The encoder applies it
and **deletes it** on first boot: no hotspot, no captive portal — the box
comes up already on Wi-Fi, already pointed at YouTube, already paired to
the cloud.

This is how pre-configured units are shipped to customers, and how a fleet
can be imaged in one pass with Raspberry Pi Imager + a per-unit JSON.

## Format

See [`example.json`](example.json). All keys optional — anything omitted
keeps its default and can be finished later on the settings page.

```json
{
  "networks": [
    {"ssid": "Home-WiFi",   "psk": "secret",  "priority": 100, "label": "home"},
    {"ssid": "Field-Router", "psk": "gameday", "priority": 90,  "label": "gameday"}
  ],
  "youtube_key": "xxxx-xxxx-xxxx-xxxx",
  "cloud": {
    "base_url": "https://playsigns.net",
    "api_key": "pce_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
  },
  "device_name": "Warriors Encoder 1",
  "network_managed": true
}
```

Notes:

* `networks` — joined in `priority` order (higher wins when several are in
  range). `psk` may be empty for open networks.
* `youtube_key` — a bare stream key **or** a full `rtmps://…/live2/<key>`
  URL; both are normalized. Not needed at all when a cloud `api_key` is
  set — assignment then supplies the YouTube target per game.
* `cloud.api_key` — enables assignment hopping + heartbeats
  (see `docs/MULTI_TEAM.md`). Real keys start with **`pce_`** and are
  minted by the PlayCall cloud — the plaintext key is shown **once**;
  only its hash is stored server-side. Most users don't need this file
  for pairing anymore: **Score Bug Studio → Encoders → Pair a Raspberry
  Pi** mints a key and opens the box's own `/pair` page (PIN + one
  confirm). Preconfig remains the zero-touch path for shipped units.
* `network_managed` — set `false` for a box that must NEVER have its
  network touched (Speedify cellular bonding, Ethernet-only, a tether the
  owner manages). No setup hotspot, no Wi-Fi writes; `networks` is then
  ignored. Omit (or `true`) for normal PlayCall-managed Wi-Fi.
* The local RTMP ingest key and the settings PIN are generated on first
  boot; read them from `/etc/playcall-encoder/config.json` or the settings
  page at `http://playcall-encoder.local:8080`.

The file holds Wi-Fi passwords in plaintext on a FAT partition, which is
why it is consumed (deleted) as soon as it has been applied.
