# PlayCall NDI Encoder

Turn a Raspberry Pi + **any RTMP camera app** (Mevo, Larix, OBS, drones…)
into a **YouTube streaming relay with a professional scorebug overlay**.

```
Your camera app ──RTMP over local Wi-Fi──►  PlayCall Encoder (this Pi)
                                              ├─ copy-mode push ──► YouTube Live
                                              ├─ rolling 12h local recording
                                              └─ live scorebug (NDI + PNG/MJPEG)
                                                   ▲
                              PlayCall cloud feed ─┘  (score, theme, assignment)
```

* **No capture card, no streaming PC.** The Pi ingests the camera's RTMP
  stream and forwards it to YouTube byte-for-byte (copy mode — a Pi 4
  loafs).
* **Professional scorebug.** Six layout families (`bar`, `tv`, `tvbox`,
  `bottomline`, `lowerthird`, `sidestack`), themeable colors/fonts, driven
  live from the PlayCall scorekeeper. Published as a real **NDI** source
  plus an always-on PNG/MJPEG fallback.
* **Survives bad internet.** Uplink drops only interrupt YouTube — the
  local recording never stops, and the push auto-reconnects.
* **Multi-team.** Pair it to the PlayCall cloud and one encoder hops
  between teams' games automatically — see
  [docs/MULTI_TEAM.md](docs/MULTI_TEAM.md).

Hardware: Raspberry Pi 4 or 5, Raspberry Pi OS Bookworm (64-bit).

---

## Quickstart A — you received a pre-configured encoder

1. Plug the encoder into power (and Ethernet if you have it). Wait ~1
   minute.
2. Open your camera app and stream to the RTMP URL on the card in the box
   (or find it any time at `http://playcall-encoder.local:8080`, PIN on
   the same card):

   ```
   rtmp://playcall-encoder.local:1935/live/<your-key>
   ```

3. That's it — the encoder pushes to your YouTube channel and overlays
   your scorebug automatically.

## Quickstart B — self-install on your own Pi

1. **Download the installer from PlayCall** — sign in to your PlayCall
   account and go to **Score Bug Studio → Encoders → Download the encoder
   installer** (`/score/encoder/download`). Any signed-in user can
   download it; the demo cannot.
2. Flash Raspberry Pi OS Bookworm (64-bit Lite is fine) and boot the Pi.
3. Copy the downloaded `playcall-encoder-<version>.tar.gz` to the Pi
   (USB stick, or `scp` if SSH is enabled), then:

```bash
tar xzf playcall-encoder-*.tar.gz
cd playcall-encoder
sudo bash install.sh
```

The installer fetches MediaMTX, installs the encoder to
`/opt/playcall-encoder`, enables the services, and sets the hostname to
`playcall-encoder.local`. On a **fresh, offline** Pi, follow the hotspot
setup below. On a Pi that is **already online** (Ethernet, Speedify,
tether), see "Already-networked Pi" — there is no hotspot step.

(Developers with repo access can still clone and `sudo bash install.sh`.)

> **⚠ Installing on a Pi you already use for other things?** (Speedify,
> ad-blocking, home automation…) **Make a full SD-card backup first.**
> The installer adds services and may change the hostname; a backup is
> your undo button. On an already-online Pi it adopts your network
> untouched (see below), but back up anyway.

## Pair it to PlayCall (cloud pairing)

Pairing connects the box to your PlayCall account so your teams can point
it at their games — each team's stream lands on **that team's** YouTube
channel automatically.

**If the Pi is installed and online** (this is also the path for a
non-fresh / Speedify Pi):

1. Sign in to PlayCall **on a device on the same network as the Pi** and
   open **Score Bug Studio → Encoders**.
2. Click **🔗 Pair a Raspberry Pi**. PlayCall creates the box's cloud key
   (shown once) and opens the box's own pairing page.
3. On that page, enter the box's **settings PIN** and click **Pair this
   encoder**. Done — the box checks in within seconds and appears in the
   Encoders list.
   * If `playcall-encoder.local` doesn't resolve, browse to
     `http://<pi-address>:8080`, sign in with the PIN, and paste the key
     from the PlayCall page.

**If the Pi is fresh and offline:** run the hotspot setup first (below) —
the final screen reminds you — then do the three steps above once the box
is on your Wi-Fi.

