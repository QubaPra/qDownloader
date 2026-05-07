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
      <div class="thumb">${meta.thumbnail? `<img src="${meta.thumbnail}" alt="thumb" onerror="this.style.display='none';this.parentElement.style.display='none';"/>` : ''}</div>
      <div class="meta">
        <div class="title">${meta.title || ''}</div>
        <div class="row">
          <div class="channel">${meta.channel || ''}</div>
          <div class="duration">${fmtHMS(meta.duration || 0)}</div>
        </div>
      </div>
      <button class="close" title="Close"><svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg></button>
  </div>
  <div class="formats"></div>
  `;

  const closeBtn = el.querySelector('.close');
  closeBtn.onclick = async () => {
    const jobId = el.dataset.job;
    const isDownloading = el.classList.contains('is-downloading');
    if (isDownloading) {
      const ok = confirm('Cancel download?');
      if (!ok) return;
    }
    if (jobId) {
      try { await fetch(`/api/cancel/${jobId}`, { method: 'DELETE' }); } catch (e) { console.error(e); }
    }
    const u = el.dataset.url;
    if (u && typeof UIState !== 'undefined') UIState.remove(u);
    el.remove();
  };

  return el;
}

function renderFormats(el, formats){
  if(!formats || !formats.length){
    el.innerHTML = '<div class="hint">No video-only formats available.</div>';
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
        <th>size</th>
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
  <div class="progress-meta"><span class="left">ETA: --:--:--</span><span class="center fmt">${infoText || ''}</span><span class="right">0 B/s</span></div>
  `;
  el.appendChild(wrap);
  return wrap;
}

function startSSE(jobId, wrap, retryCb = null){
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
      left.textContent = `ETA: ${etaToShow}`;
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
      label.textContent = 'Queued...';
    } else if(msg.type === 'retrying'){
      // połączenie zrywa/ponawianie – oznacz żółtym paskiem
      wrap.classList.add('progress-retrying');
      label.textContent = 'Retrying...';
    } else if(msg.type === 'resumed'){
      // wznawianie – wróć do normalnego paska
      wrap.classList.remove('progress-retrying');
    } else if(msg.type === 'done'){
      wrap.classList.add('progress-success');
      fill.style.width = '100%';
      label.textContent = 'Done';
      left.textContent = 'ETA: --:--:--';
      right.textContent = '0 B/s';
      inLeft.textContent = inRight.textContent;
      done = true;
      es.close();
      const cardEl = wrap.closest('.card');
      if(cardEl) cardEl.classList.remove('is-downloading');
    } else if(msg.type === 'cancelled'){
      wrap.classList.add('progress-cancelled');
      label.textContent = 'Cancelled';
      es.close();
      const cardEl = wrap.closest('.card');
      if(cardEl) cardEl.classList.remove('is-downloading');
      // Usuń tylko tę jedną kartę, nie cały kontener kolejki
      setTimeout(()=> wrap.closest('.card')?.remove(), 2000);
    } else if(msg.type === 'error'){
      wrap.classList.add('progress-cancelled');
      label.textContent = 'Error';
      es.close();
      const cardEl = wrap.closest('.card');
      if(cardEl) cardEl.classList.remove('is-downloading');
      done = true; // Fix removal bug

      if(retryCb){
        const actionsDiv = document.createElement('div');
        actionsDiv.className = 'retry-actions';

        const retryBtn = document.createElement('button');
        retryBtn.className = 'retry-btn';
        retryBtn.textContent = 'Try again';
        retryBtn.onclick = () => {
          actionsDiv.remove();
          wrap.classList.remove('progress-cancelled');
          label.textContent = '0%';
          fill.style.width = '0%';
          cardEl.classList.add('is-downloading');
          done = false;
          retryCb();
        };
        actionsDiv.appendChild(retryBtn);

        // Resume Button
        if (msg.last_time && msg.last_time > 15) {
          const resumeBtn = document.createElement('button');
          resumeBtn.className = 'retry-btn primary';
          resumeBtn.textContent = 'Resume download';
          resumeBtn.onclick = () => {
            actionsDiv.remove();
            wrap.classList.remove('progress-cancelled');
            label.textContent = '0%';
            fill.style.width = '0%';
            cardEl.classList.add('is-downloading');
            done = false;
            // Send the last_time so the caller knows how much to offset
            retryCb(msg.last_time);
          };
          actionsDiv.appendChild(resumeBtn);
        }
        wrap.appendChild(actionsDiv);
      }
    }
  };

  // Handle cancel button in the card header
  const cardEl = wrap.closest('.card');
  if (!cardEl) {
    console.error('[startSSE] Could not find card element from progress wrap');
    return;
  }
  const closeBtn = $('.close', cardEl);
  if (closeBtn) {
    closeBtn.onclick = async ()=>{
      const isQueued = wrap.classList.contains('progress-queued');
      if(!done && !isQueued){
        const ok = confirm('Cancel download?');
        if(!ok) return;
      }
      try{
        console.log(`[startSSE] Requesting cancel/remove for job ${jobId}`);
        await fetch(`/api/cancel/${jobId}`, { method: 'DELETE' });
      }catch(e){
        console.error('[startSSE] cancel request failed', e);
      }
      // Remove card and clear saved UI state for this URL
      try{
        const u = cardEl?.dataset?.url;
        if(u && typeof UIState !== 'undefined') UIState.remove(u);
      }catch(e){}
      cardEl?.remove();
    };
  } else {
    console.error('[startSSE] Could not find close button in card');
  }
}

