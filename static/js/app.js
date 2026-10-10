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

      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        throw new Error(errData.detail || 'Failed to inspect link.');
      }

      const data = await resp.json();
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
    }
    const saveLbl = $('#btn-save-label');
    if (saveLbl) saveLbl.textContent = `Save ${AppState.mode.toUpperCase()}`;

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
        subsBtn.onclick = () => { window.location.href = data.srt_url; };
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

    // Start 30-min deletion countdown
    startDeletionCountdown(30 * 60);

    setTimeout(() => {
      showScreen('ready');
    }, 280);
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
    const descEl = $('#error-desc-msg');
    if (descEl) descEl.textContent = msg || 'Could not process media stream.';
    const diagEl = $('#error-diag-detail') || $('#error-details-text');
    if (diagEl) diagEl.textContent = `Details: ${msg || 'Unknown error'}`;
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

    if ($('#view-choose')) {
      initChoosePage();
    }

    if (!navigator.onLine) {
      showScreen('offline');
    }
  });

})();
