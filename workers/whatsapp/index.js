/**
 * Delivers queued messages over WhatsApp.
 *
 * The control plane decides what should be said and never sends it: a
 * serverless function cannot hold a WhatsApp session, which needs a browser
 * and a socket that stay alive between requests. So this runs on an always-on
 * box, asks the control plane what is waiting, sends it, and reports back.
 *
 * The direction matters. This polls outward rather than being pushed to,
 * because a box behind a home router or a security group has no reachable
 * address, and a push that fails is a message silently lost.
 *
 * Two rules shape the whole file.
 *
 * A message is only ever marked sent after WhatsApp accepted it. The control
 * plane destroys the payer's phone number at that moment, so marking
 * optimistically would throw away the only way to retry.
 *
 * Nothing here may crash the loop. One malformed row, one number that has no
 * WhatsApp account, one network blip — each is that message's problem and not
 * the queue's.
 */

import { create, ev } from '@open-wa/wa-automate';

const CONTROL_PLANE = (process.env.CONTROL_PLANE_URL
  || 'https://commerce-control-plane-api.vercel.app').replace(/\/$/, '');
const API_KEY = (process.env.COMMERCE_API_KEY || '').trim();
const SESSION = process.env.WA_SESSION || 'shop';
const POLL_SECONDS = Number(process.env.POLL_SECONDS || 60);

// WhatsApp throttles a sender that blasts. A second between messages is slow
// enough to look human and fast enough that a backlog still clears.
const GAP_MS = Number(process.env.SEND_GAP_MS || 1000);

// Indian numbers, because that is who this ships to first. Razorpay reports
// E.164 already; the fallback is for a merchant who stored ten bare digits.
const DEFAULT_COUNTRY_CODE = process.env.COUNTRY_CODE || '91';

