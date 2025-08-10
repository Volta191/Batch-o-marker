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
    btn.title = isDark ? 'Switch to light (T)' : 'Switch to dark (T)';
  }
}
function initTheme() {
  const saved = localStorage.getItem('wm_theme');
  let theme = saved;
  if (!theme) {
    theme = (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches)
      ? 'light' : 'dark';
  }
  applyTheme(theme);
  const toggle = () => {
    const next = (document.documentElement.getAttribute('data-theme') === 'dark') ? 'light' : 'dark';
    localStorage.setItem('wm_theme', next);
    applyTheme(next);
  };
  const btn = document.getElementById('themeToggle');
  if (btn) btn.addEventListener('click', toggle);
  document.addEventListener('keydown', (e) => {
    if (e.key.toLowerCase() === 't' && !e.altKey && !e.ctrlKey && !e.metaKey) {
      e.preventDefault(); toggle();
    }
  });
}

// ========================
// TOASTS & LOADING
// ========================
function toast(message, type = 'info', title = '') {
  const host = document.getElementById('toaster');
  if (!host) { alert(message); return; }
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `${title ? `<div class="title">${title}</div>` : ''}<div class="msg">${message}</div>`;
  host.appendChild(el);
  const close = () => { el.classList.add('hide'); setTimeout(() => el.remove(), 180); };
  setTimeout(close, 3500);
  el.addEventListener('click', close);
}
function setLoading(btn, isLoading) {
  if (!btn) return;
  if (isLoading) btn.classList.add('loading'); else btn.classList.remove('loading');
}
async function withLoading(btn, fn) {
  try { setLoading(btn, true); return await fn(); }
  finally { setLoading(btn, false); }
}

// ========================
// CURRENT JOB STATE
// ========================
const CurrentJob = {
  id: null,       // job_id
  mode: null,     // "SSE" | "POLL"
  es: null,       // EventSource
  timer: null     // polling interval id
};
function setStopEnabled(enabled) {
  const stopBtn = document.getElementById('stopBtn');
  if (!stopBtn) return;
  stopBtn.disabled = !enabled;
  stopBtn.classList.toggle('is-disabled', !enabled);
}
function resetCurrentJob() {
  if (CurrentJob.timer) { clearInterval(CurrentJob.timer); }
  if (CurrentJob.es) { try { CurrentJob.es.close(); } catch(_){} }
  CurrentJob.id = null;
  CurrentJob.mode = null;
  CurrentJob.es = null;
  CurrentJob.timer = null;
  setStopEnabled(false);
  setLoading(document.getElementById('processLocalBtn'), false);
  setLoading(document.getElementById('previewLocalBtn'), false);
}

// ========================
// HELPERS (UI/ФОРМЫ)
// ========================
async function fetchTemplates(){ const res = await fetch('/api/templates'); return await res.json(); }
function populateTemplatesSelects(tpls){
  const list = document.getElementById('tplList');
  const forRun = document.getElementById('tplForRun');
  list.innerHTML = ''; forRun.innerHTML = '';
  const keys = Object.keys(tpls);
  if(keys.length === 0){
    const opt = document.createElement('option');
    opt.value = ''; opt.textContent = '— none —';
    list.appendChild(opt.cloneNode(true)); forRun.appendChild(opt.cloneNode(true)); return;
  }
  keys.forEach(k => {
    const opt = document.createElement('option');
    opt.value = k; opt.textContent = k;
    list.appendChild(opt); forRun.appendChild(opt.cloneNode(true));
  });
}
function toggleTypeFields(){
  const v = document.getElementById('tplType').value;
  const textFields = document.getElementById('textFields');
  const imageFields = document.getElementById('imageFields');
  if(v === 'image'){ imageFields.style.display='flex'; textFields.style.display='none'; }
  else { imageFields.style.display='none'; textFields.style.display='flex'; }
}
function syncSliderAndNumber(rangeEl, numEl, clampMin, clampMax){
  rangeEl.addEventListener('input', () => { numEl.value = rangeEl.value; });
  numEl.addEventListener('input', () => {
    const v = parseFloat(numEl.value); if(isNaN(v)) return;
    rangeEl.value = Math.min(clampMax, Math.max(clampMin, v));
  });
}
function savePathsToLocalStorage(){
  localStorage.setItem('wm_inputDir', document.getElementById('inputDir').value.trim());
  localStorage.setItem('wm_outputDir', document.getElementById('outputDir').value.trim());
}
function loadPathsFromLocalStorage(){
  const id = localStorage.getItem('wm_inputDir'); if(id) document.getElementById('inputDir').value = id;
  const od = localStorage.getItem('wm_outputDir'); if(od) document.getElementById('outputDir').value = od;
}
function startProgressUI(total){
  const progressRow = document.getElementById('progressRow');
  const progressBar = document.getElementById('progressBar');
  const progressText = document.getElementById('progressText');
  progressRow.style.display = 'block';
  progressBar.style.width = '0%';
  progressText.textContent = `${Math.min(0,total)}/${total||0}`;
}

