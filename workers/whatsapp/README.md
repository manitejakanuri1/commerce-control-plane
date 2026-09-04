# WhatsApp delivery worker

Sends the receipts the control plane queues.

The control plane decides what should be said and never sends it — a
serverless function cannot hold a WhatsApp session, which needs a browser and
a socket alive between requests. This runs on an always-on box, asks what is
waiting, sends it, and reports back.

```
Payment confirms
   → control plane queues a receipt
   → this worker asks for it (every 60s)
   → WhatsApp delivers it
   → control plane destroys the phone number
```

## Setup on Lightsail

Ubuntu 22.04, **1 GB RAM minimum** — this drives a real Chromium.

```bash
# node 20
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# chromium's shared libraries
sudo apt-get install -y chromium-browser libgbm1 libnss3 libatk-bridge2.0-0 \
  libxss1 libasound2 fonts-liberation

mkdir -p ~/commerce-whatsapp && cd ~/commerce-whatsapp
# copy index.js, package.json and .env.example here
npm install

cp .env.example .env
nano .env          # paste your FULL merchant key
```

Then run it once in the foreground and scan the QR:

```bash
node --env-file=.env index.js
```

A QR code prints. On the shop's phone: **WhatsApp → Settings → Linked
devices → Link a device**. Scan it once.

The session is written to disk, so this is the only time anyone has to do it.

Stop it, then install the service:

```bash
sudo cp commerce-whatsapp.service /etc/systemd/system/
sudo systemctl enable --now commerce-whatsapp
journalctl -u commerce-whatsapp -f
```

## The key

`COMMERCE_API_KEY` must be a **full** key. A pending message carries the
payer's phone number, so the queue is not readable with a browse key.

That also means this box holds a credential that can spend on the merchant's
behalf. It belongs on a server you control, not a laptop, and `.env` is not
something to commit.

## What it does with each message

| Outcome | What happens |
|---|---|
| Sent | reported sent — **the control plane deletes the phone number** |
| Send threw | reported failed, retried, capped at 5 attempts |
| Number unusable | reported failed once; no retry will fix it |
| WhatsApp not connected | nothing marked, nothing burned, waits |
| Control plane unreachable | message stays pending, returns next round |

A message is only marked sent after WhatsApp accepted it. The control plane
destroys the phone at that moment, so marking optimistically would throw away
the only way to try again.

## Numbers

Razorpay reports E.164 (`+919876543210`). A bare ten-digit number gets
`COUNTRY_CODE` prefixed. Anything shorter than eleven digits is refused rather
than guessed at — a guessed number reaches a stranger, and a receipt naming
somebody's purchase is not a message to send to a stranger.

## What this is not

OpenWA drives WhatsApp Web, which WhatsApp's terms do not permit. **The number
running this can be banned.**

For a demo that is an acceptable trade. For production, the answer is the
official WhatsApp Business Cloud API — and swapping it means changing one
call, `client.sendText`. Nothing else in this file cares.

## Troubleshooting

**QR never appears** — Chromium's libraries are missing. Re-run the
`apt-get install` line above.

**Killed after a few minutes** — out of memory. Chromium needs about 700 MB;
a 512 MB Lightsail instance will not hold it.

**Session lost after reboot** — the `WA_SESSION` name changed, or the data
directory was deleted. Scan again and leave the name alone.

**Nothing sends and nothing errors** — `isConnected()` is false. Check
`journalctl` for "whatsapp not connected".
