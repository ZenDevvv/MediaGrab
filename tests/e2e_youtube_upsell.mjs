/**
 * MediaGrab YouTube upsell card (§1.5 Decisions #18-19).
 *
 * Drives a real browser and intercepts /api/info at the network layer with a
 * genuine 451 UPSTREAM_BLOCKED payload, so the assertion covers the real
 * app.js path rather than a stubbed internal function.
 *
 * Verifies:
 *   - the card appears on the landing screen and is not a dead end
 *   - copy stays truthful ("most links", never "unavailable")
 *   - the CTA points somewhere real, with an OS-tailored label
 *   - dismissing it leaves the rest of the page usable
 *   - a successful grab hides it again (no stale card)
 *
 * Usage: node e2e_youtube_upsell.mjs <appUrl> <cdpPort>
 */

import { spawn } from 'node:child_process';
import http from 'node:http';

const APP_URL = process.argv[2];
const CDP_PORT = process.argv[3] || '9482';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function fetchJson(url, attempts = 40) {
  for (let i = 0; i < attempts; i++) {
    try {
      return await new Promise((resolve, reject) => {
        http.get(url, (res) => {
          let d = '';
          res.on('data', (c) => (d += c));
          res.on('end', () => { try { resolve(JSON.parse(d)); } catch (e) { reject(e); } });
        }).on('error', reject);
      });
    } catch (e) {
      if (i === attempts - 1) throw e;
      await sleep(150);
    }
  }
}

const chrome = spawn('chromium', [
  '--headless=new', '--disable-gpu', '--no-sandbox',
  `--remote-debugging-port=${CDP_PORT}`, 'about:blank'
], { stdio: 'ignore' });

const failures = [];
function check(label, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) failures.push(`${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label} = ${JSON.stringify(actual)}`);
}
function checkTrue(label, actual) { check(label, !!actual, true); }

const BLOCKED = {
  detail: {
    code: 'UPSTREAM_BLOCKED',
    message: "YouTube limits requests from shared servers, so most links won't work here. The native app downloads from your own connection — no limits.",
    remedy: 'native_app',
    retry_after_seconds: 1800
  }
};

const OK_META = {
  url: 'https://vimeo.com/12345',
  title: 'A Working Video',
  uploader: 'Someone',
  duration: 300,
  source_audio_kbps: 128,
  source_audio_codec: 'opus',
  qualities: [
    { resolution: '1080p', approx_bytes: 100000000, type: 'remux' },
    { resolution: '720p', approx_bytes: 50000000, type: 'remux' }
  ],
  subtitles: []
};

