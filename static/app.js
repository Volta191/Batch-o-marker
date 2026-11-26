// ========================
// THEME
// ========================
function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const btn = document.getElementById('themeToggle');
  if (btn) {
    const isDark = theme === 'dark';
    btn.setAttribute('aria-pressed', String(isDark));
    btn.textContent = isDark ? '🌙' : '☀️';
    btn.title = isDark ? 'Switch to light (Alt+T)' : 'Switch to dark (Alt+T)';
  }
}

function initTheme() {
  const saved = localStorage.getItem('wm_theme');
  const prefersLight = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
  applyTheme(saved || (prefersLight ? 'light' : 'dark'));

  const toggle = () => {
    const cur = document.documentElement.getAttribute('data-theme');
    const next = (cur === 'dark') ? 'light' : 'dark';
    localStorage.setItem('wm_theme', next);
    applyTheme(next);
  };

  const btn = document.getElementById('themeToggle');
  if (btn) btn.addEventListener('click', toggle);

  if (!window.__wmThemeHotkeyBound) {
    window.__wmThemeHotkeyBound = true;
    document.addEventListener('keydown', (e) => {
      const tag = (e.target?.tagName || '').toLowerCase();
      const isTyping = e.target?.isContentEditable || tag === 'input' || tag === 'textarea' || tag === 'select';
      if (isTyping) return;
      if (e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey && (e.key?.toLowerCase() === 't')) {
        e.preventDefault();
        toggle();
      }
    });
  }
}

// ========================
// TOASTS & LOADING
// ========================
function toast(message, type = 'info', title = '') {
  const host = document.getElementById('toaster');
  if (!host) { alert(message); return; }
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  if (title) {
    const t = document.createElement('div');
    t.className = 'title';
    t.textContent = title;
    el.appendChild(t);
  }
  const m = document.createElement('div');
  m.className = 'msg';
  m.textContent = message;
  el.appendChild(m);

  host.appendChild(el);
  const close = () => { el.classList.add('hide'); setTimeout(() => el.remove(), 180); };
  setTimeout(close, 3800);
  el.addEventListener('click', close);
}

function setLoading(btn, isLoading) {
  if (!btn) return;
  btn.classList.toggle('loading', Boolean(isLoading));
  if (btn.id === 'processLocalBtn') {
    btn.disabled = Boolean(isLoading);
  }
}

// ========================
// STATE
// ========================
let TEMPLATES = {};
const PreviewState = {
  index: 0,
  total: 0,
  objectUrl: null,
  showOriginal: false,
  debounceTimer: null,
};

const CurrentJob = {
  id: null,
  es: null,
  timer: null,
};

function setStopEnabled(enabled) {
  const stopBtn = document.getElementById('stopBtn');
  if (!stopBtn) return;
  stopBtn.disabled = !enabled;
  stopBtn.classList.toggle('is-disabled', !enabled);
}

function resetCurrentJob() {
  if (CurrentJob.timer) { clearInterval(CurrentJob.timer); }
  if (CurrentJob.es) { try { CurrentJob.es.close(); } catch (_) {} }
  CurrentJob.id = null;
  CurrentJob.es = null;
  CurrentJob.timer = null;
  setStopEnabled(false);
  setLoading(document.getElementById('processLocalBtn'), false);
}

