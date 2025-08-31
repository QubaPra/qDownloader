const $ = (sel, ctx=document) => ctx.querySelector(sel);
const $$ = (sel, ctx=document) => Array.from(ctx.querySelectorAll(sel));

const spinner = $('#spinner');
const urlInput = $('#urlInput');
const pathInput = $('#pathInput');
const queue = $('#queue');
// Twitch refs
const twPathInput = $('#twPathInput');
const twExtSel = $('#twExt');
const twUrlInput = $('#twUrl');
const twCheckBtn = $('#twCheck');
const twResults = $('#twResults');

function showSpinner(v){ spinner.classList.toggle('hidden', !v); }

function fmtHMS(sec){
  const s = Math.max(0, Math.floor(sec));
  const h = Math.floor(s/3600);
  const m = Math.floor((s%3600)/60);
  const r = s%60;
  return [h,m,r].map((v,i)=> i===0? String(v).padStart(2,'0'):String(v).padStart(2,'0')).join(':');
}

// Szacowanie rozmiaru po stronie klienta (spójne z backendem)
const TW_KBPS_MAP = {
  '1080p60': 8000,
  '1080p30': 5000,
  '720p60': 4500,
  '720p30': 3000,
  '480p30': 1500,
  '360p30': 800,
  '160p30': 250,
  // historycznie 'chunked' ~1080p60
  'chunked': 8000,
};

function humanSize(bytes){
  let size = Number(bytes)||0;
  const units = ['B','KB','MB','GB','TB'];
  let i = 0;
  while(size >= 1024 && i < units.length-1){
    size /= 1024;
    i++;
  }
  return `${size.toFixed(2)} ${units[i]}`;
}

function estimateSizeFromLabel(label, durationSec){
  const kbps = TW_KBPS_MAP[label];
  if(!kbps || !durationSec || durationSec <= 0) return '-';
  const bytes = durationSec * (kbps * 1000) / 8;
  return humanSize(bytes);
}

function createItemCard(meta){
  const el = document.createElement('div');
  el.className = 'card';
  el.innerHTML = `
    <div class="item">
      <div class="thumb">${meta.thumbnail? `<img src="${meta.thumbnail}" alt="thumb"/>` : ''}</div>
      <div class="meta">
        <div class="title">${meta.title || ''}</div>
        <div class="row">
          <div class="channel">${meta.channel || ''}</div>
          <div class="duration">${fmtHMS(meta.duration || 0)}</div>
        </div>
      </div>
      <button class="close hidden" title="Anuluj">✕</button>
  </div>
  <div class="formats"></div>
  `;
  return el;
}