async function fetchFormats(url, cachedData = null){
  showSpinner(true);
  try{
    let data = cachedData;
    if (!data) {
      const r = await fetch('/api/yt/formats', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ url }) });
      if(!r.ok) throw new Error('Błąd pobierania formatów');
      data = await r.json();
    }

    const card = createItemCard(data);
    card.dataset.url = url;
    const formatsEl = $('.formats', card);
    renderFormats(formatsEl, data.formats);

    queue.prepend(card);
    if (typeof UIState !== 'undefined') UIState.add(url, 'youtube', data);

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
        card.classList.add('is-downloading');
        const info = `${res}${res && ext ? ' ' : ''}${ext ? '.'+ext : ''}`.trim();
        const progress = renderProgress(formatsEl, info);
        const doDownload = async () => {
          const r2 = await fetch('/api/yt/download', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ url, format_id: fmt, download_dir, meta: data, info_text: info }) });
          if(!r2.ok){
            $('.progress-label', progress).textContent = 'Błąd';
            return;
          }
          const { job_id } = await r2.json();
          startSSE(job_id, progress, doDownload);
        };
        doDownload();
      });
    });
  } finally { showSpinner(false); }
}

urlInput.addEventListener('input', (e)=>{
  const v = (e.target.value||'').trim();
  if(v.length > 10 && v.includes('http')){
    const existing = $(`.card[data-url="${v}"]`);
    if(existing) {
      if(existing.classList.contains('is-downloading')) {
        e.target.value = '';
        return; // skip
      } else {
        existing.remove();
      }
    }
    fetchFormats(v).catch(console.error);
    e.target.value = '';
  }
});

// ===== Settings =====
const parallelToggle = $('#parallelToggle');
if(parallelToggle) {
  parallelToggle.addEventListener('change', async (e) => {
    await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ parallel: e.target.checked })
    });
  });
}

// ===== Tabs =====
$$('.tab').forEach(btn=>{
  btn.addEventListener('click', ()=>{
    $$('.tab').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    const tab = btn.dataset.tab;
    $$('.tab-panel').forEach(p=>p.classList.remove('active'));
    $(`#tab-${tab}`)?.classList.add('active');

    if (tab === 'twitch') document.body.classList.add('twitch-active');
    else document.body.classList.remove('twitch-active');

    localStorage.setItem('activeTab', tab);
  });
});

