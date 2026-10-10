import { spawn } from 'node:child_process';
import http from 'node:http';

async function runE2ETest() {
  const port = 9444;
  const chrome = spawn('chromium', [
    '--headless=new',
    '--disable-gpu',
    '--no-sandbox',
    `--remote-debugging-port=${port}`,
    'about:blank'
  ], { stdio: 'ignore' });

  await new Promise(r => setTimeout(r, 1500));

  let uncaughtErrors = [];
  let downloadRequest = null;

  try {
    const list = await new Promise((resolve, reject) => {
      http.get(`http://127.0.0.1:${port}/json`, res => {
        let data = '';
        res.on('data', chunk => data += chunk);
        res.on('end', () => resolve(JSON.parse(data)));
      }).on('error', reject);
    });

    const target = list.find(t => t.type === 'page') || list[0];
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    let msgId = 1;
    const callbacks = new Map();

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
      if (msg.method === 'Network.requestWillBeSent' && msg.params.request.url.includes('/api/download')) {
        downloadRequest = {
          url: msg.params.request.url,
          method: msg.params.request.method,
          postData: msg.params.request.postData
        };
      }
    };

    await new Promise(r => ws.onopen = r);

    function send(method, params = {}) {
      return new Promise(resolve => {
        const id = msgId++;
        callbacks.set(id, resolve);
        ws.send(JSON.stringify({ id, method, params }));
      });
    }

    await send('Runtime.enable');
    await send('Page.enable');
    await send('Network.enable');

    // 1. Visit root and prime sessionStorage with media info
    await send('Page.navigate', { url: 'http://127.0.0.1:8000/' });
    await new Promise(r => setTimeout(r, 1000));

    await send('Runtime.evaluate', {
      expression: `
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
      `
    });

    // 2. Navigate to /choose
    await send('Page.navigate', { url: 'http://127.0.0.1:8000/choose' });
    await new Promise(r => setTimeout(r, 1500));

    // 3. Verify on choose screen and click Grab button
    const clickRes = await send('Runtime.evaluate', {
      expression: `
        (() => {
          const chooseScreen = document.querySelector('#view-choose');
          if (!chooseScreen || !chooseScreen.classList.contains('active')) {
            return { error: 'Choose screen is not active' };
          }
          const btn = document.querySelector('#btn-start-download');
          if (!btn) {
            return { error: 'btn-start-download not found' };
          }
          btn.click();
          return { success: true };
        })()
      `,
      returnByValue: true
    });

    if (clickRes.result.value.error) {
      throw new Error(`Click setup failed: ${clickRes.result.value.error}`);
    }

    await new Promise(r => setTimeout(r, 1200));

    // 4. Verify working screen transition
    const stateRes = await send('Runtime.evaluate', {
      expression: `
        (() => {
          const working = document.querySelector('#view-working');
          const choose = document.querySelector('#view-choose');
          return {
            workingActive: working ? working.classList.contains('active') : false,
            chooseActive: choose ? choose.classList.contains('active') : false
          };
        })()
      `,
      returnByValue: true
    });

    const screenState = stateRes.result.value;
    if (!screenState.workingActive || screenState.chooseActive) {
      throw new Error(`Screen failed to transition to working: ${JSON.stringify(screenState)}`);
    }

    if (uncaughtErrors.length > 0) {
      throw new Error(`Uncaught runtime exceptions occurred: ${JSON.stringify(uncaughtErrors)}`);
    }

    if (!downloadRequest) {
      throw new Error('No POST /api/download request was made');
    }

    console.log('SUCCESS: Grab button properly triggered download and screen transition!');
    ws.close();
    process.exit(0);
  } catch (err) {
    console.error('FAIL:', err);
    process.exit(1);
  } finally {
    chrome.kill();
  }
}

runE2ETest();
