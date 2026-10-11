import { spawn } from 'node:child_process';
import http from 'node:http';

const APP_URL = process.argv[2];
const CDP_PORT = process.argv[3] || '9444';

if (!APP_URL) {
  console.error('FAIL: usage: node e2e_choose_grab.mjs <appUrl> [cdpPort]');
  process.exit(2);
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

async function fetchJson(url, attempts = 40) {
  for (let i = 0; i < attempts; i++) {
    try {
      return await new Promise((resolve, reject) => {
        http.get(url, res => {
          let data = '';
          res.on('data', chunk => data += chunk);
          res.on('end', () => {
            try { resolve(JSON.parse(data)); } catch (e) { reject(e); }
          });
        }).on('error', reject);
      });
    } catch (e) {
      if (i === attempts - 1) throw e;
      await sleep(150);
    }
  }
}

async function runE2ETest() {
  const chrome = spawn('chromium', [
    '--headless=new',
    '--disable-gpu',
    '--no-sandbox',
    `--remote-debugging-port=${CDP_PORT}`,
    'about:blank'
  ], { stdio: 'ignore' });

  let ws = null;
  const uncaughtErrors = [];
  let downloadRequest = null;

  try {
    // Poll for the debugging endpoint rather than sleeping a fixed interval.
    const list = await fetchJson(`http://127.0.0.1:${CDP_PORT}/json`);
    const target = list.find(t => t.type === 'page') || list[0];
    if (!target) throw new Error('No CDP page target available');

    ws = new WebSocket(target.webSocketDebuggerUrl);
    let msgId = 1;
    const callbacks = new Map();

    function send(method, params = {}) {
      return new Promise(resolve => {
        const id = msgId++;
        callbacks.set(id, resolve);
        ws.send(JSON.stringify({ id, method, params }));
      });
    }

    async function evaluate(expression) {
      const res = await send('Runtime.evaluate', { expression, returnByValue: true });
      if (res.exceptionDetails) {
        throw new Error(`Evaluate failed: ${JSON.stringify(res.exceptionDetails)}`);
      }
      return res.result ? res.result.value : undefined;
    }

    // Navigate and wait for scripts to actually run, instead of guessing.
    async function navigate(url) {
      await send('Page.navigate', { url });
      await waitFor('document.readyState === "complete"');
    }

    async function waitFor(expression, timeoutMs = 8000) {
      const deadline = Date.now() + timeoutMs;
      while (Date.now() < deadline) {
        const value = await evaluate(`(() => { try { return !!(${expression}); } catch (e) { return false; } })()`);
        if (value) return true;
        await sleep(100);
      }
      return false;
    }

    
    await new Promise((resolve, reject) => {
      ws.onopen = resolve;
      ws.onerror = reject;
    });

    // Intercept /api/download so the test exercises the UI contract without a
    // real media fetch. Fetch interception pauses the request before it is
    // sent, so we record it from Fetch.requestPaused (Network.requestWillBeSent
    // never fires for paused requests) and fulfil it with a synthetic 202.
    // Without this the suite downloads tens of megabytes and fails outright on
    // any machine without network access.
    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.id && callbacks.has(msg.id)) {
        callbacks.get(msg.id)(msg.result);
        callbacks.delete(msg.id);
      }
      if (msg.method === 'Runtime.exceptionThrown') {
        uncaughtErrors.push(msg.params.exceptionDetails);
        console.error('E2E Runtime Exception:', msg.params.exceptionDetails);
      }
      if (msg.method === 'Fetch.requestPaused') {
        const { requestId, request } = msg.params;
        if (request.url.includes('/api/download')) {
          downloadRequest = {
            url: request.url,
            method: request.method,
            postData: request.postData
          };
        }
        send('Fetch.fulfillRequest', {
          requestId,
          responseCode: 202,
          responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
          body: Buffer.from(JSON.stringify({ job_id: 'e2efakejob' })).toString('base64')
        }).catch(() => {});
      }
    };

    await send('Runtime.enable');
    await send('Page.enable');
    await send('Network.enable');
    await send('Fetch.enable', {
      patterns: [{ urlPattern: '*/api/download*', requestStage: 'Request' }]
    });

    // 1. Visit root and prime sessionStorage with media info.
    await navigate(`${APP_URL}/`);
    if (!await waitFor('document.readyState === "complete"')) {
      throw new Error(`Root page never completed loading at ${APP_URL}`);
    }

    await evaluate(`
      sessionStorage.setItem('mg_active_meta', JSON.stringify({
        url: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
        title: 'Never Gonna Give You Up',
        uploader: 'Rick Astley',
        duration: 212,
        thumbnail: 'https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg',
        source_audio_kbps: 128,
        source_audio_codec: 'opus',
        qualities: [
          { resolution: '1080p', approx_bytes: 50000000, type: 'remux' },
          { resolution: '720p', approx_bytes: 25000000, type: 'remux' }
        ],
        subtitles: []
      }));
      sessionStorage.setItem('mg_active_url', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ');
      true;
    `);

    // 2. Navigate to /choose and wait for the choose screen to activate.
    await navigate(`${APP_URL}/choose`);
    const chooseReady = await waitFor(
      `document.querySelector('#view-choose') && document.querySelector('#view-choose').classList.contains('active')`
    );

    if (!chooseReady) {
      const diag = await evaluate(`(() => {
        const views = Array.from(document.querySelectorAll('[id^="view-"]'))
          .map(v => v.id + ':' + (v.classList.contains('active') ? 'active' : 'hidden'));
        return JSON.stringify({ title: document.title, views, hasScript: !!document.querySelector('script[src*="app.js"]') });
      })()`);
      throw new Error(`Choose screen did not become active. Diagnostics: ${diag}`);
    }

    // 3. Click Grab.
    const clickRes = await evaluate(`
      (() => {
        const btn = document.querySelector('#btn-start-download');
        if (!btn) return { error: 'btn-start-download not found' };
        btn.click();
        return { success: true };
      })()
    `);

    if (!clickRes || clickRes.error) {
      throw new Error(`Click setup failed: ${clickRes && clickRes.error}`);
    }

    // 4. Verify the transition to the working screen.
    const transitioned = await waitFor(
      `document.querySelector('#view-working') && document.querySelector('#view-working').classList.contains('active')`,
      8000
    );

    const screenState = await evaluate(`(() => {
      const working = document.querySelector('#view-working');
      const choose = document.querySelector('#view-choose');
      return {
        workingActive: working ? working.classList.contains('active') : false,
        chooseActive: choose ? choose.classList.contains('active') : false
      };
    })()`);

    if (!transitioned || screenState.workingActive === false || screenState.chooseActive === true) {
      throw new Error(`Screen failed to transition to working: ${JSON.stringify(screenState)}`);
    }

    if (uncaughtErrors.length > 0) {
      throw new Error(`Uncaught runtime exceptions occurred: ${JSON.stringify(uncaughtErrors)}`);
    }

    if (!downloadRequest) {
      throw new Error('No POST /api/download request was made');
    }

    console.log('SUCCESS: Grab button properly triggered download and screen transition!');
    console.log(`DOWNLOAD REQUEST: ${downloadRequest.method} ${downloadRequest.url}`);
    ws.close();
    process.exit(0);
  } catch (err) {
    console.error('FAIL:', err && err.message ? err.message : err);
    process.exit(1);
  } finally {
    if (ws) { try { ws.close(); } catch (e) {} }
    chrome.kill();
  }
}

runE2ETest();