function renderFormats(el, formats){
  if(!formats || !formats.length){
    el.innerHTML = '<div class="hint">Brak formatów video-only.</div>';
    return;
  }
  const table = document.createElement('table');
  table.className = 'format-table';
  table.innerHTML = `
    <thead>
      <tr>
        <th>ext</th>
        <th>res</th>
        <th>bitrate</th>
        <th>rozmiar</th>
        <th></th>
      </tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = $('tbody', table);
  for(const f of formats){
    const tr = document.createElement('tr');
    tr.className = 'format-row';
    tr.dataset.format = f.id || '';
    tr.dataset.res = f.res || '';
    tr.dataset.ext = f.ext || '';
    tr.innerHTML = `
      <td>${f.ext||'-'}</td>
      <td>${f.res||'-'}</td>
      <td>${f.bitrate||'-'}</td>
      <td>${f.size_est||'-'}</td>
      <td></td>
    `;
    tbody.appendChild(tr);
  }
  el.innerHTML = '';
  el.appendChild(table);
}

function renderProgress(el, infoText){
  const wrap = document.createElement('div');
  wrap.className = 'progress-card';
  wrap.innerHTML = `
    <div class="progress-bar">
      <div class="progress-fill"></div>
      <div class="progress-left">0B</div>
      <div class="progress-label">0%</div>
      <div class="progress-right">?</div>
    </div>
  <div class="progress-meta"><span class="left">Pozostało: --:--:--</span><span class="center fmt">${infoText || ''}</span><span class="right">0 B/s</span></div>
  `;
  el.appendChild(wrap);
  return wrap;
}

function startSSE(jobId, wrap){
  const es = new EventSource(`/api/progress/${jobId}`);
  const fill = $('.progress-fill', wrap);
  const label = $('.progress-label', wrap);
  const left = $('.left', wrap);
  const right = $('.right', wrap);
  const inLeft = $('.progress-left', wrap);
  const inRight = $('.progress-right', wrap);
  let done = false;
  // Przechowuj ostatnie znane wartości
  let lastEta = null;
  let lastSpeed = null;

  es.onmessage = (ev)=>{
    const msg = JSON.parse(ev.data);
    if(msg.type === 'progress'){
  // jeśli wcześniej było "W kolejce…", usuń ten stan
  wrap.classList.remove('progress-queued');
  wrap.classList.remove('progress-retrying');
      const p = Math.max(0, Math.min(100, msg.percent||0));
      fill.style.width = `${p}%`;
      label.textContent = `${p.toFixed(1)}%`;
      // ETA: użyj ostatniej znanej gdy przychodzi 'Unknown'
      let etaRaw = msg.eta || '';
      const isEtaUnknown = typeof etaRaw === 'string' && /unknown/i.test(etaRaw);
      if(!isEtaUnknown && etaRaw){
        lastEta = etaRaw;
      }
      const etaToShow = lastEta || '--:--:--';
      left.textContent = `Pozostało: ${etaToShow}`;
      // Speed: użyj ostatniej znanej gdy przychodzi 'Unknown' (np. 'Unknown B/s')
      let speedRaw = msg.speed || '';
      const isSpeedUnknown = typeof speedRaw === 'string' && /unknown/i.test(speedRaw);
      if(!isSpeedUnknown && speedRaw){
        lastSpeed = speedRaw;
      }
  const speedToShow = lastSpeed || '0 B/s';
  inLeft.textContent = `${msg.downloaded || '0B'}`;
  inRight.textContent = `${msg.size || '?'}`;
  right.textContent = speedToShow;
    } else if(msg.type === 'queued'){
      // zadanie czeka w kolejce na start pobierania
      wrap.classList.add('progress-queued');
      label.textContent = 'W kolejce…';
    } else if(msg.type === 'retrying'){
      // połączenie zrywa/ponawianie – oznacz żółtym paskiem
      wrap.classList.add('progress-retrying');
      label.textContent = 'Ponawianie…';
    } else if(msg.type === 'resumed'){
      // wznawianie – wróć do normalnego paska
      wrap.classList.remove('progress-retrying');
    } else if(msg.type === 'done'){
      wrap.classList.add('progress-success');
      fill.style.width = '100%';
      label.textContent = 'Pobrano';
      done = true;
      es.close();
    } else if(msg.type === 'cancelled'){
      wrap.classList.add('progress-cancelled');
      label.textContent = 'Anulowano';
      es.close();
      // Usuń tylko tę jedną kartę, nie cały kontener kolejki
      setTimeout(()=> wrap.closest('.card')?.remove(), 2000);
    } else if(msg.type === 'error'){
      wrap.classList.add('progress-cancelled');
      label.textContent = 'Błąd';
      es.close();
    }
  };

  // Obsługa przycisku anulowania w nagłówku karty (widoczny po starcie pobierania)
  const cardEl = wrap.closest('.card');
  const closeBtn = $('.close', cardEl);
  closeBtn?.addEventListener('click', async ()=>{
    const isQueued = wrap.classList.contains('progress-queued');
    if(!done && !isQueued){
      const ok = confirm('Anulować pobieranie?');
      if(!ok) return;
    }
    await fetch(`/api/cancel/${jobId}`, { method: 'DELETE' });
    if(done || isQueued){
      // Usuń tylko bieżącą kartę natychmiast, jeśli była w kolejce
      cardEl?.remove();
    }
  });
}

async function fetchFormats(url){
  showSpinner(true);
  try{
    const r = await fetch('/api/yt/formats', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ url }) });
    if(!r.ok) throw new Error('Błąd pobierania formatów');
    const data = await r.json();

    const card = createItemCard(data);
    const formatsEl = $('.formats', card);
    renderFormats(formatsEl, data.formats);

    queue.prepend(card);

    // Clicks: kliknięcie w cały wiersz formatu
    $$('.format-row', card).forEach(tr=>{
      tr.addEventListener('click', async ()=>{
        const fmt = tr.dataset.format;
        const res = tr.dataset.res || '';
        const ext = tr.dataset.ext || '';
        const download_dir = pathInput.value.trim() || null;
        // Pokaż przycisk anulowania w nagłówku karty
        const closeBtn = $('.close', card);
        closeBtn?.classList.remove('hidden');
        // Zastąp tabelę formatek paskiem postępu w tym samym miejscu
        formatsEl.innerHTML = '';
        const info = `${res}${res && ext ? ' ' : ''}${ext ? '.'+ext : ''}`.trim();
        const progress = renderProgress(formatsEl, info);
        const r2 = await fetch('/api/yt/download', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ url, format_id: fmt, download_dir }) });
        if(!r2.ok){
          $('.progress-label', progress).textContent = 'Błąd';
          return;
        }
        const { job_id } = await r2.json();
        startSSE(job_id, progress);
      });
    });
  } finally { showSpinner(false); }
}

urlInput.addEventListener('input', (e)=>{
  const v = (e.target.value||'').trim();
  if(v.length > 10 && v.includes('http')){
    fetchFormats(v).catch(console.error);
    e.target.value = '';
  }
});

// ===== Tabs =====
$$('.tab').forEach(btn=>{
  btn.addEventListener('click', ()=>{
    $$('.tab').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    const tab = btn.dataset.tab;
    $$('.tab-panel').forEach(p=>p.classList.remove('active'));
    $(`#tab-${tab}`)?.classList.add('active');
  });
});

