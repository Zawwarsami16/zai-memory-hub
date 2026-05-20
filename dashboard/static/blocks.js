// ZAI Memory Hub — Blocks Home
//
// The primary surface.  Stack of enterable blocks: an Active Agents row
// at top (auto-discovers every distinct memory author), a Timeline of
// the most recent 30 events, and a grid of topical blocks (Philosophy,
// Hacking, Decisions, etc.).  Click any block → "room" overlay with
// that subject's full content properly organized.

(() => {
"use strict";

const ACTOR_IMG = {
  'vps-claude':   '/static/gen/actor_vps.jpg',
  'local-claude': '/static/gen/actor_local.jpg',
  'chat-claude':  '/static/gen/actor_chat.jpg',
};
const BLOCK_HERO = {
  'philosophy':   '/static/gen/lib_hero_today.jpg',
  'hacking':      '/static/gen/cat_terminal.jpg',
  'crypto':       '/static/gen/cat_github.jpg',
  'infra':        '/static/gen/cat_agents.jpg',
  'decisions':    '/static/gen/cat_planning.jpg',
  'references':   '/static/gen/lib_hero_archive.jpg',
  'now-building': '/static/gen/cat_coding.jpg',
  'tools':        '/static/gen/cat_web.jpg',
};
// Hover micro-loops — only some blocks have a generated video; others
// keep the still image.  Probed at load time so we don't reference a
// missing file.
const BLOCK_LOOP = {
  'philosophy':   '/static/gen/loop_philosophy.mp4',
  'hacking':      '/static/gen/loop_hacking.mp4',
  'crypto':       '/static/gen/loop_crypto.mp4',
  'infra':        '/static/gen/loop_infra.mp4',
};

function esc(s){ if (s==null) return ''; return String(s).replace(/[&<>"']/g, m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }
function trunc(s,n){ s = s||''; return s.length>n ? s.slice(0,n).trimEnd()+'…' : s; }
function timeAgo(iso){
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  const d = Math.max(1, (Date.now() - t) / 1000);
  if (d < 60) return Math.floor(d) + 's ago';
  if (d < 3600) return Math.floor(d/60) + 'm ago';
  if (d < 86400) return Math.floor(d/3600) + 'h ago';
  if (d < 86400*7) return Math.floor(d/86400) + 'd ago';
  return new Date(iso).toLocaleDateString('en-CA', {month:'short', day:'numeric'});
}
function avatarFor(slug){
  if (ACTOR_IMG[slug]) return ACTOR_IMG[slug];
  // Procedural avatar — coloured gradient with first letter
  return null;
}
function colorFor(slug){
  const colors = ['#dc2626','#ff5046','#7aa6ff','#5ee2a0','#e8d49a','#c084ff','#ff9a4a','#4adcff'];
  let h = 0; for (let i=0; i<slug.length; i++) h = (h*31 + slug.charCodeAt(i)) | 0;
  return colors[Math.abs(h) % colors.length];
}

// ----- Data loading ---------------------------------------------
const State = { agents: [], blocks: [], timeline: [], presence: [], stream: [], decisions: [] };

async function loadAll(){
  const [agents, blocks, timeline, presence, stream, decisions] = await Promise.all([
    fetch('/api/agents').then(r => r.ok ? r.json() : []).catch(()=>[]),
    fetch('/api/blocks').then(r => r.ok ? r.json() : []).catch(()=>[]),
    fetch('/api/timeline?n=8').then(r => r.ok ? r.json() : []).catch(()=>[]),
    fetch('/api/presence').then(r => r.ok ? r.json() : []).catch(()=>[]),
    fetch('/api/memory_stream').then(r => r.ok ? r.json() : []).catch(()=>[]),
    fetch('/api/decisions?n=5').then(r => r.ok ? r.json() : []).catch(()=>[]),
  ]);
  State.agents = agents; State.blocks = blocks; State.timeline = timeline;
  State.presence = presence; State.stream = stream; State.decisions = decisions;
  render();
}

// ----- Render: Active Agents row --------------------------------
function renderAgents(){
  const el = document.getElementById('agents');
  el.innerHTML = State.agents.map((a, i) => {
    const img = avatarFor(a.slug);
    const col = colorFor(a.slug);
    const delay = '';
    return `
      <article class="agent" data-slug="${esc(a.slug)}" style="--accent:${col}">
        <header class="agent-head">
          <div class="agent-avatar ${esc(a.status)}" style="${img ? `background-image:url(${img})` : `background:linear-gradient(135deg, ${col}, #0a0508)`}">
            ${!img ? `<span>${a.slug[0].toUpperCase()}</span>` : ''}
          </div>
          <div class="agent-id">
            <div class="agent-name">${esc(a.display)}</div>
            <div class="agent-meta">
              <span class="status ${esc(a.status)}">${esc(a.status)}</span>
              <span>·</span>
              <span>${a.age_s == null ? 'never' : timeAgo(a.last_seen)}</span>
              <span>·</span>
              <span>${a.total} mem</span>
            </div>
          </div>
        </header>
        <ol class="agent-recent">
          ${a.recent.map((m, i) => `
            <li class="recent-row" data-mid="${esc(m.id)}">
              <span class="r-i">${String(i+1).padStart(2,'0')}</span>
              <div class="r-body">
                <div class="r-text">${esc(trunc(m.preview, 86))}</div>
                <div class="r-meta">${esc(timeAgo(m.created_at))} · imp ${m.importance}</div>
              </div>
            </li>
          `).join('') || '<li class="recent-empty">No memories yet</li>'}
        </ol>
        <button class="agent-open" data-slug="${esc(a.slug)}">Open feed →</button>
      </article>
    `;
  }).join('') + `
    <article class="agent placeholder reveal delay-4">
      <div class="agent-placeholder-icon">
        <svg viewBox="0 0 24 24" stroke-width="1.4" stroke="currentColor" fill="none">
          <circle cx="12" cy="12" r="9"/>
          <line x1="12" y1="7" x2="12" y2="17"/><line x1="7" y1="12" x2="17" y2="12"/>
        </svg>
      </div>
      <div class="placeholder-title">Connect a new agent</div>
      <div class="placeholder-body">
        Set <code>ZAI_HUB_WRITTEN_BY=&lt;slug&gt;</code>, point your MCP client at <code>${window.location.origin}/mcp</code>, write a memory - your panel appears here automatically.
      </div>
      <a class="placeholder-cta" href="/connect">How to connect →</a>
    </article>
  `;
  // wire interactions
  document.querySelectorAll('.recent-row').forEach(el => el.addEventListener('click', () => openMemory(el.dataset.mid)));
  document.querySelectorAll('.agent-open').forEach(el => el.addEventListener('click', () => openAgentRoom(el.dataset.slug)));
}

// ----- Render: Timeline (compact ticker) ------------------------
function renderTimeline(){
  const el = document.getElementById('timeline');
  if (!State.timeline.length){
    el.innerHTML = '<div class="empty" style="padding:24px">No activity yet</div>';
    return;
  }
  el.innerHTML = State.timeline.slice(0, 7).map(m => `
    <li class="tk-row" data-mid="${esc(m.id)}">
      <div class="tk-time">${esc(timeAgo(m.created_at))}</div>
      <div class="tk-dot" style="background:${colorFor(m.written_by||'')}"></div>
      <div class="tk-author">${esc((m.written_by||'').replace('-claude','').toUpperCase())}</div>
      <div class="tk-text">${esc(trunc(m.preview, 110))}</div>
      <div class="tk-tags">${(m.tags||[]).slice(0,2).map(t => '#'+t).join(' ')}</div>
    </li>
  `).join('');
  document.querySelectorAll('.tk-row').forEach(el => el.addEventListener('click', () => openMemory(el.dataset.mid)));
}

// ----- Render: Side rail ----------------------------------------
function renderSide(){
  // Online
  const onlineEl = document.getElementById('sideOnline');
  if (onlineEl){
    if (!State.presence.length){
      onlineEl.innerHTML = '<div style="font-family:var(--serif-soft);font-style:italic;color:var(--fg-dim);font-size:12px">no presence data</div>';
    } else {
      onlineEl.innerHTML = State.presence.map(p => {
        const cls = p.status;
        const ago = p.age_s != null
          ? (p.age_s < 60 ? Math.floor(p.age_s)+'s' :
             p.age_s < 3600 ? Math.floor(p.age_s/60)+'m' :
             p.age_s < 86400 ? Math.floor(p.age_s/3600)+'h' : '—')
          : 'never';
        const img = ACTOR_IMG[p.slug];
        const lbl = p.slug.replace('-claude','').toUpperCase();
        return `<div class="so-row">
          <div class="so-avatar ${cls}" style="${img ? `background-image:url(${img})` : `background:${colorFor(p.slug)}`}">${!img ? lbl[0] : ''}</div>
          <div class="so-lbl">${esc(lbl)}-Claude</div>
          <div class="so-ago">${esc(ago)}</div>
        </div>`;
      }).join('');
    }
  }
  // Activity count + sparkline
  const ctEl = document.getElementById('sideActivityCount');
  if (ctEl){
    const lastHour = State.stream.slice(-60).reduce((s,p) => s + p.n, 0);
    ctEl.innerHTML = `${lastHour}<small>events</small>`;
  }
  const spEl = document.getElementById('sideSparkline');
  if (spEl){
    const values = State.stream.map(p => p.n);
    if (values.length){
      const W = 250, H = 30;
      const max = Math.max(1, ...values);
      const pts = values.map((v, i) => {
        const x = (i / (values.length - 1)) * W;
        const y = H - (v / max) * (H - 2) - 1;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      });
      const line = 'M ' + pts.join(' L ');
      const area = line + ` L ${W},${H} L 0,${H} Z`;
      spEl.innerHTML = `<svg class="side-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
        <path class="area" d="${area}"/>
        <path class="line" d="${line}"/>
      </svg>`;
    }
  }
  // Trending tags
  const trEl = document.getElementById('sideTrending');
  if (trEl){
    const counts = new Map();
    for (const m of State.timeline){
      for (const t of (m.tags || [])) counts.set(t, (counts.get(t)||0) + 1);
    }
    const top = Array.from(counts.entries()).sort((a,b) => b[1]-a[1]).slice(0, 8);
    if (!top.length) trEl.innerHTML = '<div style="font-family:var(--serif-soft);font-style:italic;color:var(--fg-dim);font-size:12px">no tags yet</div>';
    else trEl.innerHTML = top.map(([t, n]) => `<a class="st" href="javascript:void(0)">#${esc(t)} <span>${n}</span></a>`).join('');
  }
  // Latest decision
  const decEl = document.getElementById('sideDecision');
  if (decEl){
    const d = State.decisions[0];
    if (!d){
      decEl.innerHTML = '<div style="font-family:var(--serif-soft);font-style:italic;color:var(--fg-dim);font-size:12px">no decisions yet</div>';
    } else {
      decEl.innerHTML = `
        <div class="side-dec-title">${esc(trunc(d.summary || '', 80))}</div>
        <div class="side-dec-meta">${esc((d.written_by||'').replace('-claude',''))} · ${esc(timeAgo(d.created_at))}</div>
      `;
    }
  }
}

// ----- Render: topical blocks grid ------------------------------
function renderBlocks(){
  const el = document.getElementById('blocks');
  el.innerHTML = State.blocks.map((b, i) => {
    const hero = BLOCK_HERO[b.slug];
    const loop = BLOCK_LOOP[b.slug];
    const previews = (b.preview_items || []).slice(0, 3);
    const delay = '';
    return `
      <article class="block" data-slug="${esc(b.slug)}" style="--accent:${b.accent}">
        ${hero ? `
          <div class="block-hero">
            <img class="block-hero-img" src="${hero}" alt="" loading="lazy" decoding="async">
            ${loop ? `<video class="block-hero-vid" muted loop playsinline preload="none" data-src="${loop}"></video>` : ''}
            <div class="block-hero-tint"></div>
          </div>` : ''}
        <div class="block-body">
          <div class="block-head">
            <span class="block-tag" style="color:${b.accent}">${esc(b.label.toUpperCase())}</span>
            <span class="block-count">${b.count}</span>
          </div>
          <div class="block-sub">${esc(b.sub)}</div>
          <ul class="block-previews">
            ${previews.length
              ? previews.map(p => `<li><span class="pv-dot" style="background:${b.accent}"></span>${esc(trunc(p.title || p.preview || '', 78))}</li>`).join('')
              : '<li class="empty">nothing here yet</li>'
            }
          </ul>
          <button class="block-open">Enter →</button>
        </div>
      </article>
    `;
  }).join('');
  document.querySelectorAll('.block').forEach(el => el.addEventListener('click', () => openBlockRoom(el.dataset.slug)));
  // Hover → load + play the loop video on demand
  document.querySelectorAll('.block').forEach(el => {
    const vid = el.querySelector('.block-hero-vid');
    if (!vid) return;
    el.addEventListener('pointerenter', () => {
      if (!vid.src && vid.dataset.src){ vid.src = vid.dataset.src; }
      vid.play().catch(()=>{});
      vid.classList.add('on');
    });
    el.addEventListener('pointerleave', () => {
      vid.classList.remove('on');
      try { vid.pause(); } catch(_){}
    });
  });
  setupReveal();
}

// ----- Scroll-reveal observer (opt-in, never blocks) -----------
// Strategy: elements are visible by default (CSS).  Only the FIRST
// render adds .before to elements that are below the fold so they get
// a stagger animation in.  IntersectionObserver removes .before as
// they enter the viewport.  Worst case (observer disabled / blocked
// by privacy shields) → elements stay visible.  No invisible content.
let _revealObserver = null;
function setupReveal(){
  if (_revealObserver) _revealObserver.disconnect();
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduced || !('IntersectionObserver' in window)){
    return;  // elements already visible by default
  }
  // Mark below-fold reveal elements as .before so they fade in on scroll
  const fold = window.innerHeight * 0.85;
  document.querySelectorAll('.reveal').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.top > fold) el.classList.add('before');
  });
  _revealObserver = new IntersectionObserver((entries) => {
    for (const en of entries){
      if (en.isIntersecting){
        en.target.classList.remove('before');
        _revealObserver.unobserve(en.target);
      }
    }
  }, { threshold: 0.05, rootMargin: '0px 0px -80px 0px' });
  document.querySelectorAll('.reveal.before').forEach(el => _revealObserver.observe(el));
  // Final safety: anything still .before after 3s is forced visible
  setTimeout(() => {
    document.querySelectorAll('.reveal.before').forEach(el => el.classList.remove('before'));
  }, 3000);
}

function render(){ renderAgents(); renderTimeline(); renderBlocks(); renderSide(); renderDocs(); }

// ----- Documents (PDF vault) ------------------------------------
const Docs = { items: [], uploading: false };

async function loadDocs(){
  try {
    const r = await fetch('/api/documents');
    if (!r.ok) return;
    const d = await r.json();
    Docs.items = d.items || [];
    renderDocs();
  } catch(_){}
}

function renderDocs(){
  const el = document.getElementById('docsRecent');
  if (!el) return;
  const recent = Docs.items.slice(0, 4);
  if (!recent.length){
    el.innerHTML = '<div class="doc-empty">No documents yet — drop a PDF to start your vault.</div>';
    return;
  }
  el.innerHTML = recent.map(d => {
    const pages = d.pages || '?';
    const size = d.size_bytes ? Math.round(Number(d.size_bytes)/1024) + ' KB' : '';
    const tagStr = (d.tags || []).slice(0, 3).join(' · ');
    const cover = d.cover_url ? `<div class="doc-cover" style="background-image:url('${esc(d.cover_url)}')"></div>`
                              : `<div class="doc-ico">PDF</div>`;
    return `<a class="doc-card ${d.cover_url ? 'has-cover' : ''}" href="${esc(d.url)}" target="_blank" rel="noopener" data-mid="${esc(d.id)}">
      ${cover}
      <div class="doc-meta">
        <div class="doc-title">${esc(trunc(d.title || 'Untitled', 80))}</div>
        <div class="doc-sub">${pages}p · ${esc(size)} · ${esc(timeAgo(d.created_at))}${tagStr ? ' · ' + esc(tagStr) : ''}</div>
      </div>
      <div class="doc-arrow">→</div>
    </a>`;
  }).join('');
}

function uploadToast(msg, isErr){
  const el = document.getElementById('uploadToast');
  if (!el) return;
  el.textContent = msg;
  el.classList.toggle('err', !!isErr);
  el.classList.add('show');
  clearTimeout(window.__upToastT);
  window.__upToastT = setTimeout(() => el.classList.remove('show'), 3500);
}

// ---- Two-stage upload: pick → review → save ----
const Upload = { file: null };

function showForm(meta){
  document.getElementById('docsDrop').hidden = true;
  const f = document.getElementById('docsForm');
  f.hidden = false;
  document.getElementById('docsFormFilename').textContent = meta.filename || 'document.pdf';
  document.getElementById('docsTitle').value = meta.title || '';
  document.getElementById('docsDesc').value = meta.excerpt ? trunc(meta.excerpt, 240) : '';
  document.getElementById('docsTags').value = (meta.tags || []).filter(t => t !== 'document').join(', ');
  document.getElementById('docsCover').checked = false;
  const kb = meta.size_bytes ? Math.round(meta.size_bytes/1024) : 0;
  document.getElementById('docsFormMeta').textContent = `${meta.pages || '?'} pages · ${kb} KB`;
  document.getElementById('docsTitle').focus();
}

function hideForm(){
  document.getElementById('docsForm').hidden = true;
  document.getElementById('docsDrop').hidden = false;
  Upload.file = null;
  document.getElementById('docsFile').value = '';
}

async function pickFile(file){
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.pdf')){
    uploadToast('PDF files only', true); return;
  }
  if (file.size > 20*1024*1024){
    uploadToast('File too large (max 20 MB)', true); return;
  }
  Upload.file = file;
  uploadToast('Reading PDF…');
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await fetch('/api/upload/preview', { method: 'POST', body: fd });
    if (!r.ok) throw new Error(await r.text() || 'preview failed');
    const meta = await r.json();
    uploadToast('Review the details, then save');
    showForm(meta);
  } catch (e) {
    uploadToast('Couldn\'t read PDF: ' + e.message, true);
    Upload.file = null;
  }
}

async function saveUpload(ev){
  ev.preventDefault();
  if (!Upload.file) return;
  const title = document.getElementById('docsTitle').value.trim();
  if (!title){ uploadToast('Title is required', true); return; }
  const desc = document.getElementById('docsDesc').value.trim();
  const tags = document.getElementById('docsTags').value.trim();
  const cover = document.getElementById('docsCover').checked;
  const btn = document.getElementById('docsSave');
  btn.disabled = true;
  btn.textContent = cover ? 'Painting cover…' : 'Saving…';
  uploadToast(cover ? `Saving + painting cover (~30s)…` : `Saving ${Upload.file.name}…`);
  const fd = new FormData();
  fd.append('file', Upload.file);
  fd.append('title', title);
  fd.append('description', desc);
  fd.append('tags_csv', tags);
  fd.append('generate_cover', cover ? 'true' : 'false');
  try {
    const r = await fetch('/api/upload', { method: 'POST', body: fd });
    if (!r.ok) throw new Error(await r.text() || 'upload failed');
    const d = await r.json();
    uploadToast(d.cover_url ? `Saved with cover: ${d.title}` : `Saved: ${d.title}`);
    hideForm();
    await loadDocs();
    await loadAll();
  } catch (e) {
    uploadToast('Save failed: ' + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save to Hub';
  }
}

function setupDocsUpload(){
  const drop = document.getElementById('docsDrop');
  const input = document.getElementById('docsFile');
  const form = document.getElementById('docsForm');
  const cancel = document.getElementById('docsFormCancel');
  if (!drop || !input || !form) return;
  input.addEventListener('change', (e) => {
    const f = e.target.files && e.target.files[0];
    if (f) pickFile(f);
    input.value = '';
  });
  ['dragenter','dragover'].forEach(ev => drop.addEventListener(ev, (e) => {
    e.preventDefault(); e.stopPropagation();
    drop.classList.add('over');
  }));
  ['dragleave','drop'].forEach(ev => drop.addEventListener(ev, (e) => {
    e.preventDefault(); e.stopPropagation();
    if (ev === 'drop'){
      const f = e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) pickFile(f);
    }
    drop.classList.remove('over');
  }));
  form.addEventListener('submit', saveUpload);
  cancel.addEventListener('click', () => { hideForm(); uploadToast('Cancelled'); });
}

window.__viewAllDocs = function(){
  // Open the documents room — synthesize via the same block-room engine
  const room = document.getElementById('room');
  const body = document.getElementById('roomBody');
  body.innerHTML = `
    <header class="rm-header" style="--accent:#e8d49a">
      <div class="rm-eyebrow" style="color:#e8d49a">DOCUMENTS</div>
      <h2 class="rm-title">Your PDF vault</h2>
      <div class="rm-sub">${Docs.items.length} indexed</div>
    </header>
    <ol class="rm-list">
      ${Docs.items.map((d, i) => `
        <li class="rm-card decision">
          <div class="rm-i">${String(i+1).padStart(3,'0')}</div>
          <div class="rm-card-body">
            <div class="rm-card-meta">${esc(d.filename || '')} · ${esc(timeAgo(d.created_at))} · ${d.pages || '?'}p · ${d.size_bytes ? Math.round(Number(d.size_bytes)/1024)+'KB' : ''}</div>
            <div class="rm-card-title"><a href="${esc(d.url)}" target="_blank" rel="noopener" style="color:#f5dca3;text-decoration:none;border-bottom:1px dotted">${esc(d.title || 'Untitled')}</a></div>
            <div class="rm-card-text">${esc(trunc(d.summary, 280))}</div>
            ${(d.tags||[]).length ? `<div style="margin-top:8px;display:flex;gap:5px;flex-wrap:wrap">${d.tags.map(t => `<span class="tag" style="font-family:var(--mono);font-size:9.5px;padding:3px 9px;background:rgba(232,212,154,0.08);border:1px solid var(--line-bright);color:var(--fg-soft);border-radius:99px">#${esc(t)}</span>`).join('')}</div>` : ''}
          </div>
        </li>
      `).join('') || '<div class="rm-empty"><div class="rm-empty-title">No documents yet.</div><div class="rm-empty-sub">Drop a PDF on the home page to start.</div></div>'}
    </ol>
  `;
  room.classList.add('open');
};

// ----- Room: agent feed view ------------------------------------
async function openAgentRoom(slug){
  const room = document.getElementById('room');
  const body = document.getElementById('roomBody');
  body.innerHTML = '<div class="rm-loading">Loading…</div>';
  room.classList.add('open');
  const agent = State.agents.find(a => a.slug === slug);
  const r = await fetch('/api/recent?n=120');
  const all = r.ok ? await r.json() : [];
  const mine = all.filter(m => m.written_by === slug);
  body.innerHTML = `
    <header class="rm-header">
      <div class="rm-eyebrow">${esc(agent?.kind || 'agent').toUpperCase()}</div>
      <h2 class="rm-title">${esc(agent?.display || slug)}</h2>
      <div class="rm-sub">${mine.length} memories · ${agent ? esc(agent.status) : ''} · last seen ${agent ? timeAgo(agent.last_seen) : '—'}</div>
    </header>
    <ol class="rm-list">
      ${mine.map((m, i) => `
        <li class="rm-card" data-mid="${esc(m.id)}">
          <div class="rm-i">${String(i+1).padStart(3,'0')}</div>
          <div class="rm-card-body">
            <div class="rm-card-meta">${esc(timeAgo(m.created_at))} · imp ${m.importance}${(m.tags||[]).length ? ' · ' + m.tags.slice(0,4).map(t => '#'+t).join(' ') : ''}</div>
            <div class="rm-card-text">${esc(trunc(m.content, 240))}</div>
          </div>
        </li>
      `).join('')}
    </ol>
  `;
  body.querySelectorAll('.rm-card').forEach(el => el.addEventListener('click', () => openMemory(el.dataset.mid)));
}

// ----- Room: topical block view ---------------------------------
async function openBlockRoom(slug){
  const room = document.getElementById('room');
  const body = document.getElementById('roomBody');
  body.innerHTML = '<div class="rm-loading">Loading…</div>';
  room.classList.add('open');
  const r = await fetch('/api/block/' + slug);
  if (!r.ok){ body.innerHTML = '<div class="rm-loading">Failed to load</div>'; return; }
  const data = await r.json();
  const b = data.block;
  const heroUrl = BLOCK_HERO[slug] || '';
  const renderItem = (it) => {
    if (it.kind === 'memory'){
      return `<li class="rm-card" data-mid="${esc(it.id)}">
        <div class="rm-i">→</div>
        <div class="rm-card-body">
          <div class="rm-card-meta">${esc((it.written_by||'').replace('-claude','').toUpperCase())} · ${esc(timeAgo(it.created_at))} · imp ${it.importance}${(it.tags||[]).length ? ' · ' + it.tags.slice(0,4).map(t => '#'+t).join(' ') : ''}</div>
          <div class="rm-card-text">${esc(trunc(it.full || it.preview, 320))}</div>
        </div>
      </li>`;
    }
    if (it.kind === 'decision'){
      return `<li class="rm-card decision">
        <div class="rm-i">⊕</div>
        <div class="rm-card-body">
          <div class="rm-card-meta">${esc((it.written_by||'').replace('-claude','').toUpperCase())} · ${esc(timeAgo(it.created_at))}</div>
          <div class="rm-card-title">${esc(it.title || '')}</div>
          <div class="rm-card-text">${esc(it.preview || '')}</div>
          ${(it.alternatives && it.alternatives.length) ? `<div class="rm-alts"><span>Alternatives:</span> ${it.alternatives.map(a => `<span class="alt">${esc(a)}</span>`).join('')}</div>` : ''}
        </div>
      </li>`;
    }
    if (it.kind === 'entity'){
      return `<li class="rm-card entity">
        <div class="rm-i">◆</div>
        <div class="rm-card-body">
          <div class="rm-card-meta">${esc(it.ent_kind)}</div>
          <div class="rm-card-title">${esc(it.title || '')}</div>
          <div class="rm-card-text">${esc(it.slug)}</div>
        </div>
      </li>`;
    }
    if (it.kind === 'tool_call'){
      return `<li class="rm-card tool">
        <div class="rm-i">⚡</div>
        <div class="rm-card-body">
          <div class="rm-card-meta">${esc((it.written_by||'').replace('-claude','').toUpperCase())} · ${esc(timeAgo(it.created_at))}</div>
          <div class="rm-card-title">${esc(it.title || '')}</div>
          <div class="rm-card-text">${esc(it.preview || '')}</div>
        </div>
      </li>`;
    }
    return '';
  };
  body.innerHTML = `
    <header class="rm-header" style="--accent:${b.accent || '#dc2626'}">
      ${heroUrl ? `<div class="rm-hero" style="background-image:url(${heroUrl})"><div class="rm-hero-tint"></div></div>` : ''}
      <div class="rm-eyebrow" style="color:${b.accent}">${esc(b.label.toUpperCase())}</div>
      <h2 class="rm-title">${esc(b.label)}</h2>
      <div class="rm-sub">${esc(b.sub)} · ${data.count} items</div>
    </header>
    ${data.items.length ? `<ol class="rm-list">${data.items.map(renderItem).join('')}</ol>` : `
      <div class="rm-empty">
        <div class="rm-empty-title">Nothing tagged here yet.</div>
        <div class="rm-empty-sub">When agents write memories with these tags, they'll appear here:</div>
        <div class="rm-empty-tags">${(b.tags||[]).map(t => `<span class="tag">#${esc(t)}</span>`).join('')}</div>
      </div>
    `}
  `;
  body.querySelectorAll('.rm-card[data-mid]').forEach(el => el.addEventListener('click', () => openMemory(el.dataset.mid)));
}
function closeRoom(){ document.getElementById('room').classList.remove('open'); }
document.getElementById('roomClose')?.addEventListener('click', closeRoom);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeRoom(); });