// ===== Twitch =====
function renderTwQualities(card, qualities, meta, original_url){
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
        <th>Res</th>
        <th>Size</th>
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
      const ext = twExtSel?.value || 'ts';
      const slider = $('.range-dual', card);
      const startInput = $('input[data-role="start"]', slider);
      const endInput = $('input[data-role="end"]', slider);
      let start_sec = Number(startInput?.value||0) || 0;
      let end_v = Number(endInput?.value||0) || 0;
      const max = Number(endInput?.max||0) || Number(startInput?.max||0) || 0;
      // prawa gałka na końcu = do końca
      const end_sec = (end_v >= max) ? null : end_v;
      // Pobierz title i release_date z dataset karty
      const title = card.dataset.twitchTitle || null;
      const release_date = card.dataset.twitchReleaseDate || null;
      // show close button
      const closeBtn = $('.close', card);
      closeBtn?.classList.remove('hidden');

      const sliderWrap = $('.range-wrap', card);
      if(sliderWrap) sliderWrap.style.display = 'none';

      // replace table with progress
      formatsEl.innerHTML = '';
      card.classList.add('is-downloading');
      const info = `${q.label} .${ext}`;
      const progress = renderProgress(formatsEl, info);
      const doDownload = async (resume_offset = 0) => {
        let req_start_sec = start_sec;
        if(resume_offset > 15) {
          req_start_sec = Math.max(0, start_sec + resume_offset - 15);
        }
        const r = await fetch('/api/twitch/download', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ m3u8_url: q.m3u8, download_dir, ext, start_sec: req_start_sec, end_sec, title, release_date, meta: meta, original_url: original_url, info_text: info }) });
        if(!r.ok){
          $('.progress-label', progress).textContent = 'Błąd';
          return;
        }
        const { job_id } = await r.json();
        startSSE(job_id, progress, doDownload);
      };
      doDownload();
    });
    tbody.appendChild(tr);
  }
  formatsEl.innerHTML = '';
  formatsEl.appendChild(table);
}

async function twitchResolve(url, cachedData = null){
  showSpinner(true);
  try{
    let data = cachedData;
    if (!data) {
      const r = await fetch('/api/twitch/resolve', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ url }) });
      if(!r.ok) throw new Error('Błąd rozwiązywania Twitch');
      data = await r.json();
    }

    const card = createItemCard({
      title: data.title,
      channel: data.channel,
      duration: data.duration,
      thumbnail: data.thumbnail,
    });
    // Przechowaj title i release_date jako dataset do późniejszego użycia
    card.dataset.twitchTitle = data.title || '';
    card.dataset.twitchReleaseDate = data.release_date || '';
    card.dataset.url = url;
    twResults.prepend(card);
    if (typeof UIState !== 'undefined') UIState.add(url, 'twitch', data);

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
            <input type="range" data-role="start" min="0" max="${dur}" step="0.1" value="${Math.min(960, dur>960?960:0)}" />
            <input type="range" data-role="end" min="0" max="${dur}" step="0.1" value="${dur}" />
        </div>
      </div>
      <div class="range-labels"><span class="start-label">00:16:00</span><span class="mid-label">00:00:00</span><span class="end-label">${fmtHMS(dur)}</span></div>
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
    // Ustaw domyślny start 16:00, jeśli VOD dłuższy; inaczej 0
    if(dur > 960){ startInput.value = '960'; } else { startInput.value = '0'; }
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

    // Obsługa klawiszy strzałek dla precyzyjnej regulacji (shift = 10x szybciej)
    startInput.addEventListener('keydown', (e)=>{
      if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)){
        const step = e.shiftKey ? 10 : 1;
        const cur = Number(startInput.value) || 0;
        if(e.key === 'ArrowLeft' || e.key === 'ArrowDown'){
          startInput.value = Math.max(0, cur - step);
        } else {
          startInput.value = Math.min(dur, cur + step);
        }
        syncDual();
        updateTwTableSizes();
        e.preventDefault();
      }
    });
    endInput.addEventListener('keydown', (e)=>{
      if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)){
        const step = e.shiftKey ? 10 : 1;
        const cur = Number(endInput.value) || 0;
        if(e.key === 'ArrowLeft' || e.key === 'ArrowDown'){
          endInput.value = Math.max(0, cur - step);
        } else {
          endInput.value = Math.min(dur, cur + step);
        }
        syncDual();
        updateTwTableSizes();
        e.preventDefault();
      }
    });

    renderTwQualities(card, data.qualities||[], data, url);
    // Pierwsze przeliczenie rozmiarów dla domyślnego zakresu
    updateTwTableSizes();
  } finally{ showSpinner(false); }
}

