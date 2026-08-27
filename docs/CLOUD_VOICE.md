# ☁ Cloud voice channel (LiveKit)

The coach's live voice used to require the phone and the box on the
same WiFi (a peer-to-peer link). The cloud channel removes that: one
always-on voice room **per team**, hosted on a LiveKit SFU — the coach
publishes, the boxes and ear phones subscribe, and it works coach-on-
cellular / box-on-hotspot, ~200–400 ms.

**Only staff can talk.** Listener tokens are minted without publish
rights, server-side — the pitcher and catcher structurally cannot talk
back (the NFHS one-way posture).

## Turning it on (site operator, once)

1. Create a LiveKit Cloud project (free tier: livekit.io) — or
   self-host `livekit-server` on a small VM.
2. Set three environment variables on the web service:

       LIVEKIT_URL=wss://<project>.livekit.cloud
       LIVEKIT_API_KEY=<key>
       LIVEKIT_API_SECRET=<secret>

3. That's the whole rollout. On their next poll, coach consoles start
   publishing and boxes start subscribing. Nothing else deploys.

Unset the variables and everything quietly returns to the P2P link +
clip fallback, which also remain active as the same-WiFi fast path
while the channel is on.

## Box requirements

`install_comms.sh` installs the `livekit` Python client. A box
installed before that shipped says so on the coach page ("livekit is
not installed — re-run install_comms.sh"); re-running the encoder
install one-liner fixes it.
