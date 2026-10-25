/* MotionClipper KL – interaksi halaman (tanpa framework). */

const STATUS_TEXT = { queued: 'Menunggu antrian', running: 'Berjalan', done: 'Selesai', error: 'Gagal', cancelled: 'Dibatalkan' };

function toast(msg, bad) {
  const t = document.createElement('div');
  t.className = 'toast' + (bad ? ' bad' : '');
  t.textContent = msg;
  t.setAttribute('role', 'status');
  document.body.appendChild(t);
  setTimeout(() => t.remove(), bad ? 6000 : 2600);
}

async function api(path, body, method) {
  const opts = { method: method || (body === undefined ? 'GET' : 'POST'), headers: {} };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { /* kosong */ }
  if (!r.ok) {
    const msg = (data && (data.detail || data.error)) || ('Error ' + r.status);
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}

function tc(t) {
  if (t === null || t === undefined || isNaN(t)) return '–';
  t = Number(t);
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = Math.floor(t % 60);
  const frac = Math.round((t - Math.floor(t)) * 10);
  const base = h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
  return frac ? `${base}.${frac === 10 ? 9 : frac}` : base;
}

function parseTc(v) {
  v = String(v || '').trim().replace(',', '.');
  if (!v) return NaN;
  if (!v.includes(':')) return Number(v);
  return v.split(':').reduce((acc, p) => acc * 60 + Number(p), 0);
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

/* ---------------------------------------------------------------- beranda */

function pollDashboard() {
  const list = document.querySelector('[data-poll-jobs]');
  if (!list) return;
  async function tick() {
    let active = false;
    try {
      const jobs = await api('/api/jobs');
      jobs.forEach(j => {
        const row = list.querySelector(`[data-job="${j.id}"]`);
        if (!row) return;
        const st = row.querySelector('[data-status]');
        let text = STATUS_TEXT[j.status] || j.status;
        const labels = { download: 'unduh', audio: 'audio', transcribe: 'transkripsi', peaks: 'lonjakan suara', select: 'pilih momen', metadata: 'caption', render: 'render' };
        if (j.status === 'running' && j.stage) text += ' · ' + (labels[j.stage] || j.stage);
        if (j.status === 'queued' && j.queue_pos) text += ' #' + j.queue_pos;
        st.querySelector('span').textContent = text;
        st.querySelector('i').style.width = ((j.progress || 0) * 100) + '%';
        if (j.status === 'running' || j.status === 'queued') active = true;
      });
    } catch (e) { /* abaikan */ }
    setTimeout(tick, active ? 2500 : 10000);
  }
  tick();
}

/* ---------------------------------------------------------------- halaman job */

function initJobPage(jid, initialLayout, dmin, dmax) {
  const $ = sel => document.querySelector(sel);
  const stageLabels = {};
  document.querySelectorAll('#stages li').forEach(li => { stageLabels[li.dataset.stage] = li.textContent.trim(); });
  let layout = Object.assign({ mode: 'facecam_box', content_fit: 'cover', facecam_pos: 'top_center', facecam_width: 380, content: null, facecam: null }, initialLayout || {});
  let data = null;
  let drawTarget = 'content';
  const cards = {};
  let snapshotLoaded = false;

  /* ---------- status job */
  function renderJob() {
    const job = data.job;
    document.title = job.title + ' · MotionClipper KL';
    $('#job-title').textContent = job.title;
    $('#job-cost').textContent = `${data.cost_idr} ($${((job.usage || {}).cost_usd || 0).toFixed(4)})`;
    document.querySelectorAll('#stages li').forEach(li => {
      const st = (job.stages[li.dataset.stage] || {}).status;
      li.className = st || '';
      li.title = (job.stages[li.dataset.stage] || {}).msg || '';
    });
    let text = STATUS_TEXT[job.status] || job.status;
    if (job.status === 'running' && job.stage) text = 'Berjalan · ' + (stageLabels[job.stage] || job.stage);
    if (job.status === 'queued' && job.queue_pos) text += ` (antrian ke-${job.queue_pos})`;
    $('#status-text').textContent = text;
    const pct = Math.round((job.progress || 0) * 100);
    $('#progress-text').textContent = job.status === 'running' ? pct + '%' : '';
    $('#progress-bar').style.width = (job.status === 'done' ? 100 : pct) + '%';
    const err = $('#error-box');
    err.hidden = !(job.status === 'error' && job.error);
    err.textContent = job.error || '';
    const log = $('#log');
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
    log.textContent = (job.log || []).join('\n');
    if (atBottom) log.scrollTop = log.scrollHeight;
    const running = job.status === 'running' || job.status === 'queued';
    document.querySelector('[data-act="cancel"]').hidden = !running;
    document.querySelector('[data-act="retry"]').hidden = running || job.status === 'done';
  }

  /* ---------- timeline sumber */
  function renderTimeline() {
    const dur = data.job.duration;
    if (!dur) { $('#timeline-wrap').hidden = true; return; }
    $('#timeline-wrap').hidden = false;
    $('#tl-mid').textContent = tc(dur / 2);
    $('#tl-end').textContent = tc(dur);
    const tl = $('#timeline');
    const maxRise = Math.max(8, ...data.peaks.map(p => p.rise));
    let html = data.peaks.map(p => `<span class="peak" style="left:${(p.t / dur) * 100}%;height:${12 + (p.rise / maxRise) * 26}px" title="${tc(p.t)} · +${p.rise} dB"></span>`).join('');
    html += data.clips.map(c => `<button type="button" class="span${c.approved ? ' approved' : ''}" data-goto="${c.id}" style="left:${(c.start / dur) * 100}%;width:${((c.end - c.start) / dur) * 100}%" title="${c.id} · ${tc(c.start)}–${tc(c.end)}">${c.id}</button>`).join('');
    tl.innerHTML = html;
  }

  /* ---------- kartu clip */
  function playerHTML(c) {
    const r = c.render || {};
    if (r.status === 'done' && r.file) {
      const v = encodeURIComponent(r.rendered_at || '');
      return `<video controls preload="metadata" playsinline src="/files/jobs/${jid}/${r.file}?v=${v}" poster="/files/jobs/${jid}/${r.thumb}?v=${v}"></video>`;
    }
    if (r.status === 'rendering') return `<div class="state">Merender… ${Math.round((r.progress || 0) * 100)}%</div>`;
    if (r.status === 'queued') return '<div class="state">Menunggu giliran render</div>';
    if (r.status === 'error') return `<div class="state" style="color:#ff9a9a">Render gagal.<br>Lihat detail di bawah.</div>`;
    return '<div class="state">Belum dirender</div>';
  }

  function checksHTML(c) {
    const items = c.checks || [];
    if (!items.length) return '';
    return '<ul class="checks">' + items.map(i => `<li class="${i.ok ? 'ok' : 'no'}" title="${esc(i.detail)}">${esc(i.label)}${!i.ok && i.detail ? ' · ' + esc(i.detail) : ''}</li>`).join('') + '</ul>';
  }

  function cardHTML(c) {
    const r = c.render || {};
    const len = c.end - c.start;
    const extra = c.asset_duration ? ` + asset ${c.asset_duration.toFixed(1)} dtk` : '';
    const hot = c.score >= 80 ? ' hot' : '';
    const warn = (r.warnings || []).map(w => `<div class="hint" style="color:var(--gold)">${esc(w)}</div>`).join('');
    const err = r.status === 'error' ? `<div class="error-box">${esc(r.error || '')}</div>` : '';
    const issues = (c.compliance && !c.compliance.ok) ? `<div class="pill bad" style="margin-bottom:8px">AI menandai: ${esc((c.compliance.issues || []).join('; ') || 'melanggar larangan')}</div>` : '';
    return `
      <div class="player">${playerHTML(c)}</div>
      <div>
        <div class="clip-head">
          <div>
            <h3>${esc(c.id)} · ${esc(c.title || c.hook_line || 'Tanpa judul')}</h3>
            <div class="muted" style="font-size:13px">${tc(c.start)} – ${tc(c.end)} · ${len.toFixed(1)} dtk${extra}${c.source === 'manual' ? ' · manual' : ''}</div>
          </div>
          <div class="score${hot}">${c.score || '–'}<small>skor</small></div>
        </div>
        ${c.reason ? `<p class="reason">${esc(c.reason)}</p>` : ''}
        ${issues}${checksHTML(c)}${warn}${err}
        <div class="grid2" style="margin-top:10px">
          <div class="field"><label>Judul</label><input type="text" data-f="title" value="${esc(c.title)}"></div>
          <div class="field"><label>Teks hook di layar</label><input type="text" data-f="hook_text" value="${esc(c.hook_text)}"></div>
        </div>
        <div class="field caption-box">
          <label>Caption <span class="hint">· website dan hashtag ditambahkan otomatis</span></label>
          <textarea data-f="caption_body" rows="2">${esc(c.caption_body)}</textarea>
          <div class="caption-full">${esc(c.caption_full || '')}</div>
        </div>
        <div class="times">
          <div><label>Mulai</label><input type="text" data-f="start" data-orig="${c.start}" value="${tc(c.start)}"></div>
          <div><label>Selesai</label><input type="text" data-f="end" data-orig="${c.end}" value="${tc(c.end)}"></div>
          <div class="row"><button class="btn small" data-c="nudge" data-d="-2" title="Mulai 2 detik lebih awal">−2 dtk</button><button class="btn small" data-c="nudge" data-d="2" title="Selesai 2 detik lebih lama">+2 dtk</button></div>
        </div>
        <details class="sub-edit"><summary>Koreksi teks subtitle</summary><div class="sents hint" style="margin-top:6px">Memuat…</div><button class="btn small" data-c="save-subs" style="margin-top:8px">Simpan koreksi</button></details>
        <div class="clip-actions">
          <button class="btn small ${c.approved ? 'on' : ''}" data-c="approve">${c.approved ? '✓ Disetujui' : 'Setujui'}</button>
          <button class="btn small" data-c="copy">Salin caption</button>
          <span class="grow"></span>
          ${r.stale ? '<span class="stale">Ada perubahan, render ulang</span>' : ''}
          <button class="btn small" data-c="save">Simpan</button>
          <button class="btn small primary" data-c="render">Render ulang</button>
          ${r.status === 'done' ? `<a class="btn small" href="/files/jobs/${jid}/${r.file}" download="${esc(jid + '-' + c.id)}.mp4">Unduh</a>` : ''}
          <button class="btn small danger" data-c="delete">Hapus</button>
        </div>
      </div>`;
  }

  function renderClips() {
    const box = $('#clips');
    const clips = data.clips;
    $('#clip-count').textContent = clips.length ? `· ${clips.length} · ${clips.filter(c => c.approved).length} disetujui` : '';
    if (!clips.length) {
      if (!box.querySelector('.panel')) box.innerHTML = '<div class="panel muted">Clip muncul di sini setelah tahap Pilih momen selesai.</div>';
      return;
    }
    box.querySelector('.panel')?.remove();
    const ids = new Set(clips.map(c => c.id));
    Object.keys(cards).forEach(id => { if (!ids.has(id)) { cards[id].el.remove(); delete cards[id]; } });
    clips.forEach((c, idx) => {
      const sig = JSON.stringify(c);
      let card = cards[c.id];
      if (!card) {
        const el = document.createElement('article');
        el.className = 'clip';
        el.id = 'clip-' + c.id;
        el.dataset.id = c.id;
        card = cards[c.id] = { el, sig: '' };
      }
      if (box.children[idx] !== card.el) box.insertBefore(card.el, box.children[idx] || null);
      if (card.sig === sig) return;
      const focused = card.el.contains(document.activeElement) && document.activeElement.matches('input, textarea');
      const playing = card.el.querySelector('video') && !card.el.querySelector('video').paused;
      const subsOpen = card.el.querySelector('details.sub-edit')?.open;
      if ((focused || playing || subsOpen || card.dirty) && card.sig && card.sig !== sig) {
        // Jangan timpa yang sedang diedit; cukup perbarui pemutar & status jika render berubah.
        const old = JSON.parse(card.sig);
        if (JSON.stringify(old.render) !== JSON.stringify(c.render) && !playing) {
          card.el.querySelector('.player').innerHTML = playerHTML(c);
        }
        return;
      }
      card.sig = sig;
      card.el.classList.toggle('is-approved', !!c.approved);
      card.el.innerHTML = cardHTML(c);
    });
  }

  function clipById(id) { return data.clips.find(c => c.id === id); }

  function readCard(el) {
    const v = f => el.querySelector(`[data-f="${f}"]`).value;
    const time = f => {
      const inp = el.querySelector(`[data-f="${f}"]`);
      return inp.value.trim() === tc(Number(inp.dataset.orig)) ? Number(inp.dataset.orig) : parseTc(inp.value);
    };
    const start = time('start'), end = time('end');
    if (isNaN(start) || isNaN(end)) throw new Error('Format waktu tidak dikenali. Contoh: 1:02:15 atau 75.5');
    return { title: v('title'), hook_text: v('hook_text'), caption_body: v('caption_body'), start, end };
  }

  async function saveCard(el) {
    const id = el.dataset.id;
    const body = readCard(el);
    const len = body.end - body.start;
    if (len < dmin - 0.5 || len > dmax + 0.5) toast(`Durasi ${len.toFixed(1)} dtk di luar brief (${dmin}–${dmax} dtk).`, true);
    await api(`/api/jobs/${jid}/clips/${id}`, body);
    cards[id].dirty = false;
    cards[id].sig = '';
  }

  $('#clips').addEventListener('input', e => {
    const el = e.target.closest('.clip');
    if (el && e.target.matches('[data-f]')) cards[el.dataset.id].dirty = true;
  });

  $('#clips').addEventListener('toggle', async e => {
    const det = e.target;
    if (!det.matches('details.sub-edit') || !det.open) return;
    const el = det.closest('.clip');
    const box = det.querySelector('.sents');
    try {
      const sents = await api(`/api/jobs/${jid}/clips/${el.dataset.id}/sentences`);
      box.innerHTML = sents.map(s => `<div class="sent"><span>${tc(s.s)}</span><input type="text" data-i="${s.i}" value="${esc(s.t)}"></div>`).join('') || 'Tidak ada kalimat.';
    } catch (err) { box.textContent = err.message; }
  }, true);

  $('#clips').addEventListener('click', async e => {
    const btn = e.target.closest('[data-c]');
    if (!btn) return;
    const el = btn.closest('.clip');
    const id = el.dataset.id;
    const c = clipById(id);
    try {
      switch (btn.dataset.c) {
        case 'approve':
          await api(`/api/jobs/${jid}/clips/${id}`, { approved: !c.approved });
          cards[id].sig = '';
          break;
        case 'copy':
          await navigator.clipboard.writeText(c.caption_full || '');
          toast('Caption disalin.');
          return;
        case 'nudge': {
          const d = Number(btn.dataset.d);
          const f = el.querySelector(d < 0 ? '[data-f="start"]' : '[data-f="end"]');
          f.value = tc(Math.max(0, parseTc(f.value) + d));
          cards[id].dirty = true;
          return;
        }
        case 'save':
          await saveCard(el);
          toast('Tersimpan. Render ulang agar perubahan waktu dan hook masuk ke video.');
          break;
        case 'render':
          await saveCard(el);
          await api(`/api/jobs/${jid}/render`, { clips: [id] });
          toast('Render dimasukkan ke antrian.');
          break;
        case 'delete':
          if (!confirm(`Hapus clip ${id}?`)) return;
          await api(`/api/jobs/${jid}/clips/${id}`, undefined, 'DELETE');
          break;
        case 'save-subs': {
          const edits = [...el.querySelectorAll('.sents input')].map(i => ({ i: Number(i.dataset.i), text: i.value }));
          await api(`/api/jobs/${jid}/sentences`, { edits, clip_id: id });
          el.querySelector('details.sub-edit').open = false;
          cards[id].sig = '';
          toast('Subtitle dikoreksi. Klik Render ulang untuk menerapkan.');
          break;
        }
      }
      refresh();
    } catch (err) { toast(err.message, true); }
  });

  $('#timeline').addEventListener('click', e => {
    const b = e.target.closest('[data-goto]');
    if (b) document.getElementById('clip-' + b.dataset.goto)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  /* ---------- aksi job */
  document.addEventListener('click', async e => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const act = btn.dataset.act;
    try {
      if (act === 'cancel') { await api(`/api/jobs/${jid}/cancel`, {}); toast('Membatalkan…'); }
      if (act === 'retry') { await api(`/api/jobs/${jid}/retry`, {}); toast('Dilanjutkan dari tahap terakhir.'); }
      if (act === 'render-all') {
        if (!confirm('Render ulang semua clip dengan tata letak dan aturan campaign terbaru?')) return;
        await api(`/api/jobs/${jid}/render`, { clips: null });
        Object.values(cards).forEach(c => { c.sig = ''; c.dirty = false; });
        toast('Semua clip masuk antrian render.');
      }
      if (act === 'open-folder') { await api(`/api/jobs/${jid}/open-folder`, {}); }
      if (act === 'add-clip') {
        const start = parseTc($('#m-start').value), end = parseTc($('#m-end').value);
        await api(`/api/jobs/${jid}/clips`, { start, end });
        $('#m-start').value = ''; $('#m-end').value = '';
        toast('Clip manual ditambahkan dan masuk antrian.');
      }
      if (act === 'delete-source') {
        if (!confirm('Hapus file video sumber dan audio? Clip yang sudah jadi tetap ada. Render ulang nanti butuh unduh ulang.')) return;
        const r = await api(`/api/jobs/${jid}/delete-source`, {});
        toast(`Ruang disk dibebaskan ±${r.freed_mb} MB.`);
      }
      if (act === 'delete-job') {
        if (!confirm('Hapus job ini beserta semua clip-nya?')) return;
        await api(`/api/jobs/${jid}/delete`, {});
        location.href = '/';
        return;
      }
      if (act === 'clear-facecam') { layout.facecam = null; drawRects(); }
      if (act === 'preview') await preview();
      if (act === 'save-layout' || act === 'save-default') {
        syncLayoutFromForm();
        await api(`/api/jobs/${jid}/layout`, { layout, save_default: act === 'save-default' });
        toast(act === 'save-default' ? 'Disimpan sebagai default campaign. Klik Render ulang semua untuk menerapkan.' : 'Tata letak job disimpan. Klik Render ulang semua untuk menerapkan.');
      }
      refresh();
    } catch (err) { toast(err.message, true); }
  });

  $('#rerun-stage').addEventListener('change', async e => {
    const st = e.target.value;
    e.target.value = '';
    if (!st) return;
    if (!confirm(`Jalankan ulang mulai tahap "${stageLabels[st]}"? Hasil tahap itu dan sesudahnya akan dibuat ulang.`)) return;
    try { await api(`/api/jobs/${jid}/rerun/${st}`, {}); Object.values(cards).forEach(c => c.sig = ''); refresh(); }
    catch (err) { toast(err.message, true); }
  });

  /* ---------- penata letak */
  const img = $('#snapshot');
  const canvas = $('#draw-canvas');
  const ctx = canvas.getContext('2d');
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

  function syncFormFromLayout() {
    $('#lay-mode').value = layout.mode;
    $('#lay-fit').value = layout.content_fit;
    $('#lay-pos').value = layout.facecam_pos;
    $('#lay-width').value = layout.facecam_width;
    $('#lay-width-val').textContent = layout.facecam_width;
  }
  function syncLayoutFromForm() {
    layout.mode = $('#lay-mode').value;
    layout.content_fit = $('#lay-fit').value;
    layout.facecam_pos = $('#lay-pos').value;
    layout.facecam_width = Number($('#lay-width').value);
  }
  ['#lay-mode', '#lay-fit', '#lay-pos', '#lay-width'].forEach(s => $(s).addEventListener('input', () => { syncLayoutFromForm(); $('#lay-width-val').textContent = layout.facecam_width; }));

  document.querySelectorAll('[data-draw]').forEach(b => b.addEventListener('click', () => {
    drawTarget = b.dataset.draw;
    document.querySelectorAll('[data-draw]').forEach(x => x.classList.toggle('on', x === b));
  }));

  function sizeCanvas() {
    const r = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, r.width * dpr);
    canvas.height = Math.max(1, r.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawRects();
  }

  let dragRect = null;
  function drawRects() {
    const w = canvas.getBoundingClientRect().width, h = canvas.getBoundingClientRect().height;
    ctx.clearRect(0, 0, w, h);
    if (!snapshotLoaded) return;
    const draw = (r, color, label, dashed) => {
      if (!r) return;
      ctx.save();
      ctx.strokeStyle = color; ctx.lineWidth = 2;
      if (dashed) ctx.setLineDash([6, 5]);
      ctx.fillStyle = color + '22';
      ctx.fillRect(r.x * w, r.y * h, r.w * w, r.h * h);
      ctx.strokeRect(r.x * w + 1, r.y * h + 1, r.w * w - 2, r.h * h - 2);
      ctx.setLineDash([]);
      ctx.font = '600 12px Archivo, system-ui, sans-serif';
      const tw = ctx.measureText(label).width + 10;
      ctx.fillStyle = color;
      ctx.fillRect(r.x * w, r.y * h, tw, 18);
      ctx.fillStyle = '#fff';
      ctx.fillText(label, r.x * w + 5, r.y * h + 13);
      ctx.restore();
    };
    const accent = '#3d52e6', gold = '#e0a300';
    draw(layout.content || { x: 0, y: 0, w: 1, h: 1 }, accent, layout.content ? 'Konten' : 'Konten: seluruh frame', !layout.content);
    draw(layout.facecam, gold, 'Facecam');
    if (dragRect) draw(dragRect, drawTarget === 'content' ? accent : gold, drawTarget === 'content' ? 'Konten' : 'Facecam');
  }

  function pt(e) {
    const r = canvas.getBoundingClientRect();
    return { x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)) };
  }
  let p0 = null;
  canvas.addEventListener('pointerdown', e => { if (!snapshotLoaded) return; p0 = pt(e); canvas.setPointerCapture(e.pointerId); });
  canvas.addEventListener('pointermove', e => {
    if (!p0) return;
    const p = pt(e);
    dragRect = { x: Math.min(p0.x, p.x), y: Math.min(p0.y, p.y), w: Math.abs(p.x - p0.x), h: Math.abs(p.y - p0.y) };
    drawRects();
  });
  canvas.addEventListener('pointerup', () => {
    if (dragRect && dragRect.w > 0.03 && dragRect.h > 0.03) {
      layout[drawTarget] = Object.fromEntries(Object.entries(dragRect).map(([k, v]) => [k, Math.round(v * 10000) / 10000]));
      if (drawTarget === 'facecam' && layout.mode === 'center') { layout.mode = 'facecam_box'; syncFormFromLayout(); }
    }
    p0 = null; dragRect = null; drawRects();
  });

  img.addEventListener('load', () => { snapshotLoaded = true; img.hidden = false; $('#snapshot-empty').hidden = true; sizeCanvas(); });
  window.addEventListener('resize', sizeCanvas);

  async function preview() {
    syncLayoutFromForm();
    $('#preview-note').textContent = 'Membuat pratinjau…';
    try {
      const r = await api(`/api/jobs/${jid}/preview`, { layout });
      const p = $('#preview-img');
      p.src = r.url; p.hidden = false;
      $('#preview-note').textContent = 'Pratinjau memakai watermark dan logo campaign, tanpa subtitle. Simpan jika sudah pas.';
    } catch (err) { $('#preview-note').textContent = err.message; }
  }

  /* ---------- polling */
  let timer = null;
  async function refresh() {
    clearTimeout(timer);
    try {
      data = await api(`/api/jobs/${jid}`);
      renderJob();
      renderTimeline();
      renderClips();
      if (data.has_snapshot && !snapshotLoaded && !img.src) img.src = `/files/jobs/${jid}/snapshot.jpg`;
    } catch (err) { /* server mungkin sedang restart */ }
    const busy = data && (['running', 'queued'].includes(data.job.status) || data.clips.some(c => ['rendering', 'queued'].includes((c.render || {}).status)));
    timer = setTimeout(refresh, busy ? 2000 : 8000);
  }

  syncFormFromLayout();
  refresh();
}