// ===== Twitch =====
function renderTwQualities(card, qualities, baseMeta, params){
  const formatsEl = $('.formats', card);
  if(!qualities || !qualities.length){
    formatsEl.innerHTML = '<div class="hint">Brak dostępnych jakości.</div>';
    return;
  }
  const table = document.createElement('table');
  table.className = 'format-table tw-format-table';
  table.innerHTML = `
    <thead>
      <tr>
        <th>res</th>
        <th>rozmiar</th>
        <th></th>
      </tr>
    </thead>
    <tbody></tbody>
  `;
  const tbody = $('tbody', table);
  for(const q of qualities){
    const tr = document.createElement('tr');
    tr.className = 'format-row';
    tr.dataset.m3u8 = q.m3u8;
    tr.dataset.label = q.label;
    tr.innerHTML = `
      <td>${q.label}</td>
      <td>${q.size_est || '-'}</td>
      <td></td>
    `;
    tr.addEventListener('click', async ()=>{
      const download_dir = twPathInput?.value?.trim() || null;
      const ext = twExtSel?.value || 'mp4';
      const slider = $('.range-dual', card);
      const startInput = $('input[data-role="start"]', slider);
      const endInput = $('input[data-role="end"]', slider);
      let start_sec = Number(startInput?.value||0) || 0;
      let end_v = Number(endInput?.value||0) || 0;
      const max = Number(endInput?.max||0) || Number(startInput?.max||0) || 0;
      // prawa gałka na końcu = do końca
      const end_sec = (end_v >= max) ? null : end_v;
      // show close button
      const closeBtn = $('.close', card);
      closeBtn?.classList.remove('hidden');
      // replace table with progress
      formatsEl.innerHTML = '';
      const info = `${q.label} .${ext}`;
      const progress = renderProgress(formatsEl, info);
      const r = await fetch('/api/twitch/download', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ m3u8_url: q.m3u8, download_dir, ext, start_sec, end_sec }) });
      if(!r.ok){
        $('.progress-label', progress).textContent = 'Błąd';
        return;
      }
      const { job_id } = await r.json();
      startSSE(job_id, progress);
    });
    tbody.appendChild(tr);
  }
  formatsEl.innerHTML = '';
  formatsEl.appendChild(table);
}

