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
    deletionTimer: null,
    capabilities: null
  };

  // --- Capabilities (§1.2 Decision #9) ---
  // Mode is read from the server-rendered meta tag and is authoritative: it
  // cannot race, and it survives a capabilities endpoint that 404s against a
  // stale backend. /api/capabilities supplies only numeric limits and download
  // URLs. If that fetch fails we fall back to HOSTED_SAFE_DEFAULTS rather than
  // blocking the UI -- desktop applies no server-side guardrails, so assuming
  // "unlimited" here would misadvertise capability, while assuming hosted merely
  // mislabels it.
  const HOSTED_SAFE_DEFAULTS = Object.freeze({
    mode: 'hosted',
    limits: {
      max_resolution: '1080p',
      max_duration_seconds: 3600,
      max_file_size_bytes: 1073741824,
      locked_resolutions: ['1440p', '2160p']
    },
    downloads: { windows_url: null, linux_url: null, android_url: null }
  });

  function readMeta(name, fallback) {
    const el = document.querySelector(`meta[name="${name}"]`);
    return el ? el.getAttribute('content') : fallback;
  }

  function readMetaInt(name, fallback) {
    const raw = readMeta(name, null);
    const parsed = raw === null ? NaN : parseInt(raw, 10);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  // Mode resolution must be synchronous -- it is needed before the first render.
  function resolveMode() {
    const raw = readMeta('mg-mode', null);
    return raw === 'hosted' ? 'hosted' : 'desktop';
  }

  AppState.mode_ = resolveMode();
  AppState.retentionSeconds = readMetaInt('mg-retention-seconds', 1800);

  function isHostedMode() {
    return AppState.mode_ === 'hosted';
  }

  function lockedResolutions() {
    const fromApi = AppState.capabilities
      && AppState.capabilities.limits
      && AppState.capabilities.limits.locked_resolutions;
    const list = Array.isArray(fromApi) && fromApi.length
      ? fromApi
      : HOSTED_SAFE_DEFAULTS.limits.locked_resolutions;
    return new Set(list);
  }

  function maxDurationSeconds() {
    const fromApi = AppState.capabilities
      && AppState.capabilities.limits
      && AppState.capabilities.limits.max_duration_seconds;
    return Number.isFinite(fromApi) ? fromApi : HOSTED_SAFE_DEFAULTS.limits.max_duration_seconds;
  }

  function maxFileSizeBytes() {
    const fromApi = AppState.capabilities
      && AppState.capabilities.limits
      && AppState.capabilities.limits.max_file_size_bytes;
    return Number.isFinite(fromApi) ? fromApi : HOSTED_SAFE_DEFAULTS.limits.max_file_size_bytes;
  }

  async function loadCapabilities() {
    try {
      const resp = await fetch(`${API_BASE}/capabilities`);
      if (!resp.ok) throw new Error(`capabilities ${resp.status}`);
      const data = await resp.json();
      // The server tag stays authoritative for mode; a mismatched payload is
      // ignored rather than allowed to flip the page's gating.
      AppState.capabilities = {
        ...data,
        mode: AppState.mode_,
        limits: { ...HOSTED_SAFE_DEFAULTS.limits, ...(data.limits || {}) },
        downloads: data.downloads || {}
      };
      if (Number.isFinite(data.retention_seconds)) {
        AppState.retentionSeconds = data.retention_seconds;
        renderRetentionCopy();
      }
    } catch (e) {
      // Hosted-safe defaults. Never block the page on this.
      AppState.capabilities = {
        ...HOSTED_SAFE_DEFAULTS,
        mode: AppState.mode_,
        limits: {
          ...HOSTED_SAFE_DEFAULTS.limits,
          ...(AppState.capabilities ? AppState.capabilities.limits : {})
        },
        downloads: (AppState.capabilities && AppState.capabilities.downloads) || {}
      };
      console.warn('MediaGrab: capabilities unavailable, using conservative defaults.', e);
    }
    return AppState.capabilities;
  }

  // --- DOM Elements ---
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => document.querySelectorAll(selector);

  // Active view screens
  const ALL_SCREENS = ['paste', 'choose', 'working', 'ready', 'error', 'offline'];

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
    if (isNaN(sec) || sec < 0) sec = 0;
    sec = Math.round(sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) {
      return `${h}:${m < 10 ? '0' : ''}${m}:${s < 10 ? '0' : ''}${s}`;
    }
    return `${m < 10 ? '0' : ''}${m}:${s < 10 ? '0' : ''}${s}`;
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
    ALL_SCREENS.forEach((key) => {
      const el = $(`#view-${key}`);
      if (el) {
        el.classList.toggle('active', key === screenId);
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
      <div class="frow">
        <svg class="ico" viewBox="0 0 24 24" aria-hidden="true"><use href="#${item.format && item.format.toLowerCase().includes('mp3') ? 'i-audio' : 'i-video'}"/></svg>
        <div>
          <code title="${item.filename}">${item.filename}</code>
          <span class="mono">${item.format} · ${item.size || 'Ready'} · gone in 30:00</span>
        </div>
        <a class="btn btn-sm" href="${item.downloadUrl || '#'}" download="${item.filename}"><span>Save</span></a>
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
    // Keep the clip-aware hosted limit in sync with the trim handles.
    updateTrimLimitBanner();

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
    if (ts && document.activeElement !== ts) ts.value = formatTimeMMSS(start);
    if (te && document.activeElement !== te) te.value = formatTimeMMSS(end);

    // Duration displays
    const rStart = $('#ruler-start');
    if (rStart) rStart.textContent = '0:00';
    const rEnd = $('#ruler-end');
    if (rEnd) rEnd.textContent = formatSeconds(total);
    const clipDisp = $('#t-clip-display');
    if (clipDisp) clipDisp.innerHTML = `${formatSeconds(duration)} <small id="t-clip-total">of ${formatSeconds(total)}</small>`;
    const calcDur = $('#trim-calc-dur');
    if (calcDur) calcDur.textContent = formatSeconds(duration);
    const calcNote = $('#trim-calc-note');
    if (calcNote) calcNote.textContent = `of ${formatSeconds(total)}`;
    const calcSavings = $('#trim-calc-savings');
    if (calcSavings) {
      const savingsPct = total > 0 ? Math.round((1 - duration / total) * 100) : 0;
      calcSavings.textContent = isWhole
        ? 'The whole video is downloaded.'
        : `Only this segment is downloaded, about ${savingsPct}% less bandwidth.`;
    }

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
    const trimSummaryEl = $('#trim-summary-text');
    if (trimSummaryEl) trimSummaryEl.textContent = summaryText;
    const cfgClip = $('#config-summary-clip');
    if (cfgClip) cfgClip.textContent = `Clip: ${summaryText}`;

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
    const duration = Math.max(1, AppState.trim.end - AppState.trim.start);
    const total = AppState.trim.total || 1;
    const ratio = Math.max(0.01, duration / total);
    const isWhole = (AppState.trim.start === 0 && AppState.trim.end === total);
    const clipStr = isWhole ? 'Whole video' : `${formatSeconds(AppState.trim.start)} to ${formatSeconds(AppState.trim.end)}`;

    let label = '';
    let spec = '';

    if (AppState.mode === 'mp4') {
      const selectedChip = $('#video-qualities-grid .chip.selected');
      const res = AppState.selectedQuality || '1080p';
      const baseBytes = selectedChip ? parseInt(selectedChip.dataset.baseBytes || '0', 10) : 0;
      const sizeStr = baseBytes > 0 ? ` · ${formatBytes(Math.round(baseBytes * ratio))}` : '';
      label = `Grab MP4${sizeStr}`;

      const subsName = $('#subs-track-name')?.textContent;
      const subsStr = AppState.subtitles && AppState.subtitles.lang && subsName && subsName !== 'None' ? `${subsName} captions` : 'No captions';
      spec = `MP4 · ${res} · ${clipStr} · ${subsStr}`;

      const cfgMode = $('#config-badge-mode');
      if (cfgMode) cfgMode.textContent = 'MP4';
      const cfgDetails = $('#config-summary-details');
      if (cfgDetails) cfgDetails.textContent = `Quality: ${res}`;
    } else {
      const kbps = AppState.selectedBitrate || 192;
      const bytes = Math.round((kbps * 1000 / 8) * duration);
      label = `Grab MP3 · ${formatBytes(bytes)}`;
      spec = `MP3 · ${kbps} kbps · ${clipStr}`;

      const cfgMode = $('#config-badge-mode');
      if (cfgMode) cfgMode.textContent = 'MP3';
      const cfgDetails = $('#config-summary-details');
      if (cfgDetails) cfgDetails.textContent = `Bitrate: ${kbps} kbps`;
    }

    const btnLabel = $('#btn-download-label');
    if (btnLabel) btnLabel.textContent = label;
    const specEl = $('#config-summary-spec');
    if (specEl) specEl.textContent = spec;

    // Block the grab when the clip is definitively over the hosted limit, so the
    // user gets guidance instead of a server rejection. SRT is exempt.
    const startBtn = $('#btn-start-download');
    if (startBtn) {
      const overLimit = isHostedMode()
        && AppState.mode !== 'srt'
        && currentClipDuration() > maxDurationSeconds();
      startBtn.disabled = overLimit;
      startBtn.title = overLimit
        ? 'Trim the clip to fit the hosted limit, or use the native app.'
        : '';
    }
  }

  // --- API Client: Metadata Inspection ---
  async function inspectUrl(url) {
    const errEl = $('#inspect-error');
    if (errEl) errEl.hidden = true;
    const inpCont = $('#paste-input-container');
    if (inpCont) inpCont.classList.remove('bad');
    const grabBtn = $('#btn-grab');
    if (grabBtn) {
      grabBtn.disabled = true;
      grabBtn.textContent = 'Inspecting...';
    }

    try {
      const resp = await fetch(`${API_BASE}/info`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url })
      });

      if (resp.status === 451) {
        showYouTubeUpsell(detailPayload(await resp.json().catch(() => ({}))));
        return;
      }
      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        const payload = detailPayload(errData);
        throw new Error(payload.message || 'Failed to inspect link.');
      }

      const data = await resp.json();
      hideYouTubeUpsell();
      if (errEl) errEl.hidden = true;
      AppState.metadata = data;
      AppState.currentUrl = url;

      // Always persist active media in sessionStorage for page refreshes & direct routing
      try {
        sessionStorage.setItem('mg_active_meta', JSON.stringify(data));
        sessionStorage.setItem('mg_active_url', url);
      } catch (e) {}

      // If on landing page (/), navigate to /choose
      if ($('#view-paste')) {
        window.location.href = '/choose';
        return;
      }

      // If already on /choose
      populateChooser(data);
      showScreen('choose');
    } catch (err) {
      if (inpCont) inpCont.classList.add('bad');
      if (errEl) {
        errEl.textContent = err.message || 'Unable to inspect URL.';
        errEl.hidden = false;
      } else {
        alert(err.message || 'Unable to inspect URL.');
      }
    } finally {
      if (grabBtn) {
        grabBtn.disabled = false;
        grabBtn.textContent = 'Grab';
      }
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
      const locked = isResolutionLocked(q.resolution);
      const chip = document.createElement('div');
      chip.className = `chip ${idx === 0 ? 'selected' : ''}`;
      chip.dataset.resolution = q.resolution;
      chip.dataset.baseBytes = q.approx_bytes || 0;
      chip.innerHTML = `
        <b>${q.resolution}</b>
        <i>${formatBytes(q.approx_bytes)}</i>
        <span class="tag ${q.type === 'remux' ? 'fast' : 'slow'}">${q.type}</span>
        ${locked ? '<span class="tag locked-tag">Desktop/Android only</span>' : ''}
      `;
      if (locked) {
        chip.classList.add('locked');
        chip.setAttribute('aria-disabled', 'true');
        chip.setAttribute('title', 'Available in the Desktop or Android app');
      }
      chip.addEventListener('click', () => {
        if (isResolutionLocked(q.resolution)) return;
        $$('#video-qualities-grid .chip').forEach((c) => c.classList.remove('selected'));
        chip.classList.add('selected');
        AppState.selectedQuality = q.resolution;
        updateActionButtonLabel();
      });
      qualitiesGrid.appendChild(chip);
    });

    // Never default to a locked resolution: a stale preference (or a source whose
    // top quality is hosted-locked) must not silently produce a rejected grab.
    AppState.selectedQuality = isResolutionLocked(qualities[0].resolution)
      ? (highestAllowedResolution(qualities) || qualities[0].resolution)
      : qualities[0].resolution;
    $$('#video-qualities-grid .chip').forEach((c) => {
      c.classList.toggle('selected', c.dataset.resolution === AppState.selectedQuality);
    });
    applyQualityGating(qualities);
    updateTrimLimitBanner();

    // Populate Subtitles
    const subsTrackRow = $('#row-subs-track');
    const subsTrackName = $('#subs-track-name');
    const subsTrackDesc = $('#subs-track-desc');
    const subsDoneBtn = $('#btn-save-srt-instant');

    if (data.subtitles && data.subtitles.length) {
      const s = data.subtitles[0];
      AppState.subtitles.lang = s.lang;
      if (subsTrackName) subsTrackName.textContent = s.label || s.lang;
      if (subsTrackDesc) subsTrackDesc.textContent = s.is_auto ? 'Machine generated captions' : 'Written by the creator';
      if (subsDoneBtn) subsDoneBtn.style.display = 'inline-flex';

      if (subsTrackRow && data.subtitles.length > 1) {
        let subIdx = 0;
        subsTrackRow.style.cursor = 'pointer';
        subsTrackRow.onclick = (e) => {
          if (e.target.closest('#btn-save-srt-instant')) return;
          subIdx = (subIdx + 1) % (data.subtitles.length + 1);
          if (subIdx === data.subtitles.length) {
            AppState.subtitles.lang = null;
            if (subsTrackName) subsTrackName.textContent = 'None';
            if (subsTrackDesc) subsTrackDesc.textContent = 'No captions';
            if (subsDoneBtn) subsDoneBtn.style.display = 'none';
          } else {
            const cur = data.subtitles[subIdx];
            AppState.subtitles.lang = cur.lang;
            if (subsTrackName) subsTrackName.textContent = cur.label || cur.lang;
            if (subsTrackDesc) subsTrackDesc.textContent = cur.is_auto ? 'Machine generated' : 'Written by the creator';
            if (subsDoneBtn) subsDoneBtn.style.display = 'inline-flex';
          }
          updateActionButtonLabel();
        };
      }
    } else {
      AppState.subtitles.lang = null;
      if (subsTrackName) subsTrackName.textContent = 'None';
      if (subsTrackDesc) subsTrackDesc.textContent = 'No subtitle tracks available';
      if (subsDoneBtn) subsDoneBtn.style.display = 'none';
    }

    const subsList = $('#subtitles-list');
    if (subsList) {
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
    }

    updateActionButtonLabel();
  }

  function selectSubtitleTrack(rowEl, lang, label) {
    $$('#subtitles-list .audio-row').forEach((r) => r.classList.remove('selected'));
    rowEl.classList.add('selected');
    AppState.subtitles.lang = lang;
    const txt = lang ? `${label || lang} · embedded in MP4` : 'None';
    const subSummary = $('#subs-summary-text');
    if (subSummary) subSummary.textContent = txt;
    const cfgCaptions = $('#config-summary-captions');
    if (cfgCaptions) cfgCaptions.textContent = `Captions: ${txt}`;
  }

  // --- Promo Strip & Smart Device Detection (§1.2 Decision #3) ---
  const PLATFORM_TARGETS = [
    { id: 'windows_url', key: 'windows', label: 'Windows', cta: 'Download Windows App (.exe)' },
    { id: 'linux_url', key: 'linux', label: 'Linux', cta: 'Download Linux App' },
    { id: 'android_url', key: 'android', label: 'Android', cta: 'Download Android App (.apk)' }
  ];

  function detectPlatform() {
    const ua = navigator.userAgent || '';
    const touch = navigator.maxTouchPoints || 0;
    // Order matters: Android UA also contains "Linux" and iPad UA can claim
    // "Macintosh", so the more specific patterns are tested first.
    if (/Android/i.test(ua)) return 'android';
    if (/iPhone|iPad|iPod/i.test(ua)) return 'ios';
    if (/Windows/i.test(ua)) return 'windows';
    if (/Macintosh|Mac OS X/i.test(ua)) return 'ios';
    if (/CrOS/i.test(ua)) return 'linux';
    if (/Linux|X11/i.test(ua)) return 'linux';
    // Touch-only without a recognizable desktop UA is most likely a tablet.
    if (touch > 1 && !/Windows|Macintosh/i.test(ua)) return 'android';
    return 'linux';
  }

  function isRealDownload(url) {
    // A relative URL means the server has not published binaries yet and is
    // pointing at the "coming soon" page. The markup carries a `download`
    // attribute intended for real artifacts; leaving it on a page link would
    // save the HTML file instead of navigating (§1.4 Decision #16).
    return Boolean(url) && !/^\//.test(url);
  }

  function applyDownloadTargets() {
    const downloads = (AppState.capabilities && AppState.capabilities.downloads) || {};
    PLATFORM_TARGETS.forEach((t) => {
      const el = $(`#hosted-promo-${t.key}`);
      if (!el) return;
      const url = downloads[t.id];
      if (isRealDownload(url)) {
        el.href = url;
        el.setAttribute('download', '');
        el.removeAttribute('aria-disabled');
        el.title = 'Download the MediaGrab app';
      } else {
        // Either binaries are unpublished (relative URL) or capabilities failed.
        // Keep the control visible and honest instead of linking to a 404.
        el.href = url || '/downloads';
        el.removeAttribute('download');
        el.removeAttribute('aria-disabled');
        el.title = 'App downloads are not published yet';
      }
    });

    const cta = $('#hosted-promo-cta');
    if (cta) {
      const target = PLATFORM_TARGETS.find((t) => t.key === detectPlatform()) || PLATFORM_TARGETS[0];
      const url = downloads[target.id];
      cta.textContent = target.cta;
      if (isRealDownload(url)) {
        cta.href = url;
        cta.setAttribute('download', '');
        cta.removeAttribute('aria-disabled');
      } else {
        cta.href = url || '/downloads';
        cta.removeAttribute('download');
        cta.removeAttribute('aria-disabled');
      }
    }
  }

  // Retention is server-defined (60s hosted, 1800s desktop), so the copy must
  // not hardcode it. Hardcoded "30 min" was showing on a hosted instance that
  // deletes 60 seconds after retrieval.
  function formatRetention(seconds) {
    const s = Math.max(1, Math.round(Number(seconds) || 0));
    if (s < 120) return `${s}s`;
    if (s < 3600) return `${Math.round(s / 60)} min`;
    const hours = s / 3600;
    return `${hours % 1 === 0 ? hours : hours.toFixed(1)} hr`;
  }

  // Spelled out for running prose. formatRetention returns the terse "60s"/"5 min"
