const $ = (sel, ctx=document) => ctx.querySelector(sel);
const $$ = (sel, ctx=document) => Array.from(ctx.querySelectorAll(sel));

const spinner = $('#spinner');
const urlInput = $('#urlInput');
const pathInput = $('#pathInput');
const queue = $('#queue');

function showSpinner(v){ spinner.classList.toggle('hidden', !v); }

function fmtHMS(sec){
  const s = Math.max(0, Math.floor(sec));
  const h = Math.floor(s/3600);
  const m = Math.floor((s%3600)/60);
  const r = s%60;
  return [h,m,r].map((v,i)=> i===0? String(v).padStart(2,'0'):String(v).padStart(2,'0')).join(':');
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