**After pairing — day-to-day:**

* Connect each team's YouTube inside PlayCall (team stream settings /
  go-live). No stream keys ever need to be typed on the box.
* Every team you're staff on shows this box on its **Encoders** card:
  **Stream here** pins it to that team + channel; **Auto-follow** lets it
  chase whichever game you score; **Release** idles it. That card is your
  list of "who is this box streaming for right now."

## Already-networked Pi (Speedify, Ethernet, cellular bonding)

Some streamers run **Speedify** on the Pi to bond a cellular connection
with local Wi-Fi/Ethernet. The encoder detects this and stays out of the
way:

* If the Pi **already has working connectivity** at install/first-run
  (a default route on any interface, or an active Speedify service /
  `connectify0` bonding adapter), the installer **adopts the existing
  network as-is**: no setup hotspot, no Wi-Fi changes, ever. It prints
  your camera RTMP URL and settings PIN right in the terminal.
* On an adopted box the config records `network_managed: false`. The
  recovery hotspot is disabled outright, the settings page hides the
  Wi-Fi section, and PlayCall never writes to NetworkManager or
  wpa_supplicant — your Speedify bond is untouched, even during a
  cellular outage.
* Manage the box's connectivity with your own tools (Speedify dashboard,
  `nmcli`, `raspi-config`); the encoder just uses whatever route exists.
  Streaming, the scorebug, cloud pairing, and multi-team assignment all
  work identically in this mode.

---

## First-time setup (the hotspot walkthrough)

After install (or on a factory-reset box) the encoder broadcasts its own
Wi-Fi network.

**1. Join the setup hotspot.** On your phone, join the Wi-Fi network
named `PlayCall-Encoder-XXXX` (the XXXX is unique to your box). The setup
page opens automatically; if not, browse to `http://192.168.4.1`.

> _[screenshot placeholder: phone Wi-Fi list showing PlayCall-Encoder-7F3A]_

**2. Pick your home network.** Choose your Wi-Fi from the scanned list
and enter its password.

> _[screenshot placeholder: setup step 1 — network picker]_

**3. (Optional) Add game-day networks.** The field's Wi-Fi or your travel
router / phone hotspot. The encoder remembers up to three networks and
automatically joins whichever one it finds — set it up at home, it just
works at the field.

> _[screenshot placeholder: setup step 2 — game-day network fields]_

**4. Paste your YouTube stream key.** From YouTube Studio → **Go live** →
copy the Stream key (a bare key or the full `rtmps://` URL both work).
You can skip this and add it later.

> _[screenshot placeholder: setup step 3 — YouTube key field]_

**5. Save.** The final screen shows two things — **write them down**:

* your camera app's **RTMP URL**
  (`rtmp://playcall-encoder.local:1935/live/<key>` plus an IP fallback),
* your **settings PIN** for the settings page.

The hotspot then disappears and the encoder joins your network.

> _[screenshot placeholder: final screen with RTMP URL + PIN]_

Camera-app-specific walkthroughs (Mevo / Larix / OBS):
[docs/STREAMING.md](docs/STREAMING.md).

## Changing settings later

Browse to **`http://playcall-encoder.local:8080`** from any device on the
same network and enter your PIN. From there: view live status (camera
receiving? pushing to YouTube?), add/remove Wi-Fi networks, update the
YouTube key, adjust scorebug bandwidth, rotate the ingest key, copy a
support log bundle, or factory reset.

**Locked out / network changed?** If the encoder has no working network
connection on **any** interface (no Ethernet, no Wi-Fi, no route out) for
90 seconds, it automatically re-raises the `PlayCall-Encoder-XXXX`
hotspot so you can always get back in. A box that's happily streaming
over Ethernet or a travel router never triggers this.

## Shipping pre-configured units

Drop a `playcall-encoder.json` on the SD card's boot partition — the box
comes up fully configured with no hotspot step. See
[preconfig/README.md](preconfig/README.md).

---

## FAQ

**Do I need the PlayCall cloud?** No. Standalone, the encoder is a
rock-solid RTMP→YouTube relay with a local recording. Pairing to the
cloud adds the live scorebug feed, remote status, and multi-team
assignment hopping.