// ========================
// POLLING mode
// ========================
async function startPolling(inputDir, tplName, outFormat, outQuality, outDir, overwrite, openWhenDone){
  const res = await fetch('/api/process_local_poll_start',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      input_dir: inputDir,
      template_name: tplName,
      output_format: outFormat,
      quality: outQuality,
      output_dir: outDir,
      overwrite: overwrite === 'true',
      open_when_done: openWhenDone === 'true'
    })
  });
  if(!res.ok){
    const t = await res.json().catch(()=>({}));
    throw new Error(t.detail || res.statusText);
  }
  const data = await res.json();
  const total = data.total || 0;
  startProgressUI(total);

  CurrentJob.id = data.job_id;
  CurrentJob.mode = 'POLL';
  setStopEnabled(true);

  const progressBar = document.getElementById('progressBar');
  const progressText = document.getElementById('progressText');

  CurrentJob.timer = setInterval(async () => {
    try{
      const r = await fetch(`/api/process_local_poll_status?job_id=${encodeURIComponent(CurrentJob.id)}`);
      const d = await r.json();
      const done = d.done || 0;
      const tot = Math.max(1, d.total || total || 1);
      progressBar.style.width = Math.floor(done*100/tot) + '%';
      progressText.textContent = `${done}/${tot}`;
      if(d.state === 'done' || d.state === 'cancelled'){
        clearInterval(CurrentJob.timer);
        CurrentJob.timer = null;
        progressBar.style.width = d.state === 'done' ? '100%' : progressBar.style.width;
        progressText.textContent = d.state === 'done' ? `${tot}/${tot}` : `${done}/${tot}`;
        toast(
          `${d.state === 'done' ? 'Done' : 'Cancelled'}. Output: ${d.out_dir}${d.errors?`. Errors: ${d.errors}`:''}`,
          d.state === 'done' ? 'success' : 'error',
          d.state === 'done' ? 'Completed' : 'Stopped'
        );
        resetCurrentJob();
      }
    }catch(_){ /* ignore once */ }
  }, 1000);
}

// ========================
// DOMContentLoaded main
// ========================
document.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  setStopEnabled(false);

  // Collapsible template editor (strong collapse + remember state)
const tplCard = document.getElementById('tplCard');
const tplBody = document.getElementById('tplBody');
const toggleTpl = document.getElementById('toggleTpl');
const mainGrid = document.getElementById('mainGrid');

function applyTplCollapsed(collapsed) {
  tplCard.classList.toggle('collapsed', collapsed);
  tplBody.style.display = collapsed ? 'none' : 'block';
  toggleTpl.textContent = collapsed ? '▸' : '▾';
}

// начальное состояние из localStorage
let tplCollapsed = (localStorage.getItem('wm_tpl_collapsed') === '1');
applyTplCollapsed(tplCollapsed);