async function twitchResolve(url){
  showSpinner(true);
  try{
    const r = await fetch('/api/twitch/resolve', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ url }) });
    if(!r.ok) throw new Error('Błąd rozwiązywania Twitch');
    const data = await r.json();

    const card = createItemCard({
      title: data.title,
      channel: data.channel,
      duration: data.duration,
      thumbnail: data.thumbnail,
    });
    twResults.prepend(card);

    // render dual-handle slider nad tabelą jakości
    const dur = Number(data.duration||0);
    const formatsEl = $('.formats', card);
    const sliderWrap = document.createElement('div');
    sliderWrap.className = 'range-wrap';
    sliderWrap.innerHTML = `
      <div class="range-row">
        <div class="range-dual">
          <div class="range-track"></div>
          <div class="range-fill"></div>
            <input type="range" data-role="start" min="0" max="${dur}" step="1" value="${Math.min(900, dur>900?900:0)}" />
            <input type="range" data-role="end" min="0" max="${dur}" step="1" value="${dur}" />
        </div>
      </div>
      <div class="range-labels"><span class="start-label">00:15:00</span><span class="mid-label">00:00:00</span><span class="end-label">${fmtHMS(dur)}</span></div>
    `;
    formatsEl.before(sliderWrap);

    const startInput = $('input[data-role="start"]', sliderWrap);
    const endInput = $('input[data-role="end"]', sliderWrap);
  const startLabel = $('.start-label', sliderWrap);
  const midLabel = $('.mid-label', sliderWrap);
  const endLabel = $('.end-label', sliderWrap);
  const fill = $('.range-fill', sliderWrap);

    function syncDual(){
      let s = Number(startInput.value||0) || 0;
      let e = Number(endInput.value||0) || 0;
      const max = dur || 0;
      const isToEnd = e >= max; // prawa gałka na końcu = do końca
      // jeśli end (nie-na-końcu) jest < start – dosuń
      if(!isToEnd && e < s){
        e = Math.min(max, s+1);
        endInput.value = String(e);
      }
      startLabel.textContent = fmtHMS(s);
      midLabel.textContent = isToEnd ? fmtHMS(Math.max(0, max-s)) : fmtHMS(Math.max(0, e-s));
  endLabel.textContent = fmtHMS(isToEnd ? max : e);
      // wypełnienie – oblicz w %
      const min = 0, m = max || 1;
      const startPct = (s - min) / (m - min) * 100;
      const endPct = (isToEnd ? max : e) / (m - min) * 100;
      const left = Math.max(0, Math.min(100, startPct));
      const right = Math.max(0, Math.min(100, endPct));
      fill.style.left = `${left}%`;
      fill.style.right = `${100-right}%`;
    }
    // Ustaw domyślny start 15:00, jeśli VOD dłuższy; inaczej 0
    if(dur > 900){ startInput.value = '900'; } else { startInput.value = '0'; }
    endInput.value = String(dur);
    syncDual();
    // Aktualizacja rozmiarów w tabeli jakości na zmianę zakresu
    function selectionDuration(){
      const s = Number(startInput.value||0) || 0;
      const e = Number(endInput.value||0) || 0;
      const isToEnd = e >= dur;
      const endAbs = isToEnd ? dur : e;
      return Math.max(0, endAbs - s);
    }
    function updateTwTableSizes(){
      const selDur = selectionDuration();
      $$('.format-row', card).forEach(tr=>{
        const lbl = tr.dataset.label || '';
        const sizeCell = tr.children && tr.children[1]; // kolumna "rozmiar"
        if(sizeCell){
          sizeCell.textContent = estimateSizeFromLabel(lbl, selDur);
        }
      });
    }
    startInput.addEventListener('input', ()=>{ syncDual(); updateTwTableSizes(); });
    endInput.addEventListener('input', ()=>{ syncDual(); updateTwTableSizes(); });

    renderTwQualities(card, data.qualities||[], data, {});
    // Pierwsze przeliczenie rozmiarów dla domyślnego zakresu
    updateTwTableSizes();
  } finally{ showSpinner(false); }
}

twCheckBtn?.addEventListener('click', ()=>{
  const url = (twUrlInput?.value||'').trim();
  if(url.length>10 && url.includes('http')){
    twitchResolve(url).catch(console.error);
  }
});

