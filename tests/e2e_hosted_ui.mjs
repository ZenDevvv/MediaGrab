/**
 * MediaGrab hosted-mode UI behaviour.
 *
 * Verifies the §1.2 Milestone 2 decisions against a real browser:
 *   #3  promo strip visibility + OS-tailored CTA
 *   #8  no countdown before save; retention notice is static
 *   #9  mode comes from the server-rendered meta tag
 *   #10 locked resolutions disabled + selection auto-corrected
 *   #11 429 surfaces the upgrade prompt instead of a generic error
 *
 * Usage: node e2e_hosted_ui.mjs <appUrl> <cdpPort> <hosted|desktop>
 */

import { spawn } from 'node:child_process';
import http from 'node:http';

const APP_URL = process.argv[2];
const CDP_PORT = process.argv[3] || '9480';
const EXPECT_MODE = process.argv[4] || 'hosted';

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function fetchJson(url, attempts = 40) {
  for (let i = 0; i < attempts; i++) {
    try {
      return await new Promise((resolve, reject) => {
        http.get(url, res => {
          let d = '';
          res.on('data', c => d += c);
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

const LONG_META = {
  url: 'https://example.com/long',
  title: 'Long Video',
  uploader: 'Channel',
  duration: 7200, // 120 minutes, over the hosted cap
  source_audio_kbps: 128,
  source_audio_codec: 'opus',
  qualities: [
    { resolution: '2160p', approx_bytes: 2000000000, type: 'encode' },
    { resolution: '1080p', approx_bytes: 500000000, type: 'remux' },
    { resolution: '720p', approx_bytes: 200000000, type: 'remux' }
  ],
  subtitles: []
};

(async () => {
  const list = await fetchJson(`http://127.0.0.1:${CDP_PORT}/json`);
  const target = list.find(t => t.type === 'page') || list[0];
  const ws = new WebSocket(target.webSocketDebuggerUrl);

  let msgId = 1;
  const callbacks = new Map();
  let downloadHits = 0;
  let nextDownloadStatus = 202;
  let lastDownloadBody = JSON.stringify({ job_id: 'fakejob' });

  function send(method, params = {}) {
    return new Promise(resolve => {
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
      if (request.url.includes('/api/download')) {
        downloadHits++;
        send('Fetch.fulfillRequest', {
          requestId,
          responseCode: nextDownloadStatus,
          responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
          body: Buffer.from(lastDownloadBody).toString('base64')
        }).catch(() => {});
      } else if (request.url.includes('/api/events/')) {
        // Complete the job over SSE so the ready screen renders for real,
        // rather than stubbing internal functions.
        const sseBody =
          `event: complete\n` +
          `data: ${JSON.stringify({
            filename: 'clip.mp4',
            size: '42.0 MB',
            download_url: '/api/file/fakejob'
          })}\n` +
          `\n`; // SSE dispatches on a blank line; a single \n never fires.
        send('Fetch.fulfillRequest', {
          requestId,
          responseCode: 200,
          responseHeaders: [
            { name: 'Content-Type', value: 'text/event-stream' },
            { name: 'Cache-Control', value: 'no-cache' }
          ],
          body: Buffer.from(sseBody).toString('base64')
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
  await send('Fetch.enable', {
    patterns: [
      { urlPattern: '*/api/download*', requestStage: 'Request' },
      { urlPattern: '*/api/events/*', requestStage: 'Request' }
    ]
  });

  const hosted = EXPECT_MODE === 'hosted';

  // --- Mode comes from the server tag (Decision #9) ---
  await nav(`${APP_URL}/`);
  check('mode meta tag', await ev(`document.querySelector('meta[name="mg-mode"]')?.content`), EXPECT_MODE);
  check('retention meta present', await ev(`!!document.querySelector('meta[name="mg-retention-seconds"]')`), true);

  // --- Promo strip (Decision #3) ---
  check('promo strip hidden', await ev(`document.querySelector('#hosted-promo-strip').hidden`), !hosted);

  // --- Choose screen ---
  await ev(`sessionStorage.setItem('mg_active_meta', ${JSON.stringify(JSON.stringify(LONG_META))});
            sessionStorage.setItem('mg_active_url', 'https://example.com/long'); true`);
  await nav(`${APP_URL}/choose`);
  const ready = await waitFor(`document.querySelector('#view-choose')?.classList.contains('active')`);
  check('choose screen active', ready, true);
  await sleep(500);

  // --- Locked resolutions (Decision #10) ---
  const chipState = await ev(`Array.from(document.querySelectorAll('#video-qualities-grid .chip'))
      .map(c => c.dataset.resolution + ':' + (c.classList.contains('locked') ? 'locked' : (c.classList.contains('selected') ? 'selected' : 'free')))`);
  if (hosted) {
    check('hosted chips', chipState, ['2160p:locked', '1080p:selected', '720p:free']);
  } else {
    check('desktop chips ungated', chipState, ['2160p:selected', '1080p:free', '720p:free']);
  }

  // --- Clip-aware trim guard (Decision #5) ---
  check('trim banner hidden', await ev(`document.querySelector('#trim-limit-banner').hidden`), !hosted);
  check('grab disabled', await ev(`document.querySelector('#btn-start-download').disabled`), hosted);

  // --- Retention notice, no premature countdown (Decision #8) ---
  // The notice is populated on the ready screen, which only renders after a
  // completed job. Before that it must be absent from the DOM entirely --
  // asserting visibility here would be testing the wrong screen.
  check('retention notice hidden pre-ready',
    await ev(`document.querySelector('#ready-retention-notice')?.hidden`), true);
  check('no live timer before save', await ev(`!!document.querySelector('#ready-deletion-timer')`), false);
  check('stale countdown copy gone',
    await ev(`document.body.innerHTML.includes('Deleted from this machine')`), false);

  // --- 429 handling (Decision #11) ---
  if (hosted) {
    nextDownloadStatus = 429;
    lastDownloadBody = JSON.stringify({
      detail: 'Only 1 concurrent download allowed per session on hosted server.'
    });
    await ev(`document.querySelector('#btn-start-download').disabled = false; true`);
    await ev(`document.querySelector('#btn-start-download').click(); true`);
    await sleep(1200);

    check('429 does not navigate to error screen',
      await ev(`document.querySelector('#view-error').classList.contains('active')`), false);
    const surfaced = await ev(`(document.querySelector('#trim-limit-text').textContent || '').includes('concurrent download')`);
    check('429 upgrade prompt surfaced', surfaced, true);
    check('429 reached the server', downloadHits > 0, true);
  }

  // --- Ready screen + post-save countdown (Decision #8) ---
  // Drive a real successful job so the ready screen renders from live code.
  nextDownloadStatus = 202;
  lastDownloadBody = JSON.stringify({ job_id: 'fakejob' });
  await nav(`${APP_URL}/choose`);
  await waitFor(`document.querySelector('#view-choose')?.classList.contains('active')`);
  await ev(`document.querySelector('#btn-start-download').disabled = false; true`);
  await ev(`document.querySelector('#btn-start-download').click(); true`);

  const readyShown = await waitFor(`document.querySelector('#view-ready')?.classList.contains('active')`, 10000);
  check('ready screen reached', readyShown, true);

  if (!readyShown) {
    const diag = await ev(`JSON.stringify({
      active: Array.from(document.querySelectorAll('[id^="view-"]'))
        .filter(v => v.classList.contains('active')).map(v => v.id),
      errMsg: document.querySelector('#error-desc-msg')?.textContent
    })`);
    console.log('READY DIAG:', diag);
  }

  if (readyShown) {
    check('retention notice visible on ready',
      await ev(`!document.querySelector('#ready-retention-notice').hidden`), true);
    check('no countdown before save',
      await ev(`!!document.querySelector('#ready-deletion-timer')`), false);

    const noticeText = await ev(`document.querySelector('#ready-retention-text')?.textContent || ''`);
    if (hosted) {
      check('hosted notice mentions the server', noticeText.includes('deleted from the server'), true);
      check('hosted notice avoids the wrong referent', noticeText.includes('this machine'), false);

      // Save click is the only event that triggers the server-side purge.
      await ev(`document.querySelector('#btn-save-media').click(); true`);
      await sleep(600);
      check('countdown starts on save',
        await ev(`!!document.querySelector('#ready-deletion-timer')`), true);
      check('countdown starts at retention window',
        await ev(`document.querySelector('#ready-deletion-timer')?.textContent`), '01:00');
    } else {
      check('desktop notice mentions Downloads',
        noticeText.toLowerCase().includes('downloads'), true);
      // Desktop keeps files; a countdown would be a lie.
      await ev(`document.querySelector('#btn-save-media').click(); true`);
      await sleep(600);
      check('desktop shows no countdown after save',
        await ev(`!!document.querySelector('#ready-deletion-timer')`), false);
    }
  }

  ws.close();
  chrome.kill();

  if (failures.length) {
    console.error('\nFAILURES:\n' + failures.map(f => '  - ' + f).join('\n'));
    process.exit(1);
  }
  console.log('\nSUCCESS: hosted UI behaviours verified.');
  process.exit(0);
})().catch(e => {
  console.error('ERR:', e && e.message ? e.message : e);
  chrome.kill();
  process.exit(1);
});