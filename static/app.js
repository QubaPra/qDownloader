// ── Zakładki ─────────────────────────────────────────────────────────────────
const tabYt = document.getElementById('tab-yt');
const tabTwitch = document.getElementById('tab-twitch');
const ytPanel = document.getElementById('youtube-panel');
const twitchPanel = document.getElementById('twitch-panel');

tabYt.onclick = () => {
  tabYt.classList.add('active'); tabTwitch.classList.remove('active');
  ytPanel.classList.remove('hidden'); twitchPanel.classList.add('hidden');
};
tabTwitch.onclick = () => {
  tabTwitch.classList.add('active'); tabYt.classList.remove('active');
  twitchPanel.classList.remove('hidden'); ytPanel.classList.add('hidden');
};

// ── Elementy UI ─────────────────────────────────────────────────────────────
const btnProbe = document.getElementById('btn-probe');
const spinner = document.getElementById('spinner');
const ytUrl = document.getElementById('yt-url');
const destPath = document.getElementById('dest-path');

const videoInfo = document.getElementById('video-info');
const thumb = document.getElementById('thumb');
const titleEl = document.getElementById('title');
const uploaderEl = document.getElementById('uploader');
const durationEl = document.getElementById('duration');

const filters = document.getElementById('filters');
const filterExt = document.getElementById('filter-ext');
const filterFps = document.getElementById('filter-fps');
const formatsTable = document.getElementById('formats-table');
const formatsBody = document.getElementById('formats-body');

const progressArea = document.getElementById('progress-area');
const progressFill = document.getElementById('progress-fill');
const progressStats = document.getElementById('progress-stats');
const btnPause = document.getElementById('btn-pause');
const btnResume = document.getElementById('btn-resume');
const btnCancel = document.getElementById('btn-cancel');
const btnOpen = document.getElementById('btn-open');
const dlSpinner = document.getElementById('dl-spinner');

// Explorer modal
const btnExplorer = document.getElementById('btn-explorer');
const explorerModal = document.getElementById('explorer-modal');
const explorerClose = document.getElementById('explorer-close');
const explorerList = document.getElementById('explorer-list');
const explorerPath = document.getElementById('explorer-path');
const explorerUse = document.getElementById('explorer-use');

let explorerCwd = "/";
let explorerSelectedDir = null;

btnExplorer.onclick = async () => {
  explorerModal.classList.remove('hidden');
  await loadExplorer(null);
};
explorerClose.onclick = () => explorerModal.classList.add('hidden');
explorerUse.onclick = () => {
  if (explorerSelectedDir) {
    destPath.value = explorerSelectedDir;
  } else {
    destPath.value = explorerCwd;
  }
  explorerModal.classList.add('hidden');
};

async function loadExplorer(path){
  const q = path ? `?path=${encodeURIComponent(path)}` : "";
  const resp = await fetch(`/api/list_dir${q}`);
  if(!resp.ok){
    alert("Nie udało się otworzyć eksploratora.");
    return;
  }
  const data = await resp.json();
  explorerCwd = data.cwd;
  explorerPath.textContent = explorerCwd;
  explorerList.innerHTML = '';
  data.entries.forEach(e=>{
    const div = document.createElement('div');
    div.className = 'entry ' + (e.is_dir ? 'is-dir' : '');
    div.innerHTML = `
      <div>
        <div class="name">${e.name}</div>
        <div class="path">${e.path}</div>
      </div>
      <div>${e.is_dir ? '📁' : '📄'}</div>
    `;
    div.onclick = async () => {
      if (e.is_dir) {
        explorerSelectedDir = e.path;
        await loadExplorer(e.path);
      }
    };
    explorerList.appendChild(div);
  });
}

// ── Stan ────────────────────────────────────────────────────────────────────
let currentFormats = [];
let rawFormats = [];
let currentJobId = null;
let ws = null;
let lastUiUpdate = 0;
let lastDestPath = null;