// клики по кнопке
toggleTpl.addEventListener('click', () => {
  tplCollapsed = !tplCollapsed;
  localStorage.setItem('wm_tpl_collapsed', tplCollapsed ? '1' : '0');
  applyTplCollapsed(tplCollapsed);
});

// (опционально) кликабельная шапка карточки для сворачивания
const tplHead = tplCard.querySelector('.card-head');
if (tplHead) {
  tplHead.style.cursor = 'pointer';
  tplHead.addEventListener('click', (e) => {
    // чтобы клик по самой кнопке не дублировался
    if (e.target === toggleTpl) return;
    toggleTpl.click();
  });
}


  // Color picker sync
  const color = document.getElementById('tplColor');
  const colorHex = document.getElementById('tplColorHex');
  if (color && colorHex) {
    color.addEventListener('input', () => { colorHex.value = color.value; });
    colorHex.addEventListener('input', () => {
      const v = colorHex.value.trim();
      if(/^#?[0-9a-fA-F]{6}$/.test(v)) color.value = v.startsWith('#') ? v : ('#'+v);
    });
  }

  document.getElementById('tplType').addEventListener('change', toggleTypeFields);
  toggleTypeFields();

  // Templates
  const tpls = await fetchTemplates();
  populateTemplatesSelects(tpls);

  document.getElementById('refreshTpls').addEventListener('click', async () => {
    const tpls = await fetchTemplates();
    populateTemplatesSelects(tpls);
    toast('Templates reloaded', 'success');
  });

  document.getElementById('deleteTpl').addEventListener('click', async () => {
    const name = document.getElementById('tplList').value;
    if(!name){ toast('No template selected for deletion', 'error'); return; }
    if(!confirm(`Delete template "${name}"?`)) return;
    const res = await fetch(`/api/templates/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if(res.ok){
      const tpls = await fetchTemplates();
      populateTemplatesSelects(tpls);
      toast('Template deleted', 'success');
    }else{
      const t = await res.json().catch(() => ({}));
      toast(t.detail || res.statusText, 'error', 'Error');
    }
  });

  // Sliders sync
  syncSliderAndNumber(document.getElementById('tplScaleRange'), document.getElementById('tplScale'), 0.02, 1);
  syncSliderAndNumber(document.getElementById('tplOpacityRange'), document.getElementById('tplOpacity'), 0, 1);
  syncSliderAndNumber(document.getElementById('tplMarginRange'), document.getElementById('tplMargin'), 0, 400);
  syncSliderAndNumber(document.getElementById('tplRotationRange'), document.getElementById('tplRotation'), -180, 180);
  syncSliderAndNumber(document.getElementById('tplTileGapRange'), document.getElementById('tplTileGap'), 10, 600);

  // Save template
  document.getElementById('saveTpl').addEventListener('click', async () => {
    const fd = new FormData();
    const name = document.getElementById('tplName').value.trim();
    if(!name){ toast('Enter a template name', 'error'); return; }
    fd.append('name', name);
    const type = document.getElementById('tplType').value;
    fd.append('type', type);
    fd.append('position', document.getElementById('tplPosition').value);
    fd.append('scale', document.getElementById('tplScale').value || '0.2');
    fd.append('opacity', document.getElementById('tplOpacity').value || '0.25');
    fd.append('margin', document.getElementById('tplMargin').value || '16');
    fd.append('rotation', document.getElementById('tplRotation').value || '0');
    fd.append('tile_gap', document.getElementById('tplTileGap').value || '80');

    if(type === 'image'){
      const wm = document.getElementById('wmImage').files[0];
      if(wm) fd.append('watermark_image', wm, wm.name);
    }else{
      fd.append('text', document.getElementById('tplText').value || 'WATERMARK');
      fd.append('text_color', document.getElementById('tplColorHex').value || '#FFFFFF');
      const font = document.getElementById('fontFile').files[0];
      if(font) fd.append('font_file', font, font.name);
    }

    const res = await fetch('/api/templates', { method:'POST', body: fd });
    if(res.ok){
      const tpls = await fetchTemplates();
      populateTemplatesSelects(tpls);
      toast('Template saved', 'success');
    }else{
      const t = await res.json().catch(() => ({}));
      toast(t.detail || res.statusText, 'error', 'Error');
    }
  });

  // Persist paths
  loadPathsFromLocalStorage();
  document.getElementById('inputDir').addEventListener('change', savePathsToLocalStorage);
  document.getElementById('outputDir').addEventListener('change', savePathsToLocalStorage);

  // Processing controls
  const saveMode = document.getElementById('saveMode');
  const outDirRow = document.getElementById('outDirRow');
  const progressRow = document.getElementById('progressRow');
  const progressBar = document.getElementById('progressBar');
  const progressText = document.getElementById('progressText');

  saveMode.addEventListener('change', () => {
    outDirRow.style.display = (saveMode.value === 'DIR') ? 'block' : 'none';
    progressRow.style.display = 'none';
  });

  const processBtn = document.getElementById('processLocalBtn');
  const stopBtn = document.getElementById('stopBtn');
  const previewBtn = document.getElementById('previewLocalBtn');

  // Start processing
  processBtn.addEventListener('click', async () => {
    await withLoading(processBtn, async () => {
      const inputDir = document.getElementById('inputDir').value.trim();
      const tplName = document.getElementById('tplForRun').value || document.getElementById('tplList').value;
      if(!inputDir){ toast('Specify the source folder', 'error'); return; }
      if(!tplName){ toast('Select a saved template', 'error'); return; }

      if(saveMode.value === 'DIR'){
        const outDir = document.getElementById('outputDir').value.trim();
        if(!outDir){ toast('Specify the destination folder', 'error'); return; }
        const overwrite = document.getElementById('overwrite').checked ? 'true' : 'false';
        const openWhenDone = document.getElementById('openWhenDone').checked ? 'true' : 'false';

        // Try SSE first
        startProgressUI(0);
        const params = new URLSearchParams({
          input_dir: inputDir,
          template_name: tplName,
          output_format: document.getElementById('outFormat').value,
          quality: document.getElementById('outQuality').value || '90',
          output_dir: outDir,
          overwrite,
          open_when_done: openWhenDone
        });

        // генерим client job id и передаём
        const clientJobId = 'sse_' + Date.now().toString(36) + '_' + Math.random().toString(16).slice(2);
        params.set('job_id', clientJobId);

        const url = '/api/process_local_sse?' + params.toString();
        const es = new EventSource(url);
        CurrentJob.id = clientJobId;
        CurrentJob.es = es;
        CurrentJob.mode = 'SSE';
        setStopEnabled(true);

        let gotAnyEvent = false;
        let lastTotal = 0;  // локальный total

        es.addEventListener('start', (e) => {
          gotAnyEvent = true;
          try{
            const d = JSON.parse(e.data);
            lastTotal = d.total || 0;
            startProgressUI(lastTotal);
          }catch(_){}
        });

        es.addEventListener('progress', (e) => {
          gotAnyEvent = true;
          try{
            const d = JSON.parse(e.data);
            lastTotal = d.total || lastTotal;
            const tot = Math.max(1, lastTotal);
            const done = d.done || 0;
            const pct = Math.floor(done*100/tot);
            progressBar.style.width = pct + '%';
            progressText.textContent = `${done}/${tot}`;
          }catch(_){}
        });

        es.addEventListener('done', (e) => {
          gotAnyEvent = true;
          try{
            const d = JSON.parse(e.data);
            if (d.cancelled) {
              const tot = Math.max(1, lastTotal || d.processed || 1);
              progressText.textContent = `${d.processed}/${tot}`;
              toast(`Cancelled. Output: ${d.out_dir}${d.errors?`. Errors: ${d.errors}`:''}`, 'error', 'Stopped');
            } else {
              const tot = Math.max(1, lastTotal || d.processed || 1);
              progressBar.style.width = '100%';
              progressText.textContent = `${tot}/${tot}`;
              toast(`Output: ${d.out_dir}${d.errors?`. Errors: ${d.errors}`:''}`, 'success', 'Done');
            }
          }catch(_){}
          es.close();
          resetCurrentJob();
        });

        es.onerror = async () => {
          es.close();
          // если ничего не пришло — фоллбек на polling
          if(!gotAnyEvent){
            try{
              toast('SSE not available, switching to polling…', 'info');
              await startPolling(
                inputDir, tplName,
                document.getElementById('outFormat').value,
                document.getElementById('outQuality').value || '90',
                outDir, overwrite, openWhenDone
              );
            }catch(err){ toast(err.message || 'Processing failed', 'error', 'Error'); }
          }
          if (CurrentJob.mode === 'SSE') resetCurrentJob();
        };

      } else {
        // ZIP mode
        const fd = new FormData();
        fd.append('input_dir', inputDir);
        fd.append('template_name', tplName);
        fd.append('output_format', document.getElementById('outFormat').value);
        fd.append('quality', document.getElementById('outQuality').value || '90');
        try{
          const res = await fetch('/api/process_local', { method:'POST', body: fd });
          if(!res.ok){
            const t = await res.json().catch(()=>({}));
            throw new Error(t.detail || res.statusText);
          }
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url; a.download = 'watermarked.zip';
          document.body.appendChild(a); a.click(); a.remove();
          toast('Archive downloaded', 'success');
        }catch(err){
          toast(err.message || 'Processing failed', 'error', 'Error');
        }
      }
    });
  });

  // STOP button
  document.getElementById('stopBtn').addEventListener('click', async () => {
    if (!CurrentJob.id) { toast('No active job', 'error'); return; }
    try {
      if (CurrentJob.es) { try { CurrentJob.es.close(); } catch(_){} }
      if (CurrentJob.timer) { clearInterval(CurrentJob.timer); CurrentJob.timer = null; }

      const r = await fetch('/api/cancel_job', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ job_id: CurrentJob.id })
      });
      if (!r.ok) {
        const t = await r.json().catch(()=>({}));
        throw new Error(t.detail || r.statusText);
      }
      toast('Cancellation requested', 'success');

      // лёгкий доглад статуса
      setTimeout(async () => {
        try {
          const rr = await fetch(`/api/process_local_poll_status?job_id=${encodeURIComponent(CurrentJob.id)}`);
          if (rr.ok) {
            const d = await rr.json();
            if (d.state === 'cancelled' || d.state === 'done') {
              const tot = Math.max(1, d.total || 1);
              const done = d.done || 0;
              document.getElementById('progressBar').style.width = Math.floor(done*100/tot) + '%';
              document.getElementById('progressText').textContent = `${done}/${tot}`;
            }
          }
        } catch(_){}
        resetCurrentJob();
      }, 700);

    } catch (err) {
      toast(err.message || 'Failed to cancel', 'error');
    }
  });

  // Preview button
  document.getElementById('previewLocalBtn').addEventListener('click', async () => {
    await withLoading(previewBtn, async () => {
      const inputDir = document.getElementById('inputDir').value.trim();
      if(!inputDir){ toast('Specify the source folder', 'error'); return; }
      const tplName = document.getElementById('tplForRun').value || document.getElementById('tplList').value;
      if(!tplName){ toast('Select a saved template', 'error'); return; }
      const fd = new FormData();
      fd.append('input_dir', inputDir);
      fd.append('template_name', tplName);
      try{
        const res = await fetch('/api/preview_local', { method:'POST', body: fd });
        if(!res.ok){
          const t = await res.json().catch(()=>({}));
          throw new Error(t.detail || res.statusText);
        }
        const blob = await res.blob();
        document.getElementById('previewImg').src = URL.createObjectURL(blob);
        toast('Preview updated', 'success');
      }catch(err){
        toast(err.message || 'Failed to build preview', 'error', 'Preview error');
      }
    });
  });
});