// ========================
// HELPERS
// ========================
function formatDuration(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rem = s % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(rem).padStart(2, '0')}`;
  return `${String(m).padStart(2, '0')}:${String(rem).padStart(2, '0')}`;
}

async function fetchTemplates() {
  const res = await fetch('/api/templates');
  TEMPLATES = await res.json();
  return TEMPLATES;
}

function populateTemplatesSelects(tpls, preferName = null) {
  const list = document.getElementById('tplList');
  const forRun = document.getElementById('tplForRun');
  const prevList = preferName || list.value;
  const prevRun = preferName || forRun.value;

  list.innerHTML = '';
  forRun.innerHTML = '';

  const currentOpt = document.createElement('option');
  currentOpt.value = '__editor__';
  currentOpt.textContent = '★ Current editor settings';
  forRun.appendChild(currentOpt);

  const keys = Object.keys(tpls);
  if (keys.length === 0) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '— none saved —';
    list.appendChild(opt);
    forRun.value = '__editor__';
    return;
  }

  keys.forEach((k) => {
    const opt = document.createElement('option');
    opt.value = k;
    opt.textContent = k;
    list.appendChild(opt);
    forRun.appendChild(opt.cloneNode(true));
  });

  if (prevList && keys.includes(prevList)) list.value = prevList;
  if (prevRun && (prevRun === '__editor__' || keys.includes(prevRun))) forRun.value = prevRun;
  else forRun.value = keys[0];
}

function setSliderPair(rangeId, numId, value) {
  const r = document.getElementById(rangeId);
  const n = document.getElementById(numId);
  if (r) r.value = value;
  if (n) n.value = value;
}

function toggleConditionalFields() {
  const type = document.getElementById('tplType').value;
  document.getElementById('imageFields').style.display = (type === 'image') ? 'flex' : 'none';
  document.getElementById('textFields').style.display = (type === 'image') ? 'none' : 'flex';

  const pos = document.getElementById('tplPosition').value;
  document.getElementById('tileGapRow').style.display = (pos === 'tile') ? 'flex' : 'none';
  document.getElementById('marginRow').style.display = (pos === 'center') ? 'none' : 'flex';
}

function toggleFormatQuality() {
  const fmt = document.getElementById('outFormat').value;
  const showQ = (fmt === 'KEEP' || fmt === 'JPEG' || fmt === 'WEBP');
  document.getElementById('qualityRow').style.display = showQ ? 'flex' : 'none';
}

function loadTemplateIntoEditor(name) {
  const tpl = TEMPLATES[name];
  if (!tpl) return;
  document.getElementById('tplName').value = name;
  document.getElementById('tplType').value = tpl.type || 'text';
  document.getElementById('tplPosition').value = tpl.position || 'bottom-right';
  setSliderPair('tplScaleRange', 'tplScale', tpl.scale ?? 0.2);
  setSliderPair('tplOpacityRange', 'tplOpacity', tpl.opacity ?? 0.25);
  setSliderPair('tplMarginRange', 'tplMargin', tpl.margin ?? 16);
  setSliderPair('tplRotationRange', 'tplRotation', tpl.rotation ?? 0);
  setSliderPair('tplTileGapRange', 'tplTileGap', tpl.tile_gap ?? 80);

  document.getElementById('tplText').value = tpl.text || 'WATERMARK';
  const colorHex = tpl.text_color || '#ffffff';
  document.getElementById('tplColorHex').value = colorHex;
  if (/^#[0-9a-fA-F]{6}$/.test(colorHex)) {
    document.getElementById('tplColor').value = colorHex;
  }

  document.getElementById('fontFile').value = '';
  document.getElementById('wmImage').value = '';
  document.getElementById('fontInfo').textContent = tpl.font_path
    ? `Saved font: ${tpl.font_path.split(/[\\/]/).pop()}`
    : 'Using default font (DejaVuSans)';
  document.getElementById('wmImageInfo').textContent = tpl.image_path
    ? `Saved image: ${tpl.image_path.split(/[\\/]/).pop()}`
    : 'No watermark image selected';

  toggleConditionalFields();
}

function resetEditorForNewTemplate() {
  document.getElementById('tplName').value = '';
  document.getElementById('tplType').value = 'text';
  document.getElementById('tplPosition').value = 'bottom-right';
  setSliderPair('tplScaleRange', 'tplScale', 0.2);
  setSliderPair('tplOpacityRange', 'tplOpacity', 0.25);
  setSliderPair('tplMarginRange', 'tplMargin', 16);
  setSliderPair('tplRotationRange', 'tplRotation', 0);
  setSliderPair('tplTileGapRange', 'tplTileGap', 80);
  document.getElementById('tplText').value = '© My Brand';
  document.getElementById('tplColor').value = '#ffffff';
  document.getElementById('tplColorHex').value = '#ffffff';
  document.getElementById('fontFile').value = '';
  document.getElementById('wmImage').value = '';
  document.getElementById('fontInfo').textContent = 'Using default font (DejaVuSans)';
  document.getElementById('wmImageInfo').textContent = 'No watermark image selected';
  toggleConditionalFields();
  document.getElementById('tplName').focus();
}

function collectEditorTemplate() {
  const type = document.getElementById('tplType').value;
  const data = {
    type,
    position: document.getElementById('tplPosition').value,
    scale: parseFloat(document.getElementById('tplScale').value) || 0.2,
    opacity: parseFloat(document.getElementById('tplOpacity').value) ?? 0.25,
    margin: parseInt(document.getElementById('tplMargin').value, 10) || 0,
    rotation: parseFloat(document.getElementById('tplRotation').value) || 0,
    tile_gap: parseInt(document.getElementById('tplTileGap').value, 10) || 80,
  };
  if (type === 'image') {
    const refName = document.getElementById('tplList').value;
    if (refName && TEMPLATES[refName]?.image_path) {
      data.image_path = TEMPLATES[refName].image_path;
    }
  } else {
    data.text = document.getElementById('tplText').value || 'WATERMARK';
    data.text_color = document.getElementById('tplColorHex').value || '#FFFFFF';
    const refName = document.getElementById('tplList').value;
    if (refName && TEMPLATES[refName]?.font_path) {
      data.font_path = TEMPLATES[refName].font_path;
    }
  }
  return data;
}

function syncSliderAndNumber(rangeEl, numEl, clampMin, clampMax, onChange) {
  rangeEl.addEventListener('input', () => {
    numEl.value = rangeEl.value;
    if (onChange) onChange();
  });
  numEl.addEventListener('input', () => {
    const v = parseFloat(numEl.value);
    if (isNaN(v)) return;
    rangeEl.value = Math.min(clampMax, Math.max(clampMin, v));
    if (onChange) onChange();
  });
}

function saveSettingsToLocalStorage() {
  localStorage.setItem('wm_inputDir', document.getElementById('inputDir').value.trim());
  localStorage.setItem('wm_outputDir', document.getElementById('outputDir').value.trim());
  localStorage.setItem('wm_saveMode', document.getElementById('saveMode').value);
  localStorage.setItem('wm_workers', document.getElementById('workersCount').value);
}

function loadSettingsFromLocalStorage() {
  const id = localStorage.getItem('wm_inputDir');
  if (id) document.getElementById('inputDir').value = id;
  const od = localStorage.getItem('wm_outputDir');
  if (od) document.getElementById('outputDir').value = od;
  const sm = localStorage.getItem('wm_saveMode');
  if (sm === 'DIR' || sm === 'ZIP') document.getElementById('saveMode').value = sm;
  const wk = localStorage.getItem('wm_workers');
  if (wk) document.getElementById('workersCount').value = wk;
}

// ========================
// PREVIEW
// ========================
async function refreshPreview(silent = false) {
  const inputDir = document.getElementById('inputDir').value.trim();
  if (!inputDir) {
    if (!silent) toast('Specify the source folder first', 'error');
    return;
  }

  const previewBtn = document.getElementById('previewLocalBtn');
  const loader = document.getElementById('previewLoader');
  setLoading(previewBtn, true);
  if (loader) loader.style.display = 'block';

  try {
    const fd = new FormData();
    fd.append('input_dir', inputDir);
    fd.append('index', String(PreviewState.index));
    fd.append('show_original', PreviewState.showOriginal ? 'true' : 'false');

    const refTpl = document.getElementById('tplList').value;
    if (refTpl) fd.append('template_name', refTpl);
    fd.append('inline_template', JSON.stringify(collectEditorTemplate()));

    const wmFile = document.getElementById('wmImage').files[0];
    if (wmFile) fd.append('watermark_image', wmFile, wmFile.name);
    const fFile = document.getElementById('fontFile').files[0];
    if (fFile) fd.append('font_file', fFile, fFile.name);

    const res = await fetch('/api/preview_local', { method: 'POST', body: fd });
    if (!res.ok) {
      const t = await res.json().catch(() => ({}));
      throw new Error(t.detail || res.statusText);
    }

    const total = parseInt(res.headers.get('X-Total-Images') || '0', 10);
    const idx = parseInt(res.headers.get('X-Image-Index') || '0', 10);
    const fname = res.headers.get('X-Image-Name') || '';
    const w = res.headers.get('X-Image-Width') || '';
    const h = res.headers.get('X-Image-Height') || '';

    PreviewState.total = total;
    PreviewState.index = idx;

    const blob = await res.blob();
    const newUrl = URL.createObjectURL(blob);
    const imgEl = document.getElementById('previewImg');
    if (PreviewState.objectUrl) URL.revokeObjectURL(PreviewState.objectUrl);
    PreviewState.objectUrl = newUrl;
    imgEl.src = newUrl;

    const meta = document.getElementById('previewMeta');
    const modeTag = PreviewState.showOriginal ? 'ORIGINAL' : 'WATERMARKED';
    meta.textContent = `${fname} • ${w}×${h} • ${idx + 1} / ${total} (${modeTag})`;
  } catch (err) {
    if (!silent) toast(err.message || 'Failed to build preview', 'error', 'Preview error');
  } finally {
    setLoading(previewBtn, false);
    if (loader) loader.style.display = 'none';
  }
}

function scheduleAutoPreview() {
  if (!document.getElementById('autoPreviewToggle')?.checked) return;
  if (!document.getElementById('inputDir').value.trim()) return;
  if (PreviewState.debounceTimer) clearTimeout(PreviewState.debounceTimer);
  PreviewState.debounceTimer = setTimeout(() => refreshPreview(true), 350);
}

async function scanFolder(silent = false) {
  const inputDir = document.getElementById('inputDir').value.trim();
  const summaryEl = document.getElementById('scanSummary');
  if (!inputDir) {
    summaryEl.textContent = 'Enter or browse a folder to scan images';
    return;
  }
  summaryEl.textContent = 'Scanning folder…';
  try {
    const fd = new FormData();
    fd.append('input_dir', inputDir);
    const outDir = document.getElementById('outputDir').value.trim();
    if (outDir && document.getElementById('saveMode').value === 'DIR') {
      fd.append('output_dir', outDir);
    }
    const res = await fetch('/api/scan_local', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    PreviewState.total = data.count;
    summaryEl.textContent = `${data.count.toLocaleString()} image(s) found (${data.scan_ms} ms)`;
    if (!silent) toast(`Found ${data.count.toLocaleString()} images`, 'info');
  } catch (err) {
    summaryEl.textContent = err.message || 'Scan failed';
    if (!silent) toast(err.message || 'Scan failed', 'error');
  }
}

// ========================
// PROGRESS UI & JOB RUNNER
// ========================
function startProgressUI(total) {
  document.getElementById('progressRow').style.display = 'flex';
  document.getElementById('progressBar').style.width = '0%';
  document.getElementById('progressPct').textContent = '0%';
  document.getElementById('progressText').textContent = `0 / ${(total || 0).toLocaleString()}`;
  document.getElementById('progressMeta').textContent = 'Starting…';
  document.getElementById('zipDownloadWrap').style.display = 'none';
  document.getElementById('errorDetailsWrap').style.display = 'none';
  document.getElementById('errorList').innerHTML = '';
}

function renderProgressSnapshot(d) {
  const tot = Math.max(1, d.total || 1);
  const done = d.done || 0;
  const pct = d.state === 'done' ? 100 : Math.min(99, Math.floor((done * 100) / tot));

  document.getElementById('progressBar').style.width = pct + '%';
  document.getElementById('progressPct').textContent = pct + '%';

  let countText = `${done.toLocaleString()} / ${tot.toLocaleString()}`;
  if (d.skipped > 0) countText += ` (skipped: ${d.skipped.toLocaleString()})`;
  document.getElementById('progressText').textContent = countText;

  const parts = [];
  if (d.rate > 0) parts.push(`${d.rate} img/s`);
  if (d.elapsed_sec >= 0) parts.push(`Elapsed ${formatDuration(d.elapsed_sec)}`);
  if (d.eta_sec > 0 && d.state === 'running') parts.push(`ETA ${formatDuration(d.eta_sec)}`);
  if (d.errors > 0) parts.push(`Errors: ${d.errors}`);
  document.getElementById('progressMeta').textContent = parts.join(' • ');

  if (d.error_samples && d.error_samples.length > 0) {
    const wrap = document.getElementById('errorDetailsWrap');
    const summary = document.getElementById('errorSummary');
    const list = document.getElementById('errorList');
    wrap.style.display = 'block';
    summary.textContent = `Errors (${d.errors}${d.errors > d.error_samples.length ? `, showing first ${d.error_samples.length}` : ''})`;
    list.innerHTML = '';
    d.error_samples.forEach((item) => {
      const li = document.createElement('li');
      li.textContent = `${item.file}: ${item.error}`;
      list.appendChild(li);
    });
  }
}

function handleJobFinished(d) {
  renderProgressSnapshot(d);
  const cancelled = d.state === 'cancelled' || d.cancelled;
  if (!cancelled && d.save_mode === 'ZIP' && d.zip_ready) {
    const wrap = document.getElementById('zipDownloadWrap');
    const link = document.getElementById('zipDownloadLink');
    link.href = `/api/download_job_zip/${encodeURIComponent(d.job_id)}`;
    wrap.style.display = 'block';
    link.click();
  }
  toast(
    cancelled
      ? `Stopped at ${d.done}/${d.total}. Processed: ${d.processed}`
      : `Finished ${d.processed.toLocaleString()} image(s) in ${formatDuration(d.elapsed_sec)}${d.skipped ? `, skipped ${d.skipped.toLocaleString()}` : ''}${d.errors ? `, errors: ${d.errors}` : ''}`,
    cancelled ? 'error' : 'success',
    cancelled ? 'Stopped' : 'Completed'
  );
  resetCurrentJob();
}

function startPollingLoop(jobId) {
  CurrentJob.timer = setInterval(async () => {
    try {
      const r = await fetch(`/api/process_local_poll_status?job_id=${encodeURIComponent(jobId)}`);
      if (!r.ok) return;
      const d = await r.json();
      renderProgressSnapshot(d);
      if (d.state === 'done' || d.state === 'cancelled') {
        handleJobFinished(d);
      }
    } catch (_) {}
  }, 600);
}

// ========================
// DOMContentLoaded main
// ========================
document.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  setStopEnabled(false);

  // Collapsible template editor
  const tplCard = document.getElementById('tplCard');
  const tplBody = document.getElementById('tplBody');
  const toggleTpl = document.getElementById('toggleTpl');
  function applyTplCollapsed(collapsed) {
    tplCard.classList.toggle('collapsed', collapsed);
    tplBody.style.display = collapsed ? 'none' : 'block';
    toggleTpl.textContent = collapsed ? '▸' : '▾';
  }
  let tplCollapsed = localStorage.getItem('wm_tpl_collapsed') === '1';
  applyTplCollapsed(tplCollapsed);
  toggleTpl.addEventListener('click', () => {
    tplCollapsed = !tplCollapsed;
    localStorage.setItem('wm_tpl_collapsed', tplCollapsed ? '1' : '0');
    applyTplCollapsed(tplCollapsed);
  });

  // Color picker sync
  const color = document.getElementById('tplColor');
  const colorHex = document.getElementById('tplColorHex');
  color.addEventListener('input', () => {
    colorHex.value = color.value;
    scheduleAutoPreview();
  });
  colorHex.addEventListener('input', () => {
    const v = colorHex.value.trim();
    if (/^#?[0-9a-fA-F]{6}$/.test(v)) {
      color.value = v.startsWith('#') ? v : ('#' + v);
      scheduleAutoPreview();
    }
  });

  document.getElementById('tplType').addEventListener('change', () => {
    toggleConditionalFields();
    scheduleAutoPreview();
  });
  document.getElementById('tplPosition').addEventListener('change', () => {
    toggleConditionalFields();
    scheduleAutoPreview();
  });
  document.getElementById('tplText').addEventListener('input', scheduleAutoPreview);
  document.getElementById('wmImage').addEventListener('change', scheduleAutoPreview);
  document.getElementById('fontFile').addEventListener('change', scheduleAutoPreview);

  // Sliders sync
  syncSliderAndNumber(document.getElementById('tplScaleRange'), document.getElementById('tplScale'), 0.02, 1, scheduleAutoPreview);
  syncSliderAndNumber(document.getElementById('tplOpacityRange'), document.getElementById('tplOpacity'), 0, 1, scheduleAutoPreview);
  syncSliderAndNumber(document.getElementById('tplMarginRange'), document.getElementById('tplMargin'), 0, 400, scheduleAutoPreview);
  syncSliderAndNumber(document.getElementById('tplRotationRange'), document.getElementById('tplRotation'), -180, 180, scheduleAutoPreview);
  syncSliderAndNumber(document.getElementById('tplTileGapRange'), document.getElementById('tplTileGap'), 10, 600, scheduleAutoPreview);

  // Load templates & populate editor
  const tpls = await fetchTemplates();
  populateTemplatesSelects(tpls);
  if (document.getElementById('tplList').value) {
    loadTemplateIntoEditor(document.getElementById('tplList').value);
  } else {
    toggleConditionalFields();
  }

  document.getElementById('tplList').addEventListener('change', (e) => {
    const name = e.target.value;
    if (name) {
      loadTemplateIntoEditor(name);
      document.getElementById('tplForRun').value = name;
      scheduleAutoPreview();
    }
  });

  document.getElementById('newTplBtn').addEventListener('click', resetEditorForNewTemplate);

  document.getElementById('refreshTpls').addEventListener('click', async () => {
    const latest = await fetchTemplates();
    populateTemplatesSelects(latest);
    if (document.getElementById('tplList').value) loadTemplateIntoEditor(document.getElementById('tplList').value);
    toast('Templates reloaded', 'success');
  });

  document.getElementById('deleteTpl').addEventListener('click', async () => {
    const name = document.getElementById('tplList').value;
    if (!name) { toast('No template selected for deletion', 'error'); return; }
    if (!confirm(`Delete template "${name}"?`)) return;
    const res = await fetch(`/api/templates/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (res.ok) {
      const latest = await fetchTemplates();
      populateTemplatesSelects(latest);
      if (document.getElementById('tplList').value) loadTemplateIntoEditor(document.getElementById('tplList').value);
      toast('Template deleted', 'success');
    } else {
      const t = await res.json().catch(() => ({}));
      toast(t.detail || res.statusText, 'error', 'Error');
    }
  });

  document.getElementById('saveTpl').addEventListener('click', async () => {
    const name = document.getElementById('tplName').value.trim();
    if (!name) { toast('Enter a template name', 'error'); return; }

    const fd = new FormData();
    fd.append('name', name);
    const type = document.getElementById('tplType').value;
    fd.append('type', type);
    fd.append('position', document.getElementById('tplPosition').value);
    fd.append('scale', document.getElementById('tplScale').value || '0.2');
    fd.append('opacity', document.getElementById('tplOpacity').value || '0.25');
    fd.append('margin', document.getElementById('tplMargin').value || '16');
    fd.append('rotation', document.getElementById('tplRotation').value || '0');
    fd.append('tile_gap', document.getElementById('tplTileGap').value || '80');

    if (type === 'image') {
      const wm = document.getElementById('wmImage').files[0];
      if (wm) fd.append('watermark_image', wm, wm.name);
    } else {
      fd.append('text', document.getElementById('tplText').value || 'WATERMARK');
      fd.append('text_color', document.getElementById('tplColorHex').value || '#FFFFFF');
      const font = document.getElementById('fontFile').files[0];
      if (font) fd.append('font_file', font, font.name);
    }

    const res = await fetch('/api/templates', { method: 'POST', body: fd });
    if (res.ok) {
      const latest = await fetchTemplates();
      populateTemplatesSelects(latest, name);
      loadTemplateIntoEditor(name);
      toast(`Template "${name}" saved`, 'success');
    } else {
      const t = await res.json().catch(() => ({}));
      toast(t.detail || res.statusText, 'error', 'Error');
    }
  });

  // Persist paths & settings
  loadSettingsFromLocalStorage();
  const saveMode = document.getElementById('saveMode');
  const outDirRow = document.getElementById('outDirRow');
  const syncSaveModeUI = () => {
    outDirRow.style.display = (saveMode.value === 'DIR') ? 'flex' : 'none';
  };
  syncSaveModeUI();
  saveMode.addEventListener('change', () => {
    syncSaveModeUI();
    saveSettingsToLocalStorage();
  });

  document.getElementById('outFormat').addEventListener('change', toggleFormatQuality);
  toggleFormatQuality();
  document.getElementById('workersCount').addEventListener('change', saveSettingsToLocalStorage);
  document.getElementById('outputDir').addEventListener('change', saveSettingsToLocalStorage);
  document.getElementById('inputDir').addEventListener('change', () => {
    saveSettingsToLocalStorage();
    PreviewState.index = 0;
    scanFolder(true);
    scheduleAutoPreview();
  });

  // Folder pickers
  async function pickInto(inputEl) {
    try {
      const res = await fetch('/api/pick_folder', { method: 'POST' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);
      if (!data.cancelled && data.path) {
        inputEl.value = data.path;
        inputEl.dispatchEvent(new Event('change'));
      }
    } catch (err) {
      toast(err.message || 'Folder picker unavailable', 'error');
    }
  }
  document.getElementById('pickInputBtn').addEventListener('click', () => pickInto(document.getElementById('inputDir')));
  document.getElementById('pickOutputBtn').addEventListener('click', () => pickInto(document.getElementById('outputDir')));
  document.getElementById('scanDirBtn').addEventListener('click', () => scanFolder(false));

  if (document.getElementById('inputDir').value.trim()) {
    scanFolder(true);
    refreshPreview(true);
  }

  // Preview navigation & compare
  document.getElementById('previewLocalBtn').addEventListener('click', () => refreshPreview(false));
  document.getElementById('prevImgBtn').addEventListener('click', () => {
    PreviewState.index = Math.max(0, PreviewState.index - 1);
    refreshPreview(false);
  });
  document.getElementById('nextImgBtn').addEventListener('click', () => {
    const maxIdx = Math.max(0, PreviewState.total - 1);
    PreviewState.index = Math.min(maxIdx, PreviewState.index + 1);
    refreshPreview(false);
  });
  document.getElementById('randImgBtn').addEventListener('click', () => {
    if (PreviewState.total > 1) {
      PreviewState.index = Math.floor(Math.random() * PreviewState.total);
    }
    refreshPreview(false);
  });
  const cmpBtn = document.getElementById('compareOriginalBtn');
  cmpBtn.addEventListener('click', () => {
    PreviewState.showOriginal = !PreviewState.showOriginal;
    cmpBtn.classList.toggle('active', PreviewState.showOriginal);
    refreshPreview(false);
  });

  // Batch Start
  const processBtn = document.getElementById('processLocalBtn');
  processBtn.addEventListener('click', async () => {
    if (CurrentJob.id) { toast('A job is already running', 'error'); return; }
    const inputDir = document.getElementById('inputDir').value.trim();
    const runChoice = document.getElementById('tplForRun').value;
    if (!inputDir) { toast('Specify the source folder', 'error'); return; }

    const mode = document.getElementById('saveMode').value;
    const outDir = document.getElementById('outputDir').value.trim();
    if (mode === 'DIR' && !outDir) {
      toast('Specify the destination folder', 'error');
      return;
    }

    const payload = {
      input_dir: inputDir,
      output_format: document.getElementById('outFormat').value,
      quality: parseInt(document.getElementById('outQuality').value || '90', 10),
      output_dir: outDir,
      overwrite: document.getElementById('overwrite').checked,
      open_when_done: document.getElementById('openWhenDone').checked,
      save_mode: mode,
      workers: parseInt(document.getElementById('workersCount').value || '0', 10),
      preserve_exif: document.getElementById('preserveExif').checked,
    };

    if (runChoice === '__editor__' || !runChoice) {
      const refTpl = document.getElementById('tplList').value;
      if (refTpl) payload.template_name = refTpl;
      payload.inline_template = JSON.stringify(collectEditorTemplate());
    } else {
      payload.template_name = runChoice;
    }

    setLoading(processBtn, true);
    try {
      const res = await fetch('/api/process_local_start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || res.statusText);
      }

      CurrentJob.id = data.job_id;
      setStopEnabled(true);
      startProgressUI(data.total || 0);

      const es = new EventSource(`/api/job_events?job_id=${encodeURIComponent(data.job_id)}`);
      CurrentJob.es = es;
      let gotEvent = false;

      es.addEventListener('start', (e) => {
        gotEvent = true;
        try { renderProgressSnapshot(JSON.parse(e.data)); } catch (_) {}
      });
      es.addEventListener('progress', (e) => {
        gotEvent = true;
        try { renderProgressSnapshot(JSON.parse(e.data)); } catch (_) {}
      });
      es.addEventListener('done', (e) => {
        gotEvent = true;
        try { handleJobFinished(JSON.parse(e.data)); } catch (_) { resetCurrentJob(); }
      });
      es.onerror = () => {
        es.close();
        CurrentJob.es = null;
        if (CurrentJob.id && !CurrentJob.timer) {
          if (!gotEvent) toast('SSE disconnected, switching to polling…', 'info');
          startPollingLoop(CurrentJob.id);
        }
      };
    } catch (err) {
      toast(err.message || 'Failed to start job', 'error', 'Error');
      resetCurrentJob();
    }
  });

  // Batch Stop
  document.getElementById('stopBtn').addEventListener('click', async () => {
    if (!CurrentJob.id) { toast('No active job', 'error'); return; }
    const jobId = CurrentJob.id;
    try {
      const r = await fetch('/api/cancel_job', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_id: jobId }),
      });
      if (!r.ok) {
        const t = await r.json().catch(() => ({}));
        throw new Error(t.detail || r.statusText);
      }
      toast('Stopping job…', 'info');
      if (!CurrentJob.es && !CurrentJob.timer) {
        startPollingLoop(jobId);
      }
    } catch (err) {
      toast(err.message || 'Failed to cancel', 'error');
    }
  });
});
