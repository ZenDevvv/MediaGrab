/**
 * MediaGrab - Native Client Controller
 * Lightweight PWA Single-Page Application
 */

(function () {
  'use strict';

  // --- Constants & Global State ---
  const API_BASE = '/api';

  const AppState = {
    screen: 'paste',
    currentUrl: '',
    metadata: null,
    mode: 'mp4', // 'mp4' | 'mp3'
    selectedQuality: null, // e.g. "1080p"
    selectedBitrate: 192,
    trim: {
      active: false,
      total: 0,
      start: 0,
      end: 0
    },
    subtitles: {
      lang: null,
      embed: true
    },
    activeJobId: null,
    eventSource: null,
    pollTimer: null,
    deletionTimer: null
  };

  // --- DOM Elements ---
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => document.querySelectorAll(selector);

  // Screens
  const screens = {
    paste: $('#view-paste'),
    choose: $('#view-choose'),
    trim: $('#view-trim'),
    subtitles: $('#view-subtitles'),
    working: $('#view-working'),
    ready: $('#view-ready'),
    error: $('#view-error'),
    offline: $('#view-offline')
  };

  // --- Helper Utilities ---
  function formatSeconds(sec) {
    if (isNaN(sec) || sec < 0) sec = 0;
    sec = Math.round(sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) {
      return `${h}:${m < 10 ? '0' : ''}${m}:${s < 10 ? '0' : ''}${s}`;
    }
    return `${m}:${s < 10 ? '0' : ''}${s}`;
  }

  function formatTimeMMSS(sec) {
    return formatSeconds(sec);
  }

  function parseTimeInput(val) {
    if (val === null || val === undefined) return null;
    val = String(val).trim();
    if (!val) return null;

    // Support e.g. 15m15s or 15m or 915s
    const mMatch = /^(\d+)m(?:(\d+)s)?$/i.exec(val);
    if (mMatch) {
      return parseInt(mMatch[1], 10) * 60 + (mMatch[2] ? parseInt(mMatch[2], 10) : 0);
    }
    const sMatch = /^(\d+)s$/i.exec(val);
    if (sMatch) {
      return parseInt(sMatch[1], 10);
    }

    const parts = val.split(':');
    if (parts.length === 2) {
      const m = parseInt(parts[0], 10);
      const s = parseInt(parts[1], 10);
      if (!isNaN(m) && !isNaN(s) && s < 60 && m >= 0) return m * 60 + s;
    } else if (parts.length === 3) {
      const h = parseInt(parts[0], 10);
      const m = parseInt(parts[1], 10);
      const s = parseInt(parts[2], 10);
      if (!isNaN(h) && !isNaN(m) && !isNaN(s) && h >= 0 && m < 60 && s < 60) return h * 3600 + m * 60 + s;
    }
    const raw = parseInt(val, 10);
    return (isNaN(raw) || raw < 0) ? null : raw;
  }

  function extractUrlTimestamp(urlStr) {
    try {
      const u = new URL(urlStr);
      const t = u.searchParams.get('t');
      if (t) return parseTimeInput(t);
    } catch (_) {}
    return null;
  }

  function formatBytes(bytes) {
    if (!bytes || bytes <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return (bytes / Math.pow(1024, i)).toFixed(i > 1 ? 1 : 0) + ' ' + units[i];
  }

  // --- Navigation & Router ---
  function showScreen(screenId) {
    Object.keys(screens).forEach((key) => {
      if (screens[key]) {
        screens[key].classList.toggle('active', key === screenId);
      }
    });
    AppState.screen = screenId;
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  // --- Theme Management ---
  const root = document.documentElement;
  function getSystemTheme() {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }

  function initTheme() {
    let saved = localStorage.getItem('mg-theme');
    if (saved === 'dark' || saved === 'light') {
      root.setAttribute('data-theme', saved);
    } else {
      root.removeAttribute('data-theme');
    }
    updateThemeColorMeta();
  }

  function toggleTheme() {
    const current = root.getAttribute('data-theme') || 'system';
    let next;
    if (current === 'system') {
      next = getSystemTheme() === 'dark' ? 'light' : 'dark';
    } else if (current === 'dark') {
      next = 'light';
    } else {
      next = 'dark';
    }
    root.setAttribute('data-theme', next);
    localStorage.setItem('mg-theme', next);
    updateThemeColorMeta();
  }

  function updateThemeColorMeta() {
    const isDark = root.getAttribute('data-theme') === 'dark' ||
      (!root.getAttribute('data-theme') && getSystemTheme() === 'dark');
    const color = isDark ? '#101722' : '#F5F7F8';
    let meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', color);
  }

  // --- History Management (Local Storage) ---
  const HISTORY_KEY = 'mg-history';

  function getHistory() {
    try {
      return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
    } catch {
      return [];
    }
  }

  function saveHistoryItem(item) {
    const list = getHistory();
    list.unshift({
      id: item.id || Date.now().toString(36),
      title: item.title,
      filename: item.filename,
      size: item.size,
      format: item.format,
      downloadUrl: item.downloadUrl,
      timestamp: Date.now()
    });
    localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, 20))); // Keep last 20
    renderHistory();
  }

  function clearHistory() {
    localStorage.removeItem(HISTORY_KEY);
    renderHistory();
  }

  function renderHistory() {
    const list = getHistory();
    const countEl = $('#history-count');
    if (countEl) countEl.textContent = list.length;

    const drawerList = $('#drawer-history-list');
    const sidepanelList = $('#sidepanel-history-list');

    if (!list.length) {
      const emptyHtml = '<p class="mono" style="padding: 12px 0;">No past downloads saved yet.</p>';
      if (drawerList) drawerList.innerHTML = emptyHtml;
      if (sidepanelList) sidepanelList.innerHTML = emptyHtml;
      return;
    }

    const html = list.map((item) => `
      <div class="file-card compact">
        <div class="nm">
          <svg class="ico" viewBox="0 0 24 24"><use href="#${item.format.toLowerCase().includes('mp3') ? 'i-audio' : 'i-video'}"/></svg>
          <code title="${item.filename}">${item.filename}</code>
          <span class="meta">${item.size || item.format}</span>
        </div>
      </div>
    `).join('');

    if (drawerList) drawerList.innerHTML = html;
    if (sidepanelList) sidepanelList.innerHTML = html;
  }

  // Drawer open / close
  function openHistoryDrawer() {
    $('#history-backdrop').classList.add('open');
    $('#history-drawer').classList.add('open');
  }
  function closeHistoryDrawer() {
    $('#history-backdrop').classList.remove('open');
    $('#history-drawer').classList.remove('open');
  }

  // --- Waveform & Trim Controller ---
  const BAR_COUNT = 56;
  function initTrimWaveform() {
    const container = $('#trim-bars-container');
    if (!container) return;
    container.innerHTML = '';
    for (let i = 0; i < BAR_COUNT; i++) {
      const bar = document.createElement('i');
      const height = (22 + Math.round(58 * Math.abs(Math.sin(i * 0.9) * Math.cos(i * 0.37 + 1))));
      bar.style.height = `${height}%`;
      container.appendChild(bar);
    }
  }

  function updateTrimUI(activeSlider = null) {
    const { total, start, end } = AppState.trim;
    const duration = Math.max(0, end - start);
    const isWhole = start === 0 && end === total;

    // Sliders
    const rs = $('#trim-s');
    const re = $('#trim-e');
    if (rs) {
      rs.max = total;
      if (activeSlider !== 'start') rs.value = start;
    }
    if (re) {
      re.max = total;
      if (activeSlider !== 'end') re.value = end;
    }

    // Text inputs (avoid clobbering input while user is actively typing)
    const ts = $('#t-start');
    const te = $('#t-end');
    if (ts && document.activeElement !== ts) ts.value = formatSeconds(start);
    if (te && document.activeElement !== te) te.value = formatSeconds(end);

    // Duration displays
    $('#ruler-start').textContent = '0:00';
    $('#ruler-end').textContent = formatSeconds(total);
    $('#trim-calc-dur').textContent = formatSeconds(duration);
    $('#trim-calc-note').textContent = `of ${formatSeconds(total)}`;

    const savingsPct = total > 0 ? Math.round((1 - duration / total) * 100) : 0;
    $('#trim-calc-savings').textContent = isWhole
      ? 'The whole video is downloaded.'
      : `Only this segment is downloaded, about ${savingsPct}% less bandwidth.`;

    // Highlight active bars
    const barEls = $$('#trim-bars-container i');
    barEls.forEach((b, k) => {
      const active = (k / BAR_COUNT >= start / total - 0.001) && (k / BAR_COUNT < end / total);
      b.classList.toggle('on', active);
    });

    // Update Chooser Screen Summary
    const summaryText = isWhole
      ? `Whole video · ${formatSeconds(total)}`
      : `${formatSeconds(start)} to ${formatSeconds(end)} · ${formatSeconds(duration)}`;
    $('#trim-summary-text').textContent = summaryText;
    $('#config-summary-clip').textContent = `Clip: ${summaryText}`;

    // Recalculate dynamic sizes
    recalcSizes();
  }

  function recalcSizes() {
    if (!AppState.metadata) return;
    const duration = AppState.trim.end - AppState.trim.start;
    const total = AppState.trim.total || 1;
    const ratio = Math.max(0.01, duration / total);

    // Update video quality chip size labels
    $$('#video-qualities-grid .chip').forEach((chip) => {
      const baseBytes = parseInt(chip.dataset.baseBytes || '0', 10);
      const estBytes = Math.round(baseBytes * ratio);
      const sizeEl = chip.querySelector('i');
      if (sizeEl) sizeEl.textContent = formatBytes(estBytes);
    });

    // Update audio rows
    [320, 192, 128].forEach((kbps) => {
      const bytes = Math.round((kbps * 1000 / 8) * duration);
      const el = $(`#audio-sz-${kbps}`);
      if (el) el.textContent = formatBytes(bytes);
    });

    // Update Action Button Label
    updateActionButtonLabel();
  }

  function updateActionButtonLabel() {
    const duration = AppState.trim.end - AppState.trim.start;
    const total = AppState.trim.total || 1;
    const ratio = Math.max(0.01, duration / total);

    let label = '';
    if (AppState.mode === 'mp4') {
      const selectedChip = $('#video-qualities-grid .chip.selected');
      const res = AppState.selectedQuality || '1080p';
      const baseBytes = selectedChip ? parseInt(selectedChip.dataset.baseBytes || '0', 10) : 0;
      const sizeStr = baseBytes > 0 ? ` · ${formatBytes(Math.round(baseBytes * ratio))}` : '';
      label = `Grab MP4 (${res})${sizeStr}`;
      $('#config-summary-details').textContent = `Quality: ${res}`;
      $('#config-badge-mode').textContent = 'MP4';
    } else {
      const kbps = AppState.selectedBitrate || 192;
      const bytes = Math.round((kbps * 1000 / 8) * duration);
      label = `Grab MP3 (${kbps} kbps) · ${formatBytes(bytes)}`;
      $('#config-summary-details').textContent = `Bitrate: ${kbps} kbps`;
      $('#config-badge-mode').textContent = 'MP3';
    }
    $('#btn-download-label').textContent = label;
  }

  // --- API Client: Metadata Inspection ---
  async function inspectUrl(url) {
    $('#inspect-error').hidden = true;
    $('#paste-input-container').classList.remove('bad');
    const grabBtn = $('#btn-grab');
    grabBtn.disabled = true;
    grabBtn.textContent = 'Inspecting...';

    try {
      const resp = await fetch(`${API_BASE}/info`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url })
      });

      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        throw new Error(errData.detail || 'Failed to inspect link.');
      }

      const data = await resp.json();
      AppState.metadata = data;
      AppState.currentUrl = url;

      // Populate Chooser
      populateChooser(data);
      showScreen('choose');
    } catch (err) {
      $('#paste-input-container').classList.add('bad');
      $('#inspect-error').textContent = err.message || 'Unable to inspect URL.';
      $('#inspect-error').hidden = false;
    } finally {
      grabBtn.disabled = false;
      grabBtn.textContent = 'Grab';
    }
  }

  function populateChooser(data) {
    // Title, Uploader, Duration, Thumbnail
    $('#choose-title').textContent = data.title || 'Untitled';
    $('#choose-uploader').textContent = data.uploader || 'Unknown Channel';
    $('#choose-duration').textContent = formatSeconds(data.duration);

    if (data.thumbnail) {
      const img = $('#choose-thumb-img');
      img.src = data.thumbnail;
      img.style.display = 'block';
      $('#choose-thumb-ph').style.display = 'none';
    }

    // Working & Ready Thumbnails
    const wImg = $('#working-thumb-img');
    if (wImg && data.thumbnail) {
      wImg.src = data.thumbnail;
      wImg.style.display = 'block';
      $('#working-thumb-ph').style.display = 'none';
    }
    $('#working-title').textContent = data.title;
    $('#working-duration').textContent = formatSeconds(data.duration);

    // Initialize Trim boundaries
    AppState.trim.total = data.duration || 60;
    const targetUrl = AppState.currentUrl || (data && data.url) || '';
    const urlTime = extractUrlTimestamp(targetUrl);
    if (urlTime !== null && urlTime < AppState.trim.total) {
      AppState.trim.start = urlTime;
      AppState.trim.end = AppState.trim.total;
      AppState.trim.active = true;
    } else {
      AppState.trim.start = 0;
      AppState.trim.end = data.duration || 60;
      AppState.trim.active = false;
    }
    initTrimWaveform();
    updateTrimUI();

    // Source Audio Bitrate Meter
    const kbps = data.source_audio_kbps || 128;
    const codec = data.source_audio_codec || 'Opus';
    $('#audio-source-label').textContent = `about ${kbps} kbps ${codec}`;

    const scalePct = Math.min(100, Math.max(0, (kbps / 320) * 100));
    $('#scale-marker-dot').style.left = `${scalePct}%`;
    $('#scale-marker-lbl').style.left = `${scalePct}%`;

    // Populate Video Qualities
    const qualitiesGrid = $('#video-qualities-grid');
    qualitiesGrid.innerHTML = '';
    const qualities = data.qualities && data.qualities.length ? data.qualities : [
      { resolution: '1080p', approx_bytes: 50000000, type: 'remux' },
      { resolution: '720p', approx_bytes: 25000000, type: 'remux' },
      { resolution: '480p', approx_bytes: 12000000, type: 'remux' }
    ];

    qualities.forEach((q, idx) => {
      const chip = document.createElement('div');
      chip.className = `chip ${idx === 0 ? 'selected' : ''}`;
      chip.dataset.resolution = q.resolution;
      chip.dataset.baseBytes = q.approx_bytes || 0;
      chip.innerHTML = `
        <b>${q.resolution}</b>
        <i>${formatBytes(q.approx_bytes)}</i>
        <span class="tag ${q.type === 'remux' ? 'fast' : 'slow'}">${q.type}</span>
      `;
      chip.addEventListener('click', () => {
        $$('#video-qualities-grid .chip').forEach((c) => c.classList.remove('selected'));
        chip.classList.add('selected');
        AppState.selectedQuality = q.resolution;
        updateActionButtonLabel();
      });
      qualitiesGrid.appendChild(chip);
    });
    AppState.selectedQuality = qualities[0].resolution;

    // Populate Subtitles
    const subsList = $('#subtitles-list');
    subsList.innerHTML = '';

    const noneRow = document.createElement('div');
    noneRow.className = 'audio-row selected';
    noneRow.dataset.lang = '';
    noneRow.innerHTML = `
      <span class="dot"></span>
      <span><span class="nm">None</span><span class="ds">No subtitle track</span></span>
    `;
    noneRow.addEventListener('click', () => selectSubtitleTrack(noneRow, null));
    subsList.appendChild(noneRow);

    if (data.subtitles && data.subtitles.length) {
      data.subtitles.forEach((s) => {
        const row = document.createElement('div');
        row.className = 'audio-row';
        row.dataset.lang = s.lang;
        row.innerHTML = `
          <span class="dot"></span>
          <span>
            <span class="nm">${s.label} ${s.is_auto ? '<small style="opacity:0.7;">(auto)</small>' : ''}</span>
            <span class="ds">${s.is_auto ? 'Machine generated' : 'Uploaded by creator'}</span>
          </span>
        `;
        row.addEventListener('click', () => selectSubtitleTrack(row, s.lang, s.label));
        subsList.appendChild(row);
      });
    }

    updateActionButtonLabel();
  }

  function selectSubtitleTrack(rowEl, lang, label) {
    $$('#subtitles-list .audio-row').forEach((r) => r.classList.remove('selected'));
    rowEl.classList.add('selected');
    AppState.subtitles.lang = lang;
    const txt = lang ? `${label || lang} · embedded in MP4` : 'None';
    $('#subs-summary-text').textContent = txt;
    $('#config-summary-captions').textContent = `Captions: ${txt}`;
  }

  // --- API Client: Download & SSE Pipeline ---
  async function startDownload() {
    const payload = {
      url: AppState.currentUrl,
      mode: AppState.mode,
      quality: AppState.selectedQuality,
      bitrate: AppState.selectedBitrate,
      trim: (AppState.trim.start > 0 || AppState.trim.end < AppState.trim.total) ? {
        start: AppState.trim.start,
        end: AppState.trim.end
      } : null,
      subtitles: AppState.subtitles.lang ? {
        lang: AppState.subtitles.lang,
        embed: $('#check-embed-subs').checked
      } : null
    };

    // Prepare Working Screen
    resetWorkingScreen();
    showScreen('working');

    try {
      const resp = await fetch(`${API_BASE}/download`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || 'Download request rejected.');
      }

      const data = await resp.json();
      AppState.activeJobId = data.job_id;
      connectSSE(data.job_id);
    } catch (err) {
      showError(err.message);
    }
  }

  function resetWorkingScreen() {
    $('#live-pct').innerHTML = '0<small>%</small>';
    $('#live-bar-fill').style.width = '0%';
    $('#live-clipper').style.left = '0%';
    $('#live-speed').textContent = '-- MB/s';
    $('#live-eta').textContent = '--:--';
    $('#live-got').textContent = '-- / --';

    $$('#live-steps-list li').forEach((li) => {
      li.className = 'todo';
      const st = li.querySelector('.st');
      if (st) st.innerHTML = '';
    });
    setStepState('step-inspect', 'done');
    setStepState('step-download', 'now');

    $('#working-spec-format').textContent = AppState.mode === 'mp4'
      ? `MP4 · ${AppState.selectedQuality}`
      : `MP3 · ${AppState.selectedBitrate} kbps`;
    $('#working-spec-clip').textContent = $('#trim-summary-text').textContent;
  }

  function setStepState(stepId, state) {
    const el = $(`#${stepId}`);
    if (!el) return;
    el.className = state;
    const st = el.querySelector('.st');
    if (st) {
      if (state === 'done') {
        st.innerHTML = '<svg class="ico" viewBox="0 0 24 24"><use href="#i-check"/></svg>';
      } else {
        st.innerHTML = '';
      }
    }
  }

  function connectSSE(jobId) {
    if (AppState.eventSource) {
      AppState.eventSource.close();
    }

    const sse = new EventSource(`${API_BASE}/events/${jobId}`);
    AppState.eventSource = sse;

    sse.addEventListener('progress', (e) => {
      try {
        const data = JSON.parse(e.data);
        handleProgressUpdate(data);
      } catch (err) {
        console.error('SSE parse error:', err);
      }
    });

    sse.addEventListener('complete', (e) => {
      try {
        const data = JSON.parse(e.data);
        sse.close();
        handleComplete(data);
      } catch (err) {
        console.error('SSE complete parse error:', err);
      }
    });

    sse.addEventListener('error', () => {
      sse.close();
      // Start polling fallback if SSE dropped prematurely
      startPollingFallback(jobId);
    });
  }

  function handleProgressUpdate(data) {
    const pct = Math.min(100, Math.max(0, Math.round(data.progress || 0)));
    $('#live-pct').innerHTML = `${pct}<small>%</small>`;
    $('#live-bar-fill').style.width = `${pct}%`;
    $('#live-clipper').style.left = `${pct}%`;

    if (data.speed) $('#live-speed').textContent = data.speed;
    if (data.eta) $('#live-eta').textContent = data.eta;
    if (data.got) $('#live-got').textContent = data.got;

    // Advance Stage steps based on server state string
    const stage = (data.stage || '').toLowerCase();
    if (data.stage) {
      if (stage.includes('trim')) {
        const stepDl = $('#step-download');
        if (stepDl) {
          stepDl.className = 'now';
          stepDl.innerHTML = `<span class="st"></span>${data.stage}`;
        }
      }
    }
    if (stage.includes('process') || stage.includes('remux') || stage.includes('join') || stage.includes('convert')) {
      setStepState('step-download', 'done');
      setStepState('step-process', 'now');
    } else if (stage.includes('caption') || stage.includes('tag') || stage.includes('art')) {
      setStepState('step-process', 'done');
      setStepState('step-subtitles', 'now');
    }
  }

  function handleComplete(data) {
    // Populate Ready Screen
    $('#ready-filename').textContent = data.filename || 'media-download';
    $('#ready-spec-format').textContent = AppState.mode === 'mp4' ? `MP4 ${AppState.selectedQuality}` : `MP3 ${AppState.selectedBitrate} kbps`;
    $('#ready-spec-duration').textContent = formatSeconds(AppState.trim.end - AppState.trim.start);
    $('#ready-spec-size').textContent = data.size || '-- MB';
    $('#ready-spec-captions').textContent = AppState.subtitles.lang ? 'Embedded' : 'None';

    const saveMediaBtn = $('#btn-save-media');
    saveMediaBtn.href = data.download_url;
    saveMediaBtn.setAttribute('download', data.filename || 'media');
    $('#btn-save-label').textContent = `Save ${AppState.mode.toUpperCase()}`;

    // Subtitle card if generated
    if (data.srt_url) {
      $('#ready-srt-card').hidden = false;
      $('#ready-srt-filename').textContent = data.srt_filename || 'subtitles.srt';
      const subsBtn = $('#btn-save-subtitles');
      subsBtn.hidden = false;
      subsBtn.onclick = () => { window.location.href = data.srt_url; };
    } else {
      $('#ready-srt-card').hidden = true;
      $('#btn-save-subtitles').hidden = true;
    }

    // Save to History
    saveHistoryItem({
      id: AppState.activeJobId,
      title: AppState.metadata ? AppState.metadata.title : 'Media Download',
      filename: data.filename,
      size: data.size,
      format: AppState.mode.toUpperCase(),
      downloadUrl: data.download_url
    });

    // Start 30-min deletion countdown
    startDeletionCountdown(30 * 60);

    showScreen('ready');
  }

  function startPollingFallback(jobId) {
    if (AppState.pollTimer) clearInterval(AppState.pollTimer);
    AppState.pollTimer = setInterval(async () => {
      try {
        const resp = await fetch(`${API_BASE}/progress/${jobId}`);
        if (!resp.ok) return;
        const job = await resp.json();
        if (job.status === 'ready') {
          clearInterval(AppState.pollTimer);
          handleComplete({
            filename: job.filename,
            size: job.size,
            download_url: `/api/file/${jobId}`
          });
        } else if (job.status === 'error') {
          clearInterval(AppState.pollTimer);
          showError(job.error || 'Download failed.');
        } else {
          handleProgressUpdate(job);
        }
      } catch {
        // Continue polling
      }
    }, 1000);
  }

  function startDeletionCountdown(seconds) {
    if (AppState.deletionTimer) clearInterval(AppState.deletionTimer);
    let left = seconds;
    const timerEl = $('#ready-deletion-timer');

    AppState.deletionTimer = setInterval(() => {
      left--;
      if (left <= 0) {
        clearInterval(AppState.deletionTimer);
        if (timerEl) timerEl.textContent = 'Expired';
      } else if (timerEl) {
        timerEl.textContent = formatTimeMMSS(left);
      }
    }, 1000);
  }

  function showError(msg) {
    if (AppState.eventSource) AppState.eventSource.close();
    if (AppState.pollTimer) clearInterval(AppState.pollTimer);
    $('#error-desc-msg').textContent = msg || 'Could not process media stream.';
    $('#error-details-text').textContent = `Details: ${msg || 'Unknown error'}`;
    $('#error-url-retry').value = AppState.currentUrl;
    showScreen('error');
  }

  // --- Instant SRT Download ---
  async function downloadInstantSrt() {
    if (!AppState.currentUrl) return;
    const btn = $('#btn-save-srt-instant');
    btn.disabled = true;
    btn.textContent = 'Fetching...';

    try {
      const resp = await fetch(`${API_BASE}/download`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          url: AppState.currentUrl,
          mode: 'srt',
          subtitles: { lang: AppState.subtitles.lang || 'en' }
        })
      });
      if (!resp.ok) throw new Error('Subtitles unavailable.');
      const data = await resp.json();

      // Poll briefly for instant srt completion
      let attempts = 0;
      const poll = setInterval(async () => {
        attempts++;
        const pResp = await fetch(`${API_BASE}/progress/${data.job_id}`);
        const pData = await pResp.json();
        if (pData.status === 'ready' || attempts > 10) {
          clearInterval(poll);
          btn.disabled = false;
          btn.textContent = 'Save .srt';
          window.location.href = `/api/file/${data.job_id}`;
        }
      }, 500);
    } catch (err) {
      alert(err.message || 'Could not extract subtitles.');
      btn.disabled = false;
      btn.textContent = 'Save .srt';
    }
  }

  // --- Event Listeners Setup ---
  function setupEventListeners() {
    // Theme Switch
    $('#btn-theme-flip').addEventListener('click', toggleTheme);

    // History Drawer
    $('#btn-open-history').addEventListener('click', openHistoryDrawer);
    $('#btn-close-history').addEventListener('click', closeHistoryDrawer);
    $('#history-backdrop').addEventListener('click', closeHistoryDrawer);
    $('#btn-clear-history').addEventListener('click', clearHistory);

    // Form Inspect
    $('#form-inspect').addEventListener('submit', (e) => {
      e.preventDefault();
      const url = $('#input-url').value.trim();
      if (url) inspectUrl(url);
    });

    // Format Switching
    $('#btn-fmt-video').addEventListener('click', () => {
      AppState.mode = 'mp4';
      $('#btn-fmt-video').classList.add('active');
      $('#btn-fmt-audio').classList.remove('active');
      $('#pane-video').hidden = false;
      $('#pane-audio').hidden = true;
      updateActionButtonLabel();
    });

    $('#btn-fmt-audio').addEventListener('click', () => {
      AppState.mode = 'mp3';
      $('#btn-fmt-audio').classList.add('active');
      $('#btn-fmt-video').classList.remove('active');
      $('#pane-video').hidden = true;
      $('#pane-audio').hidden = false;
      updateActionButtonLabel();
    });

    // Audio Bitrate Selection
    $$('#audio-rows-grid .audio-row').forEach((row) => {
      row.addEventListener('click', () => {
        $$('#audio-rows-grid .audio-row').forEach((r) => r.classList.remove('selected'));
        row.classList.add('selected');
        AppState.selectedBitrate = parseInt(row.dataset.bitrate, 10);
        updateActionButtonLabel();
      });
    });

    // Step Navigations
    $('#row-open-trim').addEventListener('click', () => {
      const errEl = $('#trim-error');
      if (errEl) errEl.hidden = true;
      updateTrimUI();
      showScreen('trim');
    });
    $('#btn-trim-back').addEventListener('click', () => showScreen('choose'));

    $('#btn-trim-apply').addEventListener('click', () => {
      const ts = $('#t-start');
      const te = $('#t-end');
      const s = parseTimeInput(ts ? ts.value : '');
      const e = parseTimeInput(te ? te.value : '');
      const total = AppState.trim.total || 60;

      if (s === null || e === null || s < 0 || e <= s || e > total) {
        const errEl = $('#trim-error');
        if (errEl) {
          errEl.textContent = `Invalid clip range. End must come after start and fit within ${formatSeconds(total)}.`;
          errEl.hidden = false;
        }
        return;
      }

      const errEl = $('#trim-error');
      if (errEl) errEl.hidden = true;
      AppState.trim.start = s;
      AppState.trim.end = e;
      AppState.trim.active = (s > 0 || e < total);
      updateTrimUI();
      showScreen('choose');
    });

    $('#btn-trim-reset').addEventListener('click', () => {
      AppState.trim.start = 0;
      AppState.trim.end = AppState.trim.total;
      AppState.trim.active = false;
      const errEl = $('#trim-error');
      if (errEl) errEl.hidden = true;
      updateTrimUI();
      showScreen('choose');
    });

    $('#row-open-subtitles').addEventListener('click', () => showScreen('subtitles'));
    $('#btn-subs-back').addEventListener('click', () => showScreen('choose'));
    $('#btn-subs-done').addEventListener('click', () => showScreen('choose'));
    $('#btn-save-srt-instant').addEventListener('click', downloadInstantSrt);

    // Trim Sliders
    const rs = $('#trim-s');
    const re = $('#trim-e');
    if (rs && re) {
      rs.addEventListener('input', () => {
        let v = parseInt(rs.value, 10);
        if (v >= AppState.trim.end) {
          v = AppState.trim.end - 1;
          rs.value = v;
        }
        AppState.trim.start = Math.max(0, v);
        rs.style.zIndex = '4';
        re.style.zIndex = '3';
        const errEl = $('#trim-error');
        if (errEl) errEl.hidden = true;
        updateTrimUI('start');
      });

      re.addEventListener('input', () => {
        let v = parseInt(re.value, 10);
        if (v <= AppState.trim.start) {
          v = AppState.trim.start + 1;
          re.value = v;
        }
        AppState.trim.end = Math.min(AppState.trim.total, v);
        re.style.zIndex = '4';
        rs.style.zIndex = '3';
        const errEl = $('#trim-error');
        if (errEl) errEl.hidden = true;
        updateTrimUI('end');
      });
    }

    // Waveform click seeking
    const waveBox = $('.wavebox');
    if (waveBox) {
      waveBox.addEventListener('click', (e) => {
        if (e.target === rs || e.target === re) return;
        const wave = $('.wave');
        if (!wave) return;
        const rect = wave.getBoundingClientRect();
        const clientX = e.clientX;
        if (clientX < rect.left || clientX > rect.right) return;
        const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
        const clickedSec = Math.round(ratio * AppState.trim.total);
        const distToStart = Math.abs(clickedSec - AppState.trim.start);
        const distToEnd = Math.abs(clickedSec - AppState.trim.end);
        if (distToStart < distToEnd) {
          if (clickedSec < AppState.trim.end) {
            AppState.trim.start = clickedSec;
            if (rs) rs.style.zIndex = '4';
            if (re) re.style.zIndex = '3';
            updateTrimUI();
          }
        } else {
          if (clickedSec > AppState.trim.start) {
            AppState.trim.end = clickedSec;
            if (re) re.style.zIndex = '4';
            if (rs) rs.style.zIndex = '3';
            updateTrimUI();
          }
        }
      });
    }

    // Trim Text Inputs
    const ts = $('#t-start');
    const te = $('#t-end');
    const handleInputCommit = () => {
      const s = parseTimeInput(ts ? ts.value : '');
      const e = parseTimeInput(te ? te.value : '');
      const total = AppState.trim.total || 60;
      const errEl = $('#trim-error');
      if (s !== null && e !== null && s >= 0 && e > s && e <= total) {
        AppState.trim.start = s;
        AppState.trim.end = e;
        AppState.trim.active = (s > 0 || e < total);
        if (errEl) errEl.hidden = true;
        updateTrimUI();
      } else {
        if (errEl) {
          errEl.textContent = `Invalid clip range. End must come after start and fit within ${formatSeconds(total)}.`;
          errEl.hidden = false;
        }
      }
    };
    if (ts) {
      ts.addEventListener('change', handleInputCommit);
      ts.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); handleInputCommit(); } });
    }
    if (te) {
      te.addEventListener('change', handleInputCommit);
      te.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); handleInputCommit(); } });
    }

    // Start Download Button
    $('#btn-start-download').addEventListener('click', startDownload);
    $('#btn-choose-back').addEventListener('click', () => showScreen('paste'));

    // Cancel Download Button
    $('#btn-cancel-download').addEventListener('click', () => {
      if (AppState.eventSource) AppState.eventSource.close();
      if (AppState.pollTimer) clearInterval(AppState.pollTimer);
      showScreen('choose');
    });

    // Grab Another Link
    $('#btn-grab-another').addEventListener('click', () => {
      $('#input-url').value = '';
      showScreen('paste');
    });
    $('#btn-error-retry').addEventListener('click', () => showScreen('paste'));
    $('#btn-offline-retry').addEventListener('click', () => {
      if (navigator.onLine) showScreen('paste');
      else alert('Still offline. Please check your network connection.');
    });

    // Brand Home link
    $('#brand-home').addEventListener('click', (e) => {
      e.preventDefault();
      showScreen('paste');
    });

    // Connectivity Status
    window.addEventListener('online', () => {
      if (AppState.screen === 'offline') showScreen('paste');
    });
    window.addEventListener('offline', () => {
      showScreen('offline');
    });
  }

  // --- PWA Service Worker & Install Prompt ---
  function initPWA() {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch((err) => {
        console.warn('SW registration failed:', err);
      });
    }

    let deferredPrompt = null;
    window.addEventListener('beforeinstallprompt', (e) => {
      e.preventDefault();
      deferredPrompt = e;
      const strip = $('#pwa-install-strip');
      if (strip && !localStorage.getItem('mg-dismiss-install')) {
        strip.hidden = false;
      }
    });

    const installBtn = $('#btn-pwa-install');
    if (installBtn) {
      installBtn.addEventListener('click', async () => {
        if (!deferredPrompt) return;
        deferredPrompt.prompt();
        const choice = await deferredPrompt.userChoice;
        if (choice.outcome === 'accepted') {
          $('#pwa-install-strip').hidden = true;
        }
        deferredPrompt = null;
      });
    }

    const dismissBtn = $('#btn-pwa-dismiss');
    if (dismissBtn) {
      dismissBtn.addEventListener('click', () => {
        $('#pwa-install-strip').hidden = true;
        localStorage.setItem('mg-dismiss-install', 'true');
      });
    }
  }

  // --- App Initialization ---
  document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    renderHistory();
    setupEventListeners();
    initPWA();

    if (!navigator.onLine) {
      showScreen('offline');
    }
  });

})();