// ── Akcje ───────────────────────────────────────────────────────────────────
btnProbe.onclick = async () => {
  const url = ytUrl.value.trim();
  if (!url) { alert("Wklej URL"); return; }
  spinner.classList.remove('hidden');
  formatsTable.classList.add('hidden');
  filters.classList.add('hidden');
  videoInfo.classList.add('hidden');
  progressArea.classList.add('hidden');

  try{
    const resp = await fetch(`/api/probe?url=${encodeURIComponent(url)}`);
    if (!resp.ok) {
      alert("Błąd: " + (await resp.text()));
      spinner.classList.add('hidden');
      return;
    }
    const data = await resp.json();
    // Info o wideo
    videoInfo.classList.remove('hidden');
    thumb.src = data.thumbnail || '';
    titleEl.textContent = data.title || '';
    uploaderEl.textContent = data.uploader || '';
    durationEl.textContent = data.duration || '';
    // Formaty
    currentFormats = data.table_formats || [];
    rawFormats = data.all_formats_raw || [];
    buildFilters();
    renderFormats(currentFormats);
    formatsTable.classList.remove('hidden');
    filters.classList.remove('hidden');
  }catch(e){
    console.error(e);
    alert("Nie udało się pobrać formatów.");
  }finally{
    spinner.classList.add('hidden');
  }
};

function buildFilters(){
  const exts = new Set();
  const fpss = new Set();
  currentFormats.forEach(f => {
    if (f.ext) exts.add(f.ext);
    if (f.fps) fpss.add(f.fps);
  });
  filterExt.innerHTML = '<option value="">Wszystkie</option>' + Array.from(exts).map(e=>`<option value="${e}">${e}</option>`).join('');
  filterFps.innerHTML = '<option value="">Wszystkie</option>' + Array.from(fpss).sort((a,b)=>a-b).map(e=>`<option value="${e}">${e}fps</option>`).join('');
}
filterExt.onchange = applyFilters;
filterFps.onchange = applyFilters;

function applyFilters(){
  const ext = filterExt.value;
  const fps = filterFps.value;
  let arr = currentFormats;
  if (ext) arr = arr.filter(f => f.ext === ext);
  if (fps) arr = arr.filter(f => String(f.fps) === String(fps));
  renderFormats(arr);
}

function humanSize(bytes){
  if (!bytes) return "-";
  const units = ['B','KB','MB','GB'];
  let i=0; let n=bytes;
  while (n>=1000 && i<units.length-1){ n/=1000; i++; }
  return n.toFixed(2)+' '+units[i];
}
function formatSeconds(s){
  if (s == null) return '-';
  const n = Math.max(0, Math.round(Number(s)));
  const h = Math.floor(n/3600);
  const m = Math.floor((n%3600)/60);
  const sec = n%60;
  const hh = String(h).padStart(2,'0');
  const mm = String(m).padStart(2,'0');
  const ss = String(sec).padStart(2,'0');
  return `${hh}:${mm}:${ss}`;
}
function formatSpeed(bps){
  if (!bps) return '-';
  const kbps = bps/1000;
  if (kbps >= 1000) return (kbps/1000).toFixed(2) + ' MB/s';
  return Math.round(kbps) + ' KB/s';
}

function renderFormats(list){
  formatsBody.innerHTML = '';
  list.forEach(f=>{
    const tr = document.createElement('tr');
    const res = f.resolution || '-';
    const kbps = f.kbps || '-';
    const size = humanSize(f.filesize);
    tr.innerHTML = `
      <td>${f.ext || '-'}</td>
      <td>${res}</td>
      <td>${kbps}</td>
      <td>${size}</td>
      <td><button class="dl">Pobierz</button></td>
    `;
    const btn = tr.querySelector('button.dl');
    btn.onclick = () => startDownload(f.format_id, f.ext);
    formatsBody.appendChild(tr);
  });
}

