# Comms — the box as the catcher's & pitcher's ear

Turns this (or any activated) Pi into the team comms box: it finds the
live game by itself, speaks every called pitch into the paired
earpiece(s), and carries the coach's live voice (WebRTC). Managed from
a phone-friendly admin page on port 8790 — pair buds, label them
🧢 catcher / ⚾ pitcher, name the box, lock Bluetooth, update the code.

Install (one time, on the box):

    sudo bash comms/install_comms.sh

On an encoder box it reuses the encoder's activation key automatically;
on the display Pi it imports the display app's key; on a fresh box it
asks for a team activation code (generated on /auth/team).

These files are exported from the main repo (Play-call/pi/) — edit them
there and re-export here.