// used in labels, which reads badly mid-sentence.
  function retentionPhrase(seconds) {
    const s = Math.max(1, Math.round(Number(seconds) || 0));
    if (s < 120) return `${s} second${s === 1 ? '' : 's'}`;
    if (s < 3600) return `${Math.round(s / 60)} minute${Math.round(s / 60) === 1 ? '' : 's'}`;
    const h = Math.round(s / 3600);
    return `${h} hour${h === 1 ? '' : 's'}`;
  }

  function renderRetentionCopy() {
    const label = formatRetention(AppState.retentionSeconds);
    const side = $('#retention-side-label');
    if (side) side.textContent = `Auto-deletes ${label} after save`;
    const drawer = $('#retention-drawer-note');
    if (drawer) drawer.textContent = `Saved in this browser · Server copy deleted ${label} after you save`;
  }

  function initPromoStrip() {
    const strip = $('#hosted-promo-strip');
    if (!strip) return;

    // Only meaningful where limits actually apply.
    if (!isHostedMode()) {
      strip.hidden = true;
      return;
    }

    strip.hidden = false;

    const platform = detectPlatform();
    const text = $('#hosted-promo-text');
    if (text) {
      if (platform === 'ios') {
        text.textContent = 'iPhone and iPad: downloads are capped here. The native app unlocks 4K and unlimited size.';
      } else if (platform === 'android') {
        text.textContent = 'Download directly on your phone with the Android app — no limits.';
      } else {
        text.textContent = 'Get 4K, unlimited size, and no time limit with the native app.';
      }
    }

    // iOS has no native client; don't advertise a desktop download as primary.
    const cta = $('#hosted-promo-cta');
    if (cta && platform === 'ios') {
      cta.hidden = true;
    }

    applyDownloadTargets();

    const toggle = $('#hosted-promo-other');
    const panel = $('#hosted-promo-panel');
    // Guard against duplicate listeners: initPromoStrip runs again after
    // capabilities resolve.
    if (toggle && panel && !toggle.dataset.wired) {
      toggle.dataset.wired = '1';
      toggle.addEventListener('click', () => {
        const willOpen = panel.hidden;
        panel.hidden = !willOpen;
        toggle.setAttribute('aria-expanded', String(willOpen));
        toggle.textContent = willOpen ? 'Hide Platforms' : 'Other Platforms';
      });
    }
  }

  // --- Quality Gating (§1.2 Decision #10) ---
  function isResolutionLocked(resolution) {
    if (!isHostedMode()) return false;
    return lockedResolutions().has(resolution);
  }

  function highestAllowedResolution(qualities) {
    const allowed = qualities
      .map((q) => q.resolution)
      .filter((r) => !isResolutionLocked(r));
    if (allowed.length) {
      // Qualities arrive highest-first, so the first allowed entry is the cap.
      return allowed[0];
    }
    return null;
  }

  function applyQualityGating(qualities) {
    const grid = $('#video-qualities-grid');
    if (!grid) return;

    const locked = lockedResolutions();
    const chips = grid.querySelectorAll('.chip');

    chips.forEach((chip) => {
      const res = chip.dataset.resolution;
      const isLocked = isHostedMode() && locked.has(res);

      chip.classList.toggle('locked', isLocked);
      chip.setAttribute('aria-disabled', String(isLocked));
      if (isLocked) chip.setAttribute('title', 'Available in the Desktop or Android app');

      let tag = chip.querySelector('.locked-tag');
      if (isLocked && !tag) {
        tag = document.createElement('span');
        tag.className = 'tag locked-tag';
        tag.textContent = 'Desktop/Android only';
        chip.appendChild(tag);
      } else if (!isLocked && tag) {
        tag.remove();
      }
    });

    // Auto-correct a selection that is no longer permitted, so the user never
    // submits a request the server will reject.
    if (isResolutionLocked(AppState.selectedQuality)) {
      const fallback = highestAllowedResolution(qualities);
      if (fallback) {
        AppState.selectedQuality = fallback;
        chips.forEach((c) => c.classList.toggle('selected', c.dataset.resolution === fallback));
        updateActionButtonLabel();
      }
    }
  }

  // --- Clip-Aware Trim Guard (§1.2 Decision #5) ---
  function currentClipDuration() {
    const { active, start, end, total } = AppState.trim;
    const isWhole = start === 0 && end >= total;
    return active && !isWhole ? Math.max(0, end - start) : (total || 0);
  }

  function updateTrimLimitBanner() {
    const banner = $('#trim-limit-banner');
    const text = $('#trim-limit-text');
    if (!banner || !text) return;

    if (!isHostedMode() || AppState.mode === 'srt') {
      banner.hidden = true;
      banner.classList.remove('over');
      updateActionButtonLabel();
      return;
    }

    const limit = maxDurationSeconds();
    const duration = currentClipDuration();

    if (duration <= limit) {
      banner.hidden = true;
      banner.classList.remove('over');
      updateActionButtonLabel();
      return;
    }

    banner.hidden = false;
    banner.classList.add('over');

    const sourceTotal = AppState.trim.total || duration;
    const whole = sourceTotal > limit;
    const limitLabel = `${Math.round(limit / 60)} minutes`;

    if (whole) {
      text.innerHTML =
        `<b>Hosted limit: ${limitLabel}.</b> This video is ${Math.round(sourceTotal / 60)} minutes long. ` +
        `Trim a segment under ${limitLabel} below, or download the Desktop or Android app for full videos.`;
    } else {
      text.innerHTML =
        `<b>Selected trim is too long.</b> The chosen segment is ${Math.round(duration / 60)} minutes; ` +
        `the hosted limit is ${limitLabel}. Shorten the range, or download the Desktop or Android app.`;
    }

    updateActionButtonLabel();
  }

  // --- API Client: Download & SSE Pipeline ---
  async function startDownload() {
    const targetUrl = AppState.currentUrl || (AppState.metadata && AppState.metadata.url) || '';
    if (!targetUrl) {
      showError('No media URL specified. Please go back and enter a valid URL.');
      return;
    }

    const embedSubsEl = $('#check-embed-subs');
    const isTrimmed = AppState.trim.active && (AppState.trim.start > 0 || AppState.trim.end < AppState.trim.total);

    const payload = {
      url: targetUrl,
      mode: AppState.mode || 'mp4',
      quality: AppState.selectedQuality || '1080p',
      bitrate: AppState.selectedBitrate || 192,
      trim: isTrimmed ? {
        start: Math.round(AppState.trim.start),
        end: Math.round(AppState.trim.end)
      } : null,
      subtitles: AppState.subtitles.lang ? {
        lang: AppState.subtitles.lang,
        embed: embedSubsEl ? embedSubsEl.checked : true
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
        // 429 is a designed, recoverable state carrying a gate-specific upgrade
        // prompt. Surfacing it verbatim preserves the conversion moment instead
        // of flattening it into a generic failure (§1.2 Decision #11).
        if (resp.status === 429) {
          showRateLimited(detailPayload(err).message || 'Too many concurrent downloads. Please wait and try again.');
          return;
        }
        if (resp.status === 451) {
          showYouTubeUpsell(detailPayload(err));
          return;
        }
        // Carry the server's structured code onto the Error so the diagnosis
        // panel can show something true instead of a generic placeholder.
        const detail = detailPayload(err);
        const rejected = new Error(detail.message || 'Download request rejected.');
        rejected.code = detail.code || '';
        throw rejected;
      }

      const data = await resp.json();
      AppState.activeJobId = data.job_id;
      connectSSE(data.job_id);
    } catch (err) {
      showError(err.message, err.code);
    }
  }

  function resetWorkingScreen() {
    try {
      const livePct = $('#live-pct');
      if (livePct) livePct.innerHTML = '0<small>%</small>';
      const liveBar = $('#live-bar-fill');
      if (liveBar) liveBar.style.width = '0%';
      const liveClipper = $('#live-clipper');
      if (liveClipper) liveClipper.style.left = '0%';
      const liveSpeed = $('#live-speed');
      if (liveSpeed) liveSpeed.textContent = '-- MB/s';
      const liveEta = $('#live-eta');
      if (liveEta) liveEta.textContent = '--:--';
      const liveGot = $('#live-got');
      if (liveGot) liveGot.textContent = '-- / --';

      const track = $('.track');
      if (track) track.classList.add('indeterminate');

      $$('#live-steps-list li').forEach((li) => {
        li.className = 'todo';
        li.style.display = '';
        const st = li.querySelector('.st');
        if (st) st.innerHTML = '';
      });
      setStepState('step-inspect', 'done');
      setStepState('step-download', 'now');

      // Only display subtitles step if subtitles were configured
      const stepSubs = $('#step-subtitles');
      if (stepSubs) {
        stepSubs.style.display = AppState.subtitles && AppState.subtitles.lang ? 'flex' : 'none';
      }

      const workingFmt = $('#working-spec-format');
      if (workingFmt) {
        workingFmt.textContent = AppState.mode === 'mp4'
          ? `MP4 · ${AppState.selectedQuality || '1080p'}`
          : `MP3 · ${AppState.selectedBitrate || 192} kbps`;
      }

      const workingClip = $('#working-spec-clip');
      if (workingClip) {
        const isWhole = !AppState.trim.active || (AppState.trim.start === 0 && AppState.trim.end >= AppState.trim.total);
        workingClip.textContent = isWhole
          ? 'Whole video'
          : `${formatSeconds(AppState.trim.start)} - ${formatSeconds(AppState.trim.end)} (${formatSeconds(AppState.trim.end - AppState.trim.start)})`;
      }

      if (AppState.metadata) {
        const wTitle = $('#working-title');
        if (wTitle) wTitle.textContent = AppState.metadata.title || 'Media';
        const wDur = $('#working-duration');
        if (wDur) wDur.textContent = formatSeconds(AppState.metadata.duration);
        if (AppState.metadata.thumbnail) {
          const wImg = $('#working-thumb-img');
          const wPh = $('#working-thumb-ph');
          if (wImg) {
            wImg.src = AppState.metadata.thumbnail;
            wImg.style.display = 'block';
          }
          if (wPh) wPh.style.display = 'none';
        }
      }
    } catch (err) {
      console.warn('resetWorkingScreen non-fatal error:', err);
    }
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
    const rawPct = typeof data.progress === 'number' ? data.progress : 0;
    const pct = Math.min(100, Math.max(0, Math.round(rawPct)));

    const track = $('.track');
    const livePct = $('#live-pct');
    const liveBar = $('#live-bar-fill');
    const liveClipper = $('#live-clipper');

    if (pct > 0) {
      if (track) track.classList.remove('indeterminate');
      if (livePct) livePct.innerHTML = `${pct}<small>%</small>`;
      if (liveBar) liveBar.style.width = `${pct}%`;
      if (liveClipper) liveClipper.style.left = `${pct}%`;
    } else {
      if (track) track.classList.add('indeterminate');
      if (livePct) livePct.innerHTML = '0<small>%</small>';
      if (liveBar) liveBar.style.width = '0%';
      if (liveClipper) liveClipper.style.left = '0%';
    }

    const liveSpeed = $('#live-speed');
    const liveEta = $('#live-eta');
    const liveGot = $('#live-got');
    if (data.speed && liveSpeed) liveSpeed.textContent = data.speed;
    if (data.eta && liveEta) liveEta.textContent = data.eta;
    if (data.got && liveGot) liveGot.textContent = data.got;

    // Advance Stage steps based on server state string
    const stage = (data.stage || '').toLowerCase();
    if (data.stage) {
      const stepDl = $('#step-download');
      if (stepDl && (stage.includes('trim') || stage.includes('download') || stage.includes('stream') || stage.includes('segment') || stage.includes('preparing') || stage.includes('connecting'))) {
        stepDl.className = 'now';
        const stSpan = stepDl.querySelector('.st');
        const stHtml = stSpan ? stSpan.outerHTML : '<span class="st"></span>';
        stepDl.innerHTML = `${stHtml}${data.stage}`;
      }
    }
    if (stage.includes('process') || stage.includes('remux') || stage.includes('join') || stage.includes('convert') || stage.includes('finalizing')) {
      setStepState('step-download', 'done');
      setStepState('step-process', 'now');
    } else if (stage.includes('caption') || stage.includes('tag') || stage.includes('art') || stage.includes('metadata')) {
      setStepState('step-download', 'done');
      setStepState('step-process', 'done');
      setStepState('step-subtitles', 'now');
    }
  }

  function handleComplete(data) {
    const track = $('.track');
    if (track) track.classList.remove('indeterminate');
    const livePct = $('#live-pct');
    if (livePct) livePct.innerHTML = '100<small>%</small>';
    const liveBar = $('#live-bar-fill');
    if (liveBar) liveBar.style.width = '100%';
    const liveClipper = $('#live-clipper');
    if (liveClipper) liveClipper.style.left = '100%';

    setStepState('step-download', 'done');
    setStepState('step-process', 'done');
    if (AppState.subtitles && AppState.subtitles.lang) {
      setStepState('step-subtitles', 'done');
    }
    setStepState('step-ready', 'done');

    // Populate Ready Screen
    const readyFn = $('#ready-filename');
    if (readyFn) readyFn.textContent = data.filename || 'media-download';

    const readySz = $('#ready-spec-size');
    if (readySz) readySz.textContent = data.size || '-- MB';

    const clipDur = (AppState.trim.end - AppState.trim.start) > 0
      ? (AppState.trim.end - AppState.trim.start)
      : (AppState.metadata ? AppState.metadata.duration : 0);

    const readyDur = $('#ready-spec-duration');
    if (readyDur) readyDur.textContent = formatSeconds(clipDur);

    const readyClip = $('#ready-spec-clip');
    if (readyClip) readyClip.textContent = formatSeconds(clipDur);

    const modeLbl = $('#ready-recap-mode-label');
    if (modeLbl) modeLbl.textContent = AppState.mode === 'mp4' ? 'Quality' : 'Bitrate';

    const readyFmt = $('#ready-spec-format');
    if (readyFmt) {
      readyFmt.textContent = AppState.mode === 'mp4'
        ? (AppState.selectedQuality || '1080p')
        : `${AppState.selectedBitrate || 192} kbps`;
    }

    const readyCaps = $('#ready-spec-captions');
    if (readyCaps) {
      if (AppState.subtitles && AppState.subtitles.lang) {
        const subsName = $('#subs-track-name')?.textContent || AppState.subtitles.lang;
        readyCaps.textContent = `${subsName}, embedded`;
      } else {
        readyCaps.textContent = 'None';
      }
    }

    const readyIcon = $('#ready-file-icon');
    if (readyIcon) {
      readyIcon.innerHTML = AppState.mode === 'mp3'
        ? '<use href="#i-audio"/>'
        : '<use href="#i-video"/>';
    }

    if (AppState.metadata && AppState.metadata.thumbnail) {
      const rImg = $('#ready-thumb-img');
      const rPh = $('#ready-thumb-ph');
      if (rImg) {
        rImg.src = AppState.metadata.thumbnail;
        rImg.style.display = 'block';
      }
      if (rPh) rPh.style.display = 'none';
    }

    const saveMediaBtn = $('#btn-save-media');
    if (saveMediaBtn) {
      saveMediaBtn.href = data.download_url;
      saveMediaBtn.setAttribute('download', data.filename || 'media');

      if (isHostedMode()) {
        // Hosted serves the file over HTTP; navigating here is what triggers
        // the browser save and starts the server-side purge clock (§1.2 #8).
        saveMediaBtn.addEventListener('click', () => {
          startDeletionCountdown(AppState.retentionSeconds);
        }, { once: true });
      } else {
        // Desktop already wrote the file to ~/Downloads server-side. Navigating
        // here would save a second copy, so the button becomes "show me where"
        // rather than a download trigger (§1.3 Decision #14).
        saveMediaBtn.href = '#';
        saveMediaBtn.removeAttribute('download');
        saveMediaBtn.setAttribute('role', 'button');
        saveMediaBtn.addEventListener('click', async (e) => {
          e.preventDefault();
          try {
            const resp = await fetch(`${API_BASE}/open-folder`, { method: 'POST' });
            if (!resp.ok) throw new Error('nope');
            const label = $('#btn-save-label');
            if (label) label.textContent = 'Opened folder';
          } catch (err) {
            const label = $('#btn-save-label');
            if (label) label.textContent = 'Saved to Downloads';
          }
        }, { once: true });
      }
    }
    const saveLbl = $('#btn-save-label');
    if (saveLbl) {
      // Desktop has already saved the file by the time the ready screen shows.
      saveLbl.textContent = isHostedMode()
        ? `Save ${AppState.mode.toUpperCase()}`
        : 'Open in Downloads';
    }

    // Subtitle card if generated
    const srtCard = $('#ready-srt-card');
    const subsBtn = $('#btn-save-subtitles');
    if (data.srt_url) {
      if (srtCard) srtCard.hidden = false;
      const srtFn = $('#ready-srt-filename');
      if (srtFn) srtFn.textContent = data.srt_filename || 'subtitles.srt';
      const srtSz = $('#ready-srt-size');
      if (srtSz) srtSz.textContent = data.srt_size || '2.4 KB';
      if (subsBtn) {
        subsBtn.hidden = false;
        subsBtn.onclick = () => {
          // The SRT is a separate server object. Desktop already wrote it to
          // disk, so navigating would create a duplicate copy (§1.3 #14).
          if (isHostedMode()) {
            startDeletionCountdown(AppState.retentionSeconds);
            window.location.href = data.srt_url;
          } else {
            fetch(`${API_BASE}/open-folder`, { method: 'POST' }).catch(() => {});
          }
        };
      }
    } else {
      if (srtCard) srtCard.hidden = true;
      if (subsBtn) subsBtn.hidden = true;
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

    // Ephemeral notice: static until the user saves, then a live countdown
    // mirroring the server's purge-on-retrieval window (§1.2 Decision #8).
    renderRetentionNotice();
    stopDeletionCountdown();

    setTimeout(() => {
      showScreen('ready');
    }, 280);
  }

  function stopDeletionCountdown() {
    if (AppState.deletionTimer) {
      clearInterval(AppState.deletionTimer);
      AppState.deletionTimer = null;
    }
  }

  // Render one of the notice's three states. `title` is bold and short; `text`
  // explains the consequence. `timerHTML` carries the ticking clock.
  function setRetentionState(state, title, text, timerHTML) {
    const notice = $('#ready-retention-notice');
    const titleEl = $('#ready-retention-title');
    const textEl = $('#ready-retention-text');
    if (!notice) return;

    notice.hidden = false;
    notice.classList.remove('is-idle', 'is-counting', 'is-done');
    notice.classList.add(`is-${state}`);
    if (titleEl) titleEl.innerHTML = title;
    if (textEl) textEl.textContent = text;
    if (timerHTML && titleEl) titleEl.innerHTML = title.replace('%timer%', timerHTML);
  }

  function renderRetentionNotice() {
    // A new job supersedes any countdown still running from a previous one.
    if (isHostedMode()) {
      setRetentionState(
        'idle',
        'Ephemeral by design',
        `This file is deleted from the server ${retentionPhrase(AppState.retentionSeconds)} after you save. Nothing is kept here.`,
        ''
      );
    } else {
      // Desktop writes to the user's own machine; there is nothing to count down.
      setRetentionState(
        'idle',
        'Saved to your Downloads folder',
        'This file stays on your machine. Nothing is uploaded or kept on a server.',
        ''
      );
    }
  }

  // Called only when the user actually triggers the save, which is the moment
  // the server schedules the purge.
  function startDeletionCountdown(seconds) {
    stopDeletionCountdown();

    const notice = $('#ready-retention-notice');
    if (!notice) return;

    let left = Math.max(1, Math.round(seconds));

    setRetentionState(
      'counting',
      'Saved to your device%timer%',
      "The server's copy is deleted when the timer ends. Your downloaded file is not affected.",
      `<span class="save-notice-timer" id="ready-deletion-timer">${formatTimeMMSS(left)}</span>`
    );

    AppState.deletionTimer = setInterval(() => {
      left--;
      const timer = $('#ready-deletion-timer');
      if (left <= 0) {
        stopDeletionCountdown();
        setRetentionState(
          'done',
          'Deleted from our server',
          "The server's copy is gone. The file you saved is still on your device.",
          ''
        );
        return;
      }
      if (timer) timer.textContent = formatTimeMMSS(left);
    }, 1000);
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

  function showRateLimited(detail) {
    if (AppState.eventSource) AppState.eventSource.close();
    if (AppState.pollTimer) clearInterval(AppState.pollTimer);

    // Return the user to the configure screen rather than a dead-end error:
    // their settings are intact and retrying costs one click.
    showScreen('choose');

    const banner = $('#trim-limit-banner');
    if (banner && detail) {
      const text = $('#trim-limit-text');
      if (text) text.textContent = detail;
      banner.hidden = false;
      banner.classList.remove('over');
    }

    const hint = $('#error-upgrade-hint');
    if (hint && detail) {
      const hintText = $('#error-upgrade-text');
      if (hintText) hintText.textContent = detail;
      hint.hidden = false;
      const cta = $('#error-upgrade-cta');
      if (cta) {
        const downloads = (AppState.capabilities && AppState.capabilities.downloads) || {};
        const target = PLATFORM_TARGETS.find((t) => t.key === detectPlatform()) || PLATFORM_TARGETS[0];
        const url = downloads[target.id];
        if (url) {
          cta.href = url;
          cta.removeAttribute('aria-disabled');
          cta.hidden = false;
        } else {
          cta.hidden = true;
        }
      }
    }
  }

    // YouTube upsell card (§1.5 Decision #18).
    // Distinct from showError: the link is valid and the server is fine, so it
    // gets guidance copy and an app CTA rather than a red error line.
    function showYouTubeUpsell(payload) {
      if (AppState.eventSource) AppState.eventSource.close();
      if (AppState.pollTimer) clearInterval(AppState.pollTimer);

      const message = (payload && payload.message) || '';

      // The landing screen has no #view-choose; switching screens there blanks
      // the page, so only navigate when a configure screen actually exists.
      if ($('#view-choose')) showScreen('choose');

      // The card carries the full reason. Repeating the same sentence in the
      // red inline error line underneath just restates it in a worse style, so
      // clear any stale error and let the card speak for itself.
      const inline = $('#inspect-error');
      if (inline) { inline.hidden = true; inline.textContent = ''; }

      const card = $('#yt-upsell');
      if (!card) return;
      card.hidden = false;

      if (message) {
        const text = $('#yt-upsell-text');
        if (text) text.textContent = message;
      }

      // Prefer a real binary; fall back to the page so the CTA always works.
      const downloads = (AppState.capabilities && AppState.capabilities.downloads) || {};
      const target = PLATFORM_TARGETS.find((t) => t.key === detectPlatform()) || PLATFORM_TARGETS[0];
      const cta = $('#yt-upsell-cta');
      const ctaLabel = $('#yt-upsell-cta-label');
      if (cta) {
        const url = downloads[target.id];
        cta.href = isRealDownload(url) ? url : '/downloads';
        cta.removeAttribute('aria-disabled');
        cta.hidden = false;
      }
      if (ctaLabel) {
        const copy = {
          windows: 'Get the Windows app',
          linux: 'Get the Linux app',
          android: 'Get the Android app'
        }[target.key] || 'Get the app';
        ctaLabel.textContent = copy;
      }

      const dismiss = $('#yt-upsell-dismiss');
      if (dismiss && !dismiss.dataset.wired) {
        dismiss.dataset.wired = '1';
        dismiss.addEventListener('click', () => { card.hidden = true; }, { once: true });
      }
    }

    function hideYouTubeUpsell() {
      const card = $('#yt-upsell');
      if (card) card.hidden = true;
    }

    // Read the structured detail the API returns, tolerating a plain string.
    function detailPayload(body) {
      if (body && typeof body === 'object' && body.detail !== undefined) {
        return typeof body.detail === 'string' ? { message: body.detail } : body.detail;
      }
      return { message: typeof body === 'string' ? body : '' };
    }

    function showError(msg, code) {
    if (AppState.eventSource) AppState.eventSource.close();
    if (AppState.pollTimer) clearInterval(AppState.pollTimer);
    const descEl = $('#error-desc-msg');
    if (descEl) descEl.textContent = msg || 'Could not process media stream.';
    // Diagnosis code is opt-in: a placeholder here previously read as
    // "MEDIA_INACCESSIBLE" for every failure, telling users their video was
    // private when the real cause was a server-side error. Absent a real code,
    // show no code at all rather than a wrong one.
    const codeEl = $('#error-diag-code');
    if (codeEl) {
      const value = (code || '').trim();
      codeEl.textContent = value;
      codeEl.hidden = !value;
    }
    const diagEl = $('#error-diag-detail') || $('#error-details-text');
    if (diagEl) diagEl.textContent = msg ? `Details: ${msg}` : 'No further detail was reported.';
    const retryInp = $('#error-url-retry');
    if (retryInp) retryInp.value = AppState.currentUrl || '';
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
      if (!resp.ok) {
        if (resp.status === 429) {
          // Same designed state as the main path; do not collapse it into
          // "Subtitles unavailable." (§1.2 Decision #11).
          btn.disabled = false;
          btn.textContent = 'Save .srt';
          showRateLimited(detailPayload(await resp.json().catch(() => ({}))).message || 'Too many concurrent downloads.');
          return;
        }
        throw new Error('Subtitles unavailable.');
      }
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
    const btnTheme = $('#btn-theme-flip');
    if (btnTheme) btnTheme.addEventListener('click', toggleTheme);

    // History Drawer
    const btnOpenHist = $('#btn-open-history');
    if (btnOpenHist) btnOpenHist.addEventListener('click', openHistoryDrawer);
    const btnCloseHist = $('#btn-close-history');
    if (btnCloseHist) btnCloseHist.addEventListener('click', closeHistoryDrawer);
    const histBackdrop = $('#history-backdrop');
    if (histBackdrop) histBackdrop.addEventListener('click', closeHistoryDrawer);
    const btnClearHist = $('#btn-clear-history');
    if (btnClearHist) btnClearHist.addEventListener('click', clearHistory);

    // Form Inspect (Home page)
    const formInspect = $('#form-inspect');
    if (formInspect) {
      formInspect.addEventListener('submit', (e) => {
        e.preventDefault();
        const url = $('#input-url')?.value.trim();
        if (url) inspectUrl(url);
      });
    }

    // Format Switching (Choose page)
    const btnFmtVid = $('#btn-fmt-video');
    if (btnFmtVid) {
      btnFmtVid.addEventListener('click', () => {
        AppState.mode = 'mp4';
        btnFmtVid.classList.add('active');
        $('#btn-fmt-audio')?.classList.remove('active');
        const pVid = $('#pane-video'); if (pVid) pVid.hidden = false;
        const pAud = $('#pane-audio'); if (pAud) pAud.hidden = true;
        updateActionButtonLabel();
      });
    }

    const btnFmtAud = $('#btn-fmt-audio');
    if (btnFmtAud) {
      btnFmtAud.addEventListener('click', () => {
        AppState.mode = 'mp3';
        btnFmtAud.classList.add('active');
        $('#btn-fmt-video')?.classList.remove('active');
        const pVid = $('#pane-video'); if (pVid) pVid.hidden = true;
        const pAud = $('#pane-audio'); if (pAud) pAud.hidden = false;
        updateActionButtonLabel();
      });
    }

    // Audio Bitrate Selection
    $$('#audio-rows-grid .audio-row').forEach((row) => {
      row.addEventListener('click', () => {
        $$('#audio-rows-grid .audio-row').forEach((r) => r.classList.remove('selected'));
        row.classList.add('selected');
        AppState.selectedBitrate = parseInt(row.dataset.bitrate, 10);
        updateActionButtonLabel();
      });
    });

    // Subtitle Instant Download
    const btnSrt = $('#btn-save-srt-instant');
    if (btnSrt) {
      btnSrt.addEventListener('click', downloadInstantSrt);
    }

    // Trim Sliders
    const rs = $('#trim-s');
    const re = $('#trim-e');
    if (rs && re) {
      const updateZIndex = (active) => {
        if (active === 'start') {
          rs.style.zIndex = '5';
          re.style.zIndex = '4';
        } else if (active === 'end') {
          re.style.zIndex = '5';
          rs.style.zIndex = '4';
        }
      };

      rs.addEventListener('input', () => {
        let v = parseInt(rs.value, 10);
        if (isNaN(v)) v = 0;
        if (v >= AppState.trim.end) {
          v = Math.max(0, AppState.trim.end - 1);
          rs.value = v;
        }
        AppState.trim.start = Math.max(0, v);
        AppState.trim.active = (AppState.trim.start > 0 || AppState.trim.end < AppState.trim.total);
        updateZIndex('start');
        const errEl = $('#trim-error');
        if (errEl) errEl.hidden = true;
        updateTrimUI('start');
      });

      re.addEventListener('input', () => {
        let v = parseInt(re.value, 10);
        if (isNaN(v)) v = AppState.trim.total;
        if (v <= AppState.trim.start) {
          v = Math.min(AppState.trim.total, AppState.trim.start + 1);
          re.value = v;
        }
        AppState.trim.end = Math.min(AppState.trim.total, v);
        AppState.trim.active = (AppState.trim.start > 0 || AppState.trim.end < AppState.trim.total);
        updateZIndex('end');
        const errEl = $('#trim-error');
        if (errEl) errEl.hidden = true;
        updateTrimUI('end');
      });

      rs.addEventListener('pointerdown', () => updateZIndex('start'));
      re.addEventListener('pointerdown', () => updateZIndex('end'));
      rs.addEventListener('focus', () => updateZIndex('start'));
      re.addEventListener('focus', () => updateZIndex('end'));
    }

    // Waveform seeking & scrubbing
    const waveEl = $('.wave');
    if (waveEl) {
      let activeDragHandle = null;

      const seekFromPointer = (clientX) => {
        const rect = waveEl.getBoundingClientRect();
        if (rect.width <= 0) return;
        const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
        const total = AppState.trim.total || 1;
        const clickedSec = Math.round(ratio * total);

        if (!activeDragHandle) {
          const distToStart = Math.abs(clickedSec - AppState.trim.start);
          const distToEnd = Math.abs(clickedSec - AppState.trim.end);
          activeDragHandle = (distToStart < distToEnd) ? 'start' : 'end';
        }

        if (activeDragHandle === 'start') {
          AppState.trim.start = Math.min(clickedSec, Math.max(0, AppState.trim.end - 1));
          if (rs) {
            rs.value = AppState.trim.start;
            rs.style.zIndex = '5';
            if (re) re.style.zIndex = '4';
          }
        } else {
          AppState.trim.end = Math.max(clickedSec, Math.min(total, AppState.trim.start + 1));
          if (re) {
            re.value = AppState.trim.end;
            re.style.zIndex = '5';
            if (rs) rs.style.zIndex = '4';
          }
        }

        AppState.trim.active = (AppState.trim.start > 0 || AppState.trim.end < AppState.trim.total);
        const errEl = $('#trim-error');
        if (errEl) errEl.hidden = true;
        updateTrimUI();
      };

      const onPointerMove = (e) => {
        if (!activeDragHandle) return;
        seekFromPointer(e.clientX);
      };

      const onPointerUp = () => {
        activeDragHandle = null;
        window.removeEventListener('pointermove', onPointerMove);
        window.removeEventListener('pointerup', onPointerUp);
        window.removeEventListener('pointercancel', onPointerUp);
      };

      waveEl.addEventListener('pointerdown', (e) => {
        if (e.target === rs || e.target === re) return;
        activeDragHandle = null;
        seekFromPointer(e.clientX);
        window.addEventListener('pointermove', onPointerMove);
        window.addEventListener('pointerup', onPointerUp);
        window.addEventListener('pointercancel', onPointerUp);
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
      ts.addEventListener('blur', handleInputCommit);
      ts.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); ts.blur(); } });
    }
    if (te) {
      te.addEventListener('change', handleInputCommit);
      te.addEventListener('blur', handleInputCommit);
      te.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); te.blur(); } });
    }

    // Start Download Button
    const btnStart = $('#btn-start-download');
    if (btnStart) btnStart.addEventListener('click', startDownload);

    const btnChooseBack = $('#btn-choose-back');
    if (btnChooseBack) {
      btnChooseBack.addEventListener('click', () => {
        window.location.href = '/';
      });
    }

    // Cancel Download Button
    const btnCancel = $('#btn-cancel-download');
    if (btnCancel) {
      btnCancel.addEventListener('click', () => {
        if (AppState.eventSource) AppState.eventSource.close();
        if (AppState.pollTimer) clearInterval(AppState.pollTimer);
        showScreen('choose');
      });
    }

    // Grab Another Link
    const btnGrabAnother = $('#btn-grab-another');
    if (btnGrabAnother) {
      btnGrabAnother.addEventListener('click', () => {
        try {
          sessionStorage.removeItem('mg_active_meta');
          sessionStorage.removeItem('mg_active_url');
        } catch (e) {}
        window.location.href = '/';
      });
    }

    const btnErrorRetry = $('#btn-error-retry');
    if (btnErrorRetry) {
      btnErrorRetry.addEventListener('click', () => {
        window.location.href = '/';
      });
    }

    const btnOfflineRetry = $('#btn-offline-retry');
    if (btnOfflineRetry) {
      btnOfflineRetry.addEventListener('click', () => {
        if (navigator.onLine) {
          if ($('#view-choose')) showScreen('choose');
          else if ($('#view-paste')) showScreen('paste');
        } else {
          alert('Still offline. Please check your network connection.');
        }
      });
    }

    // Brand Home link
    const brandHome = $('#brand-home');
    if (brandHome) {
      brandHome.addEventListener('click', (e) => {
        if (!$('#view-paste')) {
          // If on choose page, allow default link action to navigate home
          return;
        }
        e.preventDefault();
        showScreen('paste');
      });
    }

    // Connectivity Status
    window.addEventListener('online', () => {
      if (AppState.screen === 'offline') {
        if ($('#view-choose')) showScreen('choose');
        else if ($('#view-paste')) showScreen('paste');
      }
    });
    window.addEventListener('offline', () => {
      showScreen('offline');
    });
  }

  // --- Choose Page Initializer ---
  function initChoosePage() {
    let meta = null;
    let url = '';
    try {
      const raw = sessionStorage.getItem('mg_active_meta');
      if (raw) meta = JSON.parse(raw);
      url = sessionStorage.getItem('mg_active_url') || '';
    } catch (e) {}

    const urlParams = new URLSearchParams(window.location.search);
    const queryUrl = urlParams.get('url');

    if (meta) {
      AppState.metadata = meta;
      AppState.currentUrl = url || meta.url || '';
      populateChooser(meta);
      showScreen('choose');
    } else if (queryUrl) {
      inspectUrl(queryUrl);
    } else {
      window.location.href = '/';
    }
  }

  // --- PWA Service Worker & Install Prompt ---
  function initPWA() {
    // Hosted mode deliberately suppresses install prompts: the site's value
    // proposition is the native apps, and a PWA prompt competes with them.
    // iOS gets no prompt regardless of mode (§1.2 Decision #8).
    const isIos = /iPhone|iPad|iPod/i.test(navigator.userAgent || '');
    const pwaAllowed = !isHostedMode() && !isIos;

    if (pwaAllowed && 'serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch((err) => {
        console.warn('SW registration failed:', err);
      });
    }

    let deferredPrompt = null;
    window.addEventListener('beforeinstallprompt', (e) => {
      // Always cancel; on hosted/iOS we never intend to show it.
      e.preventDefault();
      if (!pwaAllowed) return;
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
    initPromoStrip();
    renderRetentionCopy();

    if ($('#view-choose')) {
      initChoosePage();
    }

    if (!navigator.onLine) {
      showScreen('offline');
    }

    // Capabilities are advisory: they refine limits and download URLs but must
    // never gate rendering or block the Grab button. Load after first paint so
    // the page is usable immediately and re-apply gating if anything changes.
    loadCapabilities().then((caps) => {
      if (!caps) return;
      initPromoStrip();
      applyDownloadTargets();
      if ($('#view-choose') && AppState.metadata) {
        applyQualityGating(AppState.metadata.qualities || []);
        updateTrimLimitBanner();
      }
      updateActionButtonLabel();
    });
  });

})();