twCheckBtn?.addEventListener('click', ()=>{
  const url = (twUrlInput?.value||'').trim();
  if(url.length>10 && url.includes('http')){
    // Sprawdź czy już istnieje karta
    const existing = $(`.card[data-url="${url}"]`);
    if(existing) {
      if(existing.classList.contains('is-downloading')) {
        return; // skip
      } else {
        existing.remove(); // e.g. previous error or finished, recreate
      }
    }
    twitchResolve(url).catch(console.error);
  }
});



// ===== State Restoration =====
const UIState = {
  get urls() { return JSON.parse(localStorage.getItem('openedUrls') || '[]'); },
  add(url, platform, data) {
     const all = this.urls.filter(x => x.url !== url);
     all.unshift({url, platform, data});
     localStorage.setItem('openedUrls', JSON.stringify(all));
  },
  remove(url) {
     localStorage.setItem('openedUrls', JSON.stringify(this.urls.filter(x => x.url !== url)));
  }
};

document.addEventListener('DOMContentLoaded', async () => {
    const savedTab = localStorage.getItem('activeTab');
    if (savedTab) {
        const btn = document.querySelector(`.tab[data-tab="${savedTab}"]`);
        if (btn) btn.click();
    }

    try {
        const r = await fetch('/api/state');
        if (r.ok) {
            const data = await r.json();
            if (typeof data.parallel !== 'undefined') {
                const parallelToggle = document.querySelector('#parallelToggle');
                if (parallelToggle) parallelToggle.checked = data.parallel;
            }
            for (const job of data.jobs) {
                const u = job.meta?.url || job.req_data?.url || job.req_data?.original_url;
                if (document.querySelector(`.card[data-job="${job.id}"]`)) continue;
                if (u) {
                    UIState.remove(u);
                    const existing = document.querySelector(`.card[data-url="${u}"]`);
                    if (existing) existing.remove();
                }

                const card = createItemCard(job.meta || {});
                card.dataset.job = job.id;
                card.dataset.url = u;
                const closeBtn = card.querySelector('.close');
                if (closeBtn) closeBtn.classList.remove('hidden');
                if (!job.done && !job.error) {
                  card.classList.add('is-downloading');
                }

                if (job.platform === 'twitch') {
                    const twResults = document.querySelector('#twResults');
                    twResults.append(card);
                } else {
                    const queue = document.querySelector('#queue');
                    queue.append(card);
                }

                const formatsEl = card.querySelector('.formats');
                formatsEl.innerHTML = '';
                const progress = renderProgress(formatsEl, job.info_text || '');
                if (!job.done && !job.error) {
                    startSSE(job.id, progress);
                } else if (job.error) {
                    // job.error - wyświetl error z ostatnimi danymi + retry/resume buttons
                    const lp = job.last_progress || {};
                    const fill = progress.querySelector('.progress-fill');
                    const label = progress.querySelector('.progress-label');
                    if(label) label.textContent = `${(lp.percent || 0).toFixed(1)}%`;
                    if(fill) fill.style.width = `${lp.percent || 0}%`;
                    progress.classList.add('progress-retrying');
                    const inLeft = progress.querySelector('.progress-left');
                    const inRight = progress.querySelector('.progress-right');
                    if(inLeft) inLeft.textContent = lp.downloaded || '?';
                    if(inRight) inRight.textContent = lp.size || '?';
                    const rightSpan = progress.querySelector('.right');
                    if(rightSpan) rightSpan.textContent = lp.speed || '0 B/s';

                    // Dodaj retry/resume buttons
                    const actionsDiv = document.createElement('div');
                    actionsDiv.className = 'retry-actions';

                    const retryBtn = document.createElement('button');
                    retryBtn.className = 'retry-btn';
                    retryBtn.textContent = 'Retry from start';
                    retryBtn.onclick = async () => {
                      try { await fetch(`/api/cancel/${job.id}`, { method: 'DELETE' }); } catch(e) {}
                      const reqBody = {...job.req_data, meta: job.meta, info_text: job.info_text};
                      if (job.platform === 'twitch') {
                        reqBody.start_sec = 0;
                        reqBody.end_sec = job.req_data.end_sec;
                      }
                      const endpoint = job.platform === 'twitch' ? '/api/twitch/download' : '/api/yt/download';
                      try {
                        const r = await fetch(endpoint, {method: 'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(reqBody)});
                        if (r.ok) {
                          const {job_id: newJobId} = await r.json();
                          card.dataset.job = newJobId;
                          actionsDiv.remove();
                          progress.classList.remove('progress-cancelled');
                          label.textContent = 'Starting...';
                          startSSE(newJobId, progress);
                        }
                      } catch(e) { console.error(e); }
                    };
                    actionsDiv.appendChild(retryBtn);

                    // Resume button dla Twitch
                    if (job.platform === 'twitch' && job.last_time && job.last_time > 15) {
                      const resumeBtn = document.createElement('button');
                      resumeBtn.className = 'retry-btn primary';
                      resumeBtn.textContent = 'Resume download';
                      resumeBtn.onclick = async () => {
                        try { await fetch(`/api/cancel/${job.id}`, { method: 'DELETE' }); } catch(e) {}
                        const reqBody = {...job.req_data, meta: job.meta, info_text: job.info_text};
                        reqBody.start_sec = Math.max(0, job.last_time - 15);
                        reqBody.end_sec = job.req_data.end_sec;
                        try {
                          const r = await fetch('/api/twitch/download', {method: 'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(reqBody)});
                          if (r.ok) {
                            const {job_id: newJobId} = await r.json();
                            card.dataset.job = newJobId;
                            actionsDiv.remove();
                            progress.classList.remove('progress-cancelled');
                            label.textContent = 'Resuming...';
                            startSSE(newJobId, progress);
                          }
                        } catch(e) { console.error(e); }
                      };
                      actionsDiv.appendChild(resumeBtn);
                    }
                    progress.appendChild(actionsDiv);
                } else {
                    // job.done - ustaw last_progress lub domyślne wartości
                    const lp = job.last_progress || {};
                    const label = progress.querySelector('.progress-label');
                    if(label) label.textContent = 'Done';
                    progress.classList.add('progress-success');
                    const fill = progress.querySelector('.progress-fill');
                    if(fill) fill.style.width = '100%';
                    // Ustaw ostatnie znane wartości rozmiarów
                    const inLeft = progress.querySelector('.progress-left');
                    const inRight = progress.querySelector('.progress-right');
                    if(inLeft) inLeft.textContent = lp.downloaded || '?';
                    if(inRight) inRight.textContent = lp.size || '?';
                    const rightSpan = progress.querySelector('.right');
                    if(rightSpan) rightSpan.textContent = lp.speed || '0 B/s';
                }
            }
        }
    } catch(e) { console.error(e); }

    for (const item of UIState.urls) {
        if (document.querySelector(`.card[data-url="${item.url}"]`)) continue;
        if (item.platform === 'twitch') {
            if (typeof twitchResolve !== 'undefined') twitchResolve(item.url, item.data).catch(e => console.error(e));
        } else {
            if (typeof fetchFormats !== 'undefined') fetchFormats(item.url, item.data).catch(e => console.error(e));
        }
    }
});