(async () => {
  const list = await fetchJson(`http://127.0.0.1:${CDP_PORT}/json`);
  const target = list.find((t) => t.type === 'page') || list[0];
  const ws = new WebSocket(target.webSocketDebuggerUrl);

  let msgId = 1;
  const callbacks = new Map();
  let blockNext = true;

  function send(method, params = {}) {
    return new Promise((resolve) => {
      const id = msgId++;
      callbacks.set(id, resolve);
      ws.send(JSON.stringify({ id, method, params }));
    });
  }

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id && callbacks.has(msg.id)) {
      callbacks.get(msg.id)(msg.result);
      callbacks.delete(msg.id);
    }
    if (msg.method === 'Fetch.requestPaused') {
      const { requestId, request } = msg.params;
      if (request.url.includes('/api/info')) {
        const status = blockNext ? 451 : 200;
        console.log(`INFO  intercepted ${request.url} -> ${status}`);
        const body = blockNext ? JSON.stringify(BLOCKED) : JSON.stringify(OK_META);
        send('Fetch.fulfillRequest', {
          requestId,
          responseCode: status,
          responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
          body: Buffer.from(body).toString('base64')
        }).catch(() => {});
      } else {
        send('Fetch.continueRequest', { requestId }).catch(() => {});
      }
    }
  };

  await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });

  const ev = async (expression) => {
    const r = await send('Runtime.evaluate', { expression, returnByValue: true });
    if (r.exceptionDetails) throw new Error(`evaluate failed: ${JSON.stringify(r.exceptionDetails)}`);
    return r.result ? r.result.value : undefined;
  };
  async function waitFor(expr, ms = 8000) {
    const d = Date.now() + ms;
    while (Date.now() < d) {
      if (await ev(`(()=>{try{return !!(${expr})}catch(e){return false}})()`)) return true;
      await sleep(100);
    }
    return false;
  }
  async function nav(url) {
    await send('Page.navigate', { url });
    await waitFor('document.readyState === "complete"');
    await sleep(400);
  }

  await send('Runtime.enable');
  await send('Page.enable');
  await send('Network.enable');
  await send('Fetch.enable', { patterns: [{ urlPattern: '*/api/info*', requestStage: 'Request' }] });

  // --- Card starts hidden ---
  await nav(`${APP_URL}/`);
  check('card hidden before any attempt', await ev(`document.querySelector('#yt-upsell').hidden`), true);

  // --- A blocked YouTube URL surfaces the card on the landing screen ---
  await ev(`(()=>{const i=document.querySelector('#input-url');i.value='https://www.youtube.com/watch?v=aqz-KE-bpKQ';
            document.querySelector('#btn-grab').click();return true})()`);
  const appeared = await waitFor(`document.querySelector('#yt-upsell') && !document.querySelector('#yt-upsell').hidden`);
  check('card appears on blocked URL', appeared, true);

  // The landing screen must not blank out (regression on the old handler).
  check('landing screen still active', await ev(`document.querySelector('#view-paste').classList.contains('active')`), true);

  const text = await ev(`document.querySelector('#yt-upsell-text').textContent`);
  checkTrue('copy says "most links"', /most links/.test(text));
  check('copy never claims "unavailable"', /unavailable/i.test(text), false);
  checkTrue('copy names the native app', /native app/i.test(text));

  checkTrue('title present', (await ev(`document.querySelector('.yt-upsell-title').textContent`)).length > 0);
  checkTrue('reassurance line present', (await ev(`document.querySelector('.yt-upsell-foot').textContent`)).includes('other sites work right now'));

  // --- CTA is actionable, never a dead "#" ---
  const href = await ev(`document.querySelector('#yt-upsell-cta').getAttribute('href')`);
  checkTrue('CTA is not a placeholder', href && href !== '#' && href.length > 1);
  const ctaLabel = await ev(`document.querySelector('#yt-upsell-cta-label').textContent`);
  checkTrue('CTA label is app-specific', /app/i.test(ctaLabel) && ctaLabel !== 'Get the app');
  check('CTA text visible', await ev(`document.querySelector('#yt-upsell-cta').offsetParent !== null`), true);

  // The card carries the reason; the red inline line must not restate it.
  check('no duplicated inline error', await ev(`(()=>{const e=document.querySelector('#inspect-error');return e && e.hidden && !e.textContent.trim()})()`), true);

  // --- Dismiss leaves the page usable ---
  await ev(`document.querySelector('#yt-upsell-dismiss').click(); true`);
  await sleep(200);
  check('dismiss hides card', await ev(`document.querySelector('#yt-upsell').hidden`), true);
  check('page still interactive after dismiss', await ev(`!!document.querySelector('#input-url')`), true);

  // --- A successful grab hides any stale card ---
  blockNext = false;
  await ev(`(()=>{const i=document.querySelector('#input-url');i.value='https://vimeo.com/12345';
            document.querySelector('#btn-grab').click();return true})()`);
  await waitFor(`document.querySelector('#yt-upsell').hidden`);
  check('no stale card after a working grab', await ev(`document.querySelector('#yt-upsell').hidden`), true);
  check('working URL advances to configure', await waitFor(`document.querySelector('#view-choose')?.classList.contains('active')`), true);

  console.log(failures.length ? `FAILURES:\n${failures.join('\n')}` : 'SUCCESS');
  chrome.kill();
  process.exit(failures.length ? 1 : 0);
})().catch((e) => {
  console.error(e);
  chrome.kill();
  process.exit(1);
});