if (!API_KEY) {
  console.error('COMMERCE_API_KEY is not set. This worker reads a queue that '
    + 'carries payer contact details, so it will not start without one.');
  // Node does not read a .env file on its own, and a worker started without
  // the flag fails here with a message about a missing key rather than about
  // the file sitting right next to it — which sends you looking in the wrong
  // place. Say both.
  console.error('\nIf you have a .env file here, start it with:');
  console.error('  node --env-file=.env index.js');
  process.exit(1);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/**
 * Turn a phone number into the id WhatsApp expects.
 *
 * Returns null rather than guessing when the number is too short to be real —
 * a guessed number reaches a stranger, and a receipt naming somebody's
 * purchase is not a message to send to a stranger.
 */
function toWhatsAppId(contact) {
  if (!contact) return null;
  let digits = String(contact).replace(/\D/g, '');
  if (digits.length === 10) digits = DEFAULT_COUNTRY_CODE + digits;
  if (digits.length < 11 || digits.length > 15) return null;
  return `${digits}@c.us`;
}

async function api(path, options = {}) {
  const response = await fetch(`${CONTROL_PLANE}${path}`, {
    ...options,
    headers: {
      'X-API-Key': API_KEY,
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    throw new Error(`${options.method || 'GET'} ${path} -> ${response.status}`);
  }
  return response.json();
}

async function deliver(client, message) {
  const id = toWhatsAppId(message.contact);

  if (!id) {
    // Unsendable, and no amount of retrying changes that. Report it as a
    // failure so the queue stops carrying it and a merchant can see why.
    await api(`/v1/messages/${message.id}/failed`, {
      method: 'POST',
      body: JSON.stringify({
        error: message.contact ? 'contact is not a usable phone number'
          : 'no contact on this order',
      }),
    });
    return 'unsendable';
  }

  const body = message.link ? `${message.body}\n\n${message.link}`
    : message.body;

  try {
    await client.sendText(id, body);
  } catch (error) {
    // Marked failed, not sent, so the control plane keeps the number and this
    // is tried again. Attempts are capped there, not here.
    await api(`/v1/messages/${message.id}/failed`, {
      method: 'POST',
      body: JSON.stringify({ error: String(error).slice(0, 300) }),
    });
    return 'failed';
  }

  // Only now. This is what destroys the stored phone number.
  await api(`/v1/messages/${message.id}/sent`, { method: 'POST' });
  return 'sent';
}

async function tick(client) {
  const connected = await client.isConnected().catch(() => false);
  if (!connected) {
    // Nothing is marked, nothing burns an attempt. The queue waits, which is
    // the reason it is a queue.
    console.log('whatsapp not connected, skipping this round');
    return;
  }

  const { messages } = await api('/v1/messages/pending?limit=25');
  if (!messages.length) return;

  console.log(`${messages.length} to deliver`);
  const counts = { sent: 0, failed: 0, unsendable: 0 };

  for (const message of messages) {
    try {
      counts[await deliver(client, message)] += 1;
    } catch (error) {
      // Reporting the outcome failed — the control plane is unreachable or
      // rejected us. The message stays pending and will come back next round.
      console.error(`could not report ${message.id}:`, String(error));
    }
    await sleep(GAP_MS);
  }

  console.log(`sent ${counts.sent}, failed ${counts.failed}, `
    + `unsendable ${counts.unsendable}`);
}

/**
 * Tell the control plane where this worker has got to.
 *
 * The merchant sees the QR on the page they are already on rather than being
 * sent to a terminal they have no access to, which is the difference between
 * a step they complete and a step they abandon.
 *
 * Reporting failures are logged and swallowed. A worker that stopped
 * delivering receipts because it could not report its own status would be
 * failing at its actual job to succeed at describing it.
 */
async function report(state) {
  try {
    await api('/v1/whatsapp/state', {
      method: 'POST',
      body: JSON.stringify({ ...state, version: '1.0.0' }),
    });
  } catch (error) {
    console.error('could not report state:', String(error).slice(0, 120));
  }
}

async function main() {
  console.log(`control plane: ${CONTROL_PLANE}`);
  console.log(`session: ${SESSION}, polling every ${POLL_SECONDS}s`);

  // A WhatsApp QR lasts about twenty seconds and a fresh one replaces it.
  // Every one is forwarded, so whatever the merchant is looking at is the
  // one currently valid.
  ev.on('qr.**', (qr) => {
    console.log('\nQR code sent to the setup page. Scan it there, or above:');
    console.log('  WhatsApp -> Settings -> Linked devices -> Link a device\n');
    report({ status: 'waiting', qr });
  });

  const client = await create({
    sessionId: SESSION,
    headless: true,
    qrTimeout: 0,          // wait indefinitely; a person has to walk over
    authTimeout: 0,
    restartOnCrash: main,
    cacheEnabled: false,

    // Never the system `chromium-browser`. On Ubuntu 24.04 that name is a
    // shim for a snap, and a snap-confined browser refuses to launch from
    // inside another snap's cgroup — which is exactly what happens when this
    // is started by the SSM agent. The failure names xdg-settings and a
    // cgroup tag, and mentions neither snap confinement nor the fix.
    //
    // A real binary avoids the whole question. CHROME_PATH points at one when
    // the box has Google Chrome installed; otherwise Puppeteer's own download
    // is used, which is never a snap.
    useChrome: Boolean(process.env.CHROME_PATH),
    executablePath: process.env.CHROME_PATH || undefined,

    // A server has no user namespace to sandbox into, and no /dev/shm worth
    // the name. Without these Chrome exits immediately with a message about
    // the sandbox that reads like a permissions problem.
    chromiumArgs: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--disable-gpu',
    ],

    // Written to disk so a reboot reconnects without another scan. A worker
    // that demanded a QR after every restart would quietly stop sending
    // receipts the first time the box rebooted overnight.
    multiDevice: true,
  });

  console.log('whatsapp connected\n');

  const number = await client.getHostNumber().catch(() => null);
  await report({ status: 'connected', connected_number: number });

  let stopping = false;
  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.on(signal, () => {
      console.log(`\n${signal} — finishing this round, then stopping`);
      stopping = true;
    });
  }

  // Re-reported every round. A merchant refreshing the setup page an hour
  // later should see whether the link is still alive, not a stale "connected"
  // from whenever it last started.
  let sinceReport = 0;

  while (!stopping) {
    try {
      await tick(client);
      sinceReport += POLL_SECONDS;
      if (sinceReport >= 30) {
        const live = await client.isConnected().catch(() => false);
        await report(live
          ? { status: 'connected', connected_number: number }
          : { status: 'waiting' });
        sinceReport = 0;
      }
    } catch (error) {
      // The control plane being down is not this process's problem to solve.
      // Log it, wait, ask again.
      console.error('round failed:', String(error));
    }
    for (let i = 0; i < POLL_SECONDS && !stopping; i += 1) await sleep(1000);
  }

  await report({ status: 'stopped' });
  await client.kill();
  process.exit(0);
}

main().catch((error) => {
  console.error('worker could not start:', error);
  process.exit(1);
});
