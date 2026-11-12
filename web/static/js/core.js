const API = '';

// ── Navigation ───────────────────────────────────────────────────────────────
function showPanel(name) {
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('panel-' + name).classList.add('active');
  document.querySelectorAll('.nav-item').forEach(n => {
    if (n.getAttribute('onclick') === `showPanel('${name}')`) n.classList.add('active');
  });
  const titles = {
    watchlist:      'Watchlist',
    recordings:     'TK Recordings',
    downloader:     'Downloader',
    downloads:      'Downloads',
    logs:           'System Log',
  };
  document.getElementById('page-title').textContent = titles[name] || name;

  if (name === 'recordings')      loadRecordings();
  if (name === 'downloads')       loadDownloadsList();
  if (name === 'logs')            loadLogs();
}

function refreshAll() {
  loadWatchlist();
  loadStats();
}

// ── API helpers ───────────────────────────────────────────────────────────────
async function apiFetch(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json', ...opts.headers },
    ...opts,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

// ── Stats ─────────────────────────────────────────────────────────────────────
async function loadStats() {
  try {
    const s = await apiFetch('/api/stats');
    document.getElementById('stat-total').textContent     = s.total_users;
    document.getElementById('stat-recording').textContent = s.currently_recording;
    document.getElementById('stat-clips').textContent     = s.total_recordings;
    document.getElementById('stat-disk').textContent      = s.disk_used_mb + ' MB';
  } catch(e) { /* silent */ }
}

// ── Logs ──────────────────────────────────────────────────────────────────────
function _renderLogLines(lines) {
  return lines.map(line => {
    const cls = line.includes('ERROR') || line.includes('[!]') ? 'error'
              : line.includes('WARNING') || line.includes('WARN') ? 'warn' : 'info';
    return `<span class="log-line ${cls}">${escHtml(line)}</span>`;
  }).join('\n');
}

async function loadLogs() {
  const container = document.getElementById('log-lines');
  try {
    const data = await apiFetch('/api/logs?lines=200');
    container.innerHTML = _renderLogLines(data.lines);
    if (document.getElementById('log-autoscroll').checked) {
      container.scrollTop = container.scrollHeight;
    }
  } catch(e) {
    container.innerHTML = `<span class="log-line error">Failed to load logs: ${e.message}</span>`;
  }
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, type = 'info') {
  const icons = { success: '✓', error: '✕', info: '◎' };
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `<span>${icons[type]}</span><span>${msg}</span>`;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

// ── View mode (list/grid) per section ────────────────────────────────────────
// { sectionKey: 'list' | 'thumb' }
const _sectionViewMode = {};

function toggleViewMode(sectionKey, reloadFn) {
  _sectionViewMode[sectionKey] = _sectionViewMode[sectionKey] === 'thumb' ? 'list' : 'thumb';
  window[reloadFn]();
}

function _viewToggleBtnHtml(sectionKey, reloadFn) {
  const isThumb = _sectionViewMode[sectionKey] === 'thumb';
  return `<button class="rec-btn" style="padding:2px 7px;font-size:10px;
    ${isThumb ? 'color:var(--text);border-color:var(--text2);' : 'color:var(--muted);border-color:var(--border);'}"
    onclick="_cycleViewMode('${sectionKey}','${reloadFn}')" title="Toggle view">
    ${isThumb ? '⊞ Grid' : '≡ List'}
  </button>`;
}

function _cycleViewMode(sectionKey, reloadFn) {
  _sectionViewMode[sectionKey] = _sectionViewMode[sectionKey] === 'thumb' ? 'list' : 'thumb';
  window[reloadFn]();
}

function _buildFileCard(f, key, thumbUrl, playType, sectionKey, onSelect, onDownload, onDelete) {
  const isThumb = _sectionViewMode[sectionKey] === 'thumb';
  const isSelected = typeof onSelect === 'string' ? false : false; // selection handled by caller

  if (isThumb) {
    return `<div class="rec-card thumb-card" data-key="${key}" style="padding:0;overflow:hidden;">
      <div class="thumb-img-wrap" onclick="${onSelect}">
        <img class="thumb-img" src="${thumbUrl}"
          onerror="this.parentElement.innerHTML='<div class=thumb-placeholder>▶</div>'"
          loading="lazy" />
      </div>
      <div style="padding:10px 10px 8px;">
        <div class="rec-filename" style="font-size:10px;margin-bottom:6px;">${f.filename}</div>
        <div class="rec-meta" style="margin-bottom:6px;">
          <span>${f.size_mb} MB</span>
          <span>${new Date(f.created_at).toLocaleDateString()}</span>
        </div>
        <div class="rec-actions" onclick="event.stopPropagation()">${onDownload}${onDelete}</div>
      </div>
    </div>`;
  }
  return null; // list mode handled by each module
}

// ── Video Modal (shared) ──────────────────────────────────────────────────────
let _plyrInstance = null;

function openVideoModal(username, filename, type = 'rec') {
  const baseMap = { rec: '/api/recordings', dl: '/api/tiktok/downloads' };
  const base = baseMap[type] || '/api/recordings';
  const src  = `${base}/${encodeURIComponent(username)}/${encodeURIComponent(filename)}?inline=true`;

  const overlay = document.getElementById('vid-modal-overlay');
  const body    = overlay.querySelector('.vid-modal-body');
  document.getElementById('vid-modal-title').textContent = filename;

  // Plyr.destroy() detaches/replaces the <video>, so never reuse it by id:
  // tear down fully and build a fresh element on every open.
  _destroyPlayer();
  body.innerHTML = '<video id="vid-modal-player" playsinline controls></video>';
  const videoEl = body.querySelector('video');

  _plyrInstance = new Plyr(videoEl, {
    controls: ['play-large','play','progress','current-time','mute','volume','fullscreen'],
    keyboard: { global: false },
  });
  _plyrInstance.source = {
    type: 'video',
    sources: [{ src, type: 'video/mp4' }],
  };

  overlay.classList.add('open');
  document.body.style.overflow = 'hidden';
}

function _destroyPlayer() {
  if (_plyrInstance) {
    try { _plyrInstance.pause(); _plyrInstance.destroy(); } catch (e) {}
    _plyrInstance = null;
  }
  const body = document.querySelector('#vid-modal-overlay .vid-modal-body');
  if (body) body.innerHTML = '';  // also stops any buffering
}

function _closeVideo() {
  document.getElementById('vid-modal-overlay').classList.remove('open');
  document.body.style.overflow = '';
  _destroyPlayer();
}

function closeVideoModal(e) {
  if (e && e.target !== document.getElementById('vid-modal-overlay')) return;
  _closeVideo();
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') _closeVideo();
});

// ── Init + auto-refresh ───────────────────────────────────────────────────────
loadWatchlist();
loadStats();
pollLiveStatus();

// refresh watchlist every 15s when on watchlist panel
setInterval(() => {
  const active = document.querySelector('.panel.active');
  if (active && active.id === 'panel-watchlist') {
    loadWatchlist();
    loadStats();
  }
}, 15000);

// poll live status every 60s
setInterval(() => {
  const active = document.querySelector('.panel.active');
  if (active && active.id === 'panel-watchlist') {
    pollLiveStatus();
  }
}, 60000);