// ── Start / Progress / Sterowanie ───────────────────────────────────────────
async function startDownload(format_id, prefer_ext){
  const url = ytUrl.value.trim();
  const dest = destPath.value.trim();
  if (!url){ alert("Brak URL"); return; }
  if (!dest){ alert("Wybierz folder docelowy"); return; }

  // pokaż panel progresu natychmiast z kółkiem ładowania
  progressArea.classList.remove('hidden');
  formatsTable.classList.add('hidden');
  filters.classList.add('hidden');
  progressFill.style.width = '0%';
  progressStats.textContent = 'Przygotowywanie…';
  dlSpinner.classList.remove('hidden');

  const payload = {
    url,
    format_id,
    prefer_ext,
    dest_path: dest
  };
  const resp = await fetch('/api/start_download', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
  if(!resp.ok){
    dlSpinner.classList.add('hidden');
    alert("Błąd: " + await resp.text());
    return;
  }
  const data = await resp.json();
  currentJobId = data.job_id;

  openWS(currentJobId);
}

function openWS(jobId){
  if (ws) try{ ws.close(); }catch(e){}
  lastUiUpdate = 0;
  ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + `/ws/${jobId}`);
  ws.onmessage = (ev) => {
    const d = JSON.parse(ev.data || "{}");
    if (d.error){ return; }

    // odświeżaj co 1s (throttle)
    const now = Date.now();
    if (now - lastUiUpdate < 1000) return;
    lastUiUpdate = now;

    const pct = Math.max(0, Math.min(100, Math.round(d.progress || 0)));
    progressFill.style.width = pct + '%';
    const speed = formatSpeed(d.speed);
    const eta = formatSeconds(d.eta);
    const down = humanSize(d.downloaded_bytes) || '-';
    const total = humanSize(d.total_bytes) || '-';
    const status = d.status || '-';
    const msg = d.message || '';

    // schowaj spinner gdy faktycznie pobiera
    if (status !== 'starting' && status !== 'queued' && status !== 'probing') {
      dlSpinner.classList.add('hidden');
    }

    progressStats.textContent =
      `Status: ${status} • ${pct}% • ${down} / ${total} • prędkość: ${speed} • ETA: ${eta}${msg ? ' • ' + msg : ''}`;

    if (status === 'paused'){
      btnPause.classList.add('hidden');
      btnResume.classList.remove('hidden');
    } else if (status === 'downloading'){
      btnPause.classList.remove('hidden');
      btnResume.classList.add('hidden');
    }

    if (status === 'canceled') {
      // po anulowaniu — usuń progres i pokaż listę formatów
      try{ ws.close(); }catch(e){}
      currentJobId = null;
      progressArea.classList.add('hidden');
      btnResume.classList.add('hidden');
      btnPause.classList.remove('hidden'); // przywróć domyślnie
      btnCancel.classList.remove('hidden');
      btnOpen.classList.add('hidden');
      formatsTable.classList.remove('hidden');
      filters.classList.remove('hidden');
      return;
    }

    if (status === 'done'){
      try{ ws.close(); }catch(e){}
      // ukryj sterowanie, pokaż "Otwórz folder"
      btnPause.classList.add('hidden');
      btnResume.classList.add('hidden');
      btnCancel.classList.add('hidden');
      btnOpen.classList.remove('hidden');
      lastDestPath = d.dest_path || lastDestPath;
    }

    if (status === 'error'){
      try{ ws.close(); }catch(e){}
    }
  };
  ws.onclose = () => {};
}

// Pauza
btnPause.onclick = async () => {
  if (!currentJobId) return;
  if (!confirm("Wstrzymać pobieranie?")){
    return;
  }
  await fetch('/api/pause', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({job_id: currentJobId})});
};

// Wznowienie
btnResume.onclick = async () => {
  if (!currentJobId) return;
  if (!confirm("Wznowić pobieranie?")){
    return;
  }
  await fetch('/api/resume', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({job_id: currentJobId})});
};

// Anuluj
btnCancel.onclick = async () => {
  if (!currentJobId) return;
  if (!confirm("Anulować pobieranie? Tego nie można cofnąć.")){
    return;
  }
  await fetch('/api/cancel', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({job_id: currentJobId})});
};

// Otwórz folder docelowy (wbudowany eksplorator)
btnOpen.onclick = async () => {
  const path = lastDestPath || destPath.value.trim();
  if (!path) return;
  explorerModal.classList.remove('hidden');
  await loadExplorer(path);
};