**Does it transcode?** No — video is pushed to YouTube byte-for-byte
(`-c:v copy`). What your camera sends is what YouTube gets, so set the
camera bitrate for your *uplink*, not your LAN. (Opus audio from
WebRTC-ish sources is the one exception — it's transcoded to AAC because
RTMP can't carry Opus.)

**Where does the scorebug appear?** It's published as an NDI source
("PlayCall Bug") and at `http://playcall-encoder.local:8765/bug.png` /
`/bug.mjpg`. Composite it over your cameras in Mevo Studio / OBS / vMix —
then it's baked into the stream *and* the recording.

**What if the internet dies mid-game?** YouTube viewers see a gap; the
local recording doesn't. The push reconnects automatically when the
uplink returns.

**Does it make highlight clips?** Yes — one box does both. Booking a play
in the scoring app queues a clip window; `playcall-encoder-clipper` cuts
it from the **local recording** (never the uplink, so clips are clean
even when the stream stuttered) and uploads it to the gamecast's 🎬 Clips
tab. Turn it on at **Scorekeeper Settings → 🎬 Auto-clips**. Clips need
cloud pairing; uploads are rate-capped (`UPLOAD_BPS`, default 250 kB/s)
so they never starve the live push. Local copies are kept in
`/var/lib/playcall-encoder/clips` and pruned after `RETAIN_DAYS` (7).

**Can two cameras publish at once?** No — one publisher per ingest path.
Stop one before starting the other.

**HEVC?** Ingest yes. Forwarding HEVC to YouTube (Enhanced RTMP) needs
ffmpeg ≥ 7.1 — Bookworm ships older, so use H.264 on the camera, or run
Raspberry Pi OS Trixie.

## Troubleshooting

| Symptom | Check |
|---|---|
| Camera app can't connect | Same Wi-Fi as the encoder? Try the IP-form URL (settings page shows it). Key spelled exactly? |
| `playcall-encoder.local` doesn't resolve | Some Android builds lack mDNS — use the IP URL. Is avahi running? (`systemctl status avahi-daemon`) |
| Status shows Receiving but YouTube is dark | YouTube key set and the broadcast created in YouTube Studio? Check `reconnects/5m` on the status page. |
| Scorebug frozen | Is the feed URL assigned (cloud pairing) and reachable? See the log viewer. |
| No clips appearing | Auto-clips enabled in Scorekeeper Settings? Box paired to the cloud? `systemctl status playcall-encoder-clipper` and `journalctl -u playcall-encoder-clipper -n 50`. |
| Setup hotspot never appears | It only broadcasts when unconfigured or offline >90 s — and **never** on an adopted box (`network_managed: false`, e.g. Speedify/Ethernet installs). To force setup: factory reset from the settings page, or delete `/etc/playcall-encoder/config.json` and reboot. |
| Anything weird | Settings page → **Copy logs for AI help** → paste the bundle into your support chat. Secrets are already redacted. |

## Security notes

* **Settings page.** Access is PIN-gated with a global exponential
  lockout (5 straight failures locks logins for 60 s, doubling up to
  15 min). Keep the PIN off the label if strangers handle the box.
* **Stream key visibility on the box itself.** The YouTube push runs
  ffmpeg with the full `rtmps://…/live2/<key>` URL on its command line,
  so any *local* user on the Pi can read the key via `ps` /
  `/proc/<pid>/cmdline` (ffmpeg has no supported way to read an RTMP
  *output* URL from a file). The encoder is designed as a single-purpose
  appliance with no additional local users; don't hand out shell accounts
  on it. The config file itself is `root:playcall` mode `0640`.
* **Logs and support bundles are scrubbed.** The "Copy logs for AI help"
  bundle, the on-page log viewer, and the cloud heartbeat's `log_tail`
  all pass through a redactor that strips `rtmp(s)://…/live[2]/<key>`
  URLs, the configured YouTube stream key, and the local ingest key
  before the text leaves the box.
* **Setup hotspot.** The first-boot / recovery AP is open by design (so
  setup works from any phone). It only appears when the box is
  unconfigured or has had no connectivity at all for 90 s — complete
  setup promptly and it disappears.

## Developing / running on a laptop

`SCOREBUG_FAKE=1` runs the entire stack with PNG output and zero hardware
calls — see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## License

MIT © Loggerhead Turtle — see [LICENSE](LICENSE).