// ----- Memory reader (small drawer inside room) -----------------
async function openMemory(id){
  const dr = document.getElementById('reader');
  const body = document.getElementById('readerBody');
  body.innerHTML = '<div class="rd-loading">Loading…</div>';
  dr.classList.add('open');
  try {
    const r = await fetch('/api/memory/' + id);
    if (!r.ok) throw new Error(r.status);
    const m = await r.json();
    const tags = (m.tags || []).map(t => `<span class="tag">#${esc(t)}</span>`).join('');
    const ents = (m.entity_slugs || []).map(s => `<span class="tag">${esc(s)}</span>`).join('');
    body.innerHTML = `
      <div class="rd-actions">
        <button class="rd-del-btn" id="rdDelete" title="Move this memory to trash (restorable)">🗑 Delete</button>
      </div>
      <div class="rd-eyebrow">${esc((m.written_by||'').replace('-claude','').toUpperCase())} · ${esc(timeAgo(m.created_at))} · imp ${m.importance}</div>
      <h2 class="rd-headline">${esc(trunc(m.content, 220))}</h2>
      <div class="rd-meta">
        <span class="k">at</span><span class="v">${esc(new Date(m.created_at).toLocaleString('en-CA', {hour12:false}))}</span>
        <span class="k">id</span><span class="v">${esc(m.id)}</span>
      </div>
      <div class="rd-body">${esc(m.content)}</div>
      ${ents ? `<div class="rd-tags">${ents}</div>` : ''}
      ${tags ? `<div class="rd-tags" style="margin-top:8px">${tags}</div>` : ''}
    `;
    document.getElementById('rdDelete')?.addEventListener('click', async () => {
      const ok = confirm(`Delete this memory?\n\n"${trunc(m.content, 120)}"\n\nIt will move to Trash and can be restored.`);
      if (!ok) return;
      try {
        const r = await fetch('/api/memory/' + id + '/delete', { method: 'POST' });
        if (!r.ok) throw new Error('http ' + r.status);
        uploadToast && uploadToast('Memory moved to trash');
        closeReader();
        loadAll();  // refresh everything
      } catch (e) {
        uploadToast && uploadToast('Delete failed: ' + e.message, true);
      }
    });
  } catch (e) { body.innerHTML = '<div class="rd-loading">Failed to load</div>'; }
}
function closeReader(){ document.getElementById('reader').classList.remove('open'); }
document.getElementById('readerClose')?.addEventListener('click', closeReader);

// ----- SSE — refresh on activity ---------------------------------
function connectSSE(){
  const es = new EventSource('/events');
  es.addEventListener('activity', () => loadAll());
  es.onerror = () => { es.close(); setTimeout(connectSSE, 3000); };
}

loadAll().then(() => connectSSE());
setupDocsUpload();
loadDocs();
setInterval(loadAll, 45000);
setInterval(loadDocs, 60000);

})();
