// ZAI Memory Hub — Living Library
//
// Primary surface at /.  Editorial reader for everything ZAI has recorded.
// Three-column shell, taxonomy nav on left, card feed in centre, live
// context on right.  Search-first.  Click any card → reader drawer.

(() => {
"use strict";

const CAT_HERO = {
  'coding':   '/static/gen/cat_coding.jpg',
  'web':      '/static/gen/cat_web.jpg',
  'mobile':   '/static/gen/cat_mobile.jpg',
  'github':   '/static/gen/cat_github.jpg',
  'agents':   '/static/gen/cat_agents.jpg',
  'planning': '/static/gen/cat_planning.jpg',
  'longterm': '/static/gen/cat_longterm.jpg',
  'terminal': '/static/gen/cat_terminal.jpg',
  'core':     '/static/gen/zai_lockup.jpg',
};
const CAT_COLORS = {
  'coding':'#ff5046','web':'#7aa6ff','mobile':'#5ee2a0','github':'#e8d49a',
  'agents':'#c084ff','planning':'#ff9a4a','longterm':'#dc2626','terminal':'#4adcff',
};
const ACTOR_IMG = {
  'vps-claude':'/static/gen/actor_vps.jpg',
  'local-claude':'/static/gen/actor_local.jpg',
  'chat-claude':'/static/gen/actor_chat.jpg',
};
const HEROES = ['/static/gen/lib_hero_today.jpg','/static/gen/lib_hero_archive.jpg','/static/gen/lib_hero_window.jpg'];

const State = {
  memories: [],       // full set, sorted newest first
  decisions: [],
  clusters: [],
  presence: [],
  stream: [],
  filter: { kind: 'all', value: null, label: 'All memories' },
  search: '',
  saved: loadSaved(),
};

// ----- Helpers ----------------------------------------------------
function esc(s){ if (s==null) return ''; return String(s).replace(/[&<>"']/g, m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }
function trunc(s, n){ s = s || ''; return s.length > n ? s.slice(0, n).trimEnd() + '…' : s; }
function loadSaved(){
  try { return new Set(JSON.parse(localStorage.getItem('zai_lib_saved_v1') || '[]')); }
  catch(_) { return new Set(); }
}
function persistSaved(){
  try { localStorage.setItem('zai_lib_saved_v1', JSON.stringify(Array.from(State.saved))); }
  catch(_) {}
}
function timeAgo(iso){
  const t = new Date(iso).getTime();
  const d = Math.max(1, (Date.now() - t) / 1000);
  if (d < 60) return Math.floor(d) + 's ago';
  if (d < 3600) return Math.floor(d/60) + 'm ago';
  if (d < 86400) return Math.floor(d/3600) + 'h ago';
  if (d < 86400*7) return Math.floor(d/86400) + 'd ago';
  return new Date(iso).toLocaleDateString('en-CA', {month:'short', day:'numeric'});
}
function guessCategory(m){
  // Heuristic mapping from tags / author → category slug
  const tags = (m.tags || []).map(t => t.toLowerCase());
  const byTag = {
    code:'coding', coding:'coding', dev:'coding', ui:'coding',
    web:'web', chrome:'web', chat:'web',
    mobile:'mobile', phone:'mobile', iphone:'mobile',
    github:'github', pr:'github', repo:'github', commit:'github', oss:'github',
    plan:'planning', planning:'planning', goal:'planning', project:'planning', milestone:'planning',
    shell:'terminal', ssh:'terminal', bash:'terminal', htb:'terminal', vpn:'terminal', log:'terminal',
  };
  for (const t of tags){ if (byTag[t]) return byTag[t]; }
  if ((m.importance || 3) >= 4) return 'longterm';
  const auth = m.written_by || '';
  if (auth === 'vps-claude') return 'terminal';
  if (auth === 'local-claude') return 'coding';
  if (auth === 'chat-claude') return 'web';
  return 'agents';
}

// ----- Data loading ----------------------------------------------
async function loadAll(){
  const [recent, decisions, clusters, presence, stream] = await Promise.all([
    fetch('/api/recent?n=120').then(r => r.ok ? r.json() : []).catch(()=>[]),
    fetch('/api/decisions?n=20').then(r => r.ok ? r.json() : []).catch(()=>[]),
    fetch('/api/clusters').then(r => r.ok ? r.json() : []).catch(()=>[]),
    fetch('/api/presence').then(r => r.ok ? r.json() : []).catch(()=>[]),
    fetch('/api/memory_stream').then(r => r.ok ? r.json() : []).catch(()=>[]),
  ]);
  State.memories = recent;
  State.decisions = decisions;
  State.clusters = clusters;
  State.presence = presence;
  State.stream = stream;
  renderAll();
}

// ----- Feed filtering --------------------------------------------
function filteredMemories(){
  let arr = State.memories.slice();
  const f = State.filter;
  if (f.kind === 'today'){
    const dayStart = new Date(); dayStart.setHours(0,0,0,0);
    arr = arr.filter(m => new Date(m.created_at) >= dayStart);
  } else if (f.kind === 'week'){
    const wkAgo = new Date(Date.now() - 7*86400*1000);
    arr = arr.filter(m => new Date(m.created_at) >= wkAgo);
  } else if (f.kind === 'saved'){
    arr = arr.filter(m => State.saved.has(m.id));
  } else if (f.kind === 'category'){
    arr = arr.filter(m => guessCategory(m) === f.value);
  } else if (f.kind === 'entity'){
    arr = arr.filter(m => (m.entity_slugs || []).includes(f.value));
  } else if (f.kind === 'author'){
    arr = arr.filter(m => m.written_by === f.value);
  }
  if (State.search){
    const q = State.search.toLowerCase();
    arr = arr.filter(m => {
      const hay = (m.content || '') + ' ' + (m.tags || []).join(' ') + ' ' + (m.written_by || '');
      return hay.toLowerCase().includes(q);
    });
  }
  return arr;
}

// ----- Render: feed (memory cards) -------------------------------
function renderFeed(){
  const list = filteredMemories();
  const el = document.getElementById('feed');
  if (!list.length){
    el.innerHTML = `<div class="empty">
      <div class="empty-eyebrow">No memories match</div>
      <div class="empty-title">try clearing the filter</div>
      <button class="empty-reset" onclick="window.__lib.clearFilter()">Reset</button>
    </div>`;
    return;
  }
  // Hero card on top
  const heroIdx = Math.floor(new Date().getDate() % HEROES.length);
  const hero = list[0];
  const heroCat = guessCategory(hero);
  el.innerHTML = `
    <article class="card hero" data-id="${esc(hero.id)}">
      <div class="card-img" style="background-image:url(${HEROES[heroIdx]})">
        <div class="card-img-overlay"></div>
        <div class="card-img-tag" style="--c:${CAT_COLORS[heroCat]||'#dc2626'}">${heroCat.toUpperCase()} · LATEST</div>
      </div>
      <div class="card-body">
        <div class="eyebrow"><span>${(hero.written_by||'').replace('-claude','').toUpperCase()}</span><span>${esc(timeAgo(hero.created_at))}</span></div>
        <h2 class="headline">${esc(trunc(hero.content, 160))}</h2>
        <p class="body">${esc(trunc(hero.content.slice(120), 220))}</p>
        <div class="tags">${(hero.tags||[]).slice(0,6).map(t => `<span class="tag" data-tag="${esc(t)}">#${esc(t)}</span>`).join('')}</div>
      </div>
    </article>
  ` + list.slice(1).map(m => renderCard(m)).join('');

  // wire interactions
  el.querySelectorAll('.card').forEach(c => {
    c.addEventListener('click', (e) => {
      if (e.target.closest('.tag')) return;
      if (e.target.closest('.save-btn')) return;
      openReader(c.dataset.id);
    });
  });
  el.querySelectorAll('.tag').forEach(t => {
    t.addEventListener('click', (e) => {
      e.stopPropagation();
      setFilter({ kind: 'tag', value: t.dataset.tag, label: '#' + t.dataset.tag });
    });
  });
  el.querySelectorAll('.save-btn').forEach(b => {
    b.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleSaved(b.dataset.id);
    });
  });
}
function renderCard(m){
  const cat = guessCategory(m);
  const col = CAT_COLORS[cat] || '#dc2626';
  const heroImg = CAT_HERO[cat];
  const saved = State.saved.has(m.id);
  return `
    <article class="card" data-id="${esc(m.id)}" style="--accent:${col}">
      <div class="card-bar"></div>
      <div class="card-meta">
        <span class="cat" style="color:${col}">${cat.toUpperCase()}</span>
        <span class="dot">·</span>
        <span class="time">${esc(timeAgo(m.created_at))}</span>
        <span class="dot">·</span>
        <span class="by">${esc((m.written_by||'').replace('-claude','').toUpperCase())}</span>
        <span class="dot">·</span>
        <span class="imp">imp ${m.importance||3}</span>
        <button class="save-btn ${saved ? 'on' : ''}" data-id="${esc(m.id)}" title="${saved ? 'Saved' : 'Save'}">${saved ? '★' : '☆'}</button>
      </div>
      <h3 class="card-headline">${esc(trunc(m.content, 140))}</h3>
      ${m.content.length > 140 ? `<p class="card-preview">${esc(trunc(m.content.slice(110), 260))}</p>` : ''}
      <div class="tags">${(m.tags||[]).slice(0,6).map(t => `<span class="tag" data-tag="${esc(t)}">#${esc(t)}</span>`).join('')}</div>
    </article>
  `;
}

// ----- Render: taxonomy nav --------------------------------------
function renderTaxonomy(){
  const dayStart = new Date(); dayStart.setHours(0,0,0,0);
  const wkAgo = new Date(Date.now() - 7*86400*1000);
  const todayN = State.memories.filter(m => new Date(m.created_at) >= dayStart).length;
  const weekN  = State.memories.filter(m => new Date(m.created_at) >= wkAgo).length;
  const savedN = State.saved.size;
  const f = State.filter;
  const isActive = (kind, val) => f.kind === kind && (val == null || f.value === val);

  const cats = State.clusters.filter(c => c.slug !== 'core');
  const catRows = cats.map(c => {
    const localN = State.memories.filter(m => guessCategory(m) === c.slug).length;
    const active = isActive('category', c.slug);
    return `<a class="tx-row ${active ? 'active' : ''}" data-kind="category" data-val="${esc(c.slug)}" data-label="${esc(c.label)}" href="javascript:void(0)">
      <span class="tx-dot" style="background:${CAT_COLORS[c.slug]||'#dc2626'}"></span>
      <span class="tx-lbl">${esc(c.label)}</span>
      <span class="tx-n">${localN}</span>
    </a>`;
  }).join('');

  // Unique authors
  const authors = Array.from(new Set(State.memories.map(m => m.written_by).filter(Boolean)));
  const authorRows = authors.map(a => {
    const localN = State.memories.filter(m => m.written_by === a).length;
    const active = isActive('author', a);
    const lbl = a.replace('-claude','').toUpperCase();
    return `<a class="tx-row ${active ? 'active' : ''}" data-kind="author" data-val="${esc(a)}" data-label="${esc(lbl)}-CLAUDE" href="javascript:void(0)">
      <span class="tx-avatar" style="background-image:url(${ACTOR_IMG[a]||''})"></span>
      <span class="tx-lbl">${esc(lbl)}-Claude</span>
      <span class="tx-n">${localN}</span>
    </a>`;
  }).join('');

  // Unique referenced entities
  const ents = new Map();
  for (const m of State.memories){
    for (const slug of (m.entity_slugs || [])) {
      ents.set(slug, (ents.get(slug) || 0) + 1);
    }
  }
  const entRows = Array.from(ents.entries()).sort((a,b) => b[1]-a[1]).slice(0, 8).map(([slug, n]) => {
    const active = isActive('entity', slug);
    return `<a class="tx-row ${active ? 'active' : ''}" data-kind="entity" data-val="${esc(slug)}" data-label="${esc(slug)}" href="javascript:void(0)">
      <span class="tx-dot" style="background:#e8d49a"></span>
      <span class="tx-lbl">${esc(slug)}</span>
      <span class="tx-n">${n}</span>
    </a>`;
  }).join('');

  document.getElementById('taxonomy').innerHTML = `
    <div class="tx-block">
      <a class="tx-row ${isActive('all') ? 'active' : ''}" data-kind="all" data-label="All memories" href="javascript:void(0)">
        <span class="tx-dot" style="background:#f5ecdb"></span><span class="tx-lbl">All memories</span><span class="tx-n">${State.memories.length}</span>
      </a>
      <a class="tx-row ${isActive('today') ? 'active' : ''}" data-kind="today" data-label="Today" href="javascript:void(0)">
        <span class="tx-dot" style="background:#ff7060"></span><span class="tx-lbl">Today</span><span class="tx-n">${todayN}</span>
      </a>
      <a class="tx-row ${isActive('week') ? 'active' : ''}" data-kind="week" data-label="This week" href="javascript:void(0)">
        <span class="tx-dot" style="background:#dc2626"></span><span class="tx-lbl">This week</span><span class="tx-n">${weekN}</span>
      </a>
      <a class="tx-row ${isActive('saved') ? 'active' : ''}" data-kind="saved" data-label="Saved" href="javascript:void(0)">
        <span class="tx-dot" style="background:#e8d49a"></span><span class="tx-lbl">Saved</span><span class="tx-n">${savedN}</span>
      </a>
    </div>
    <div class="tx-head">Categories</div>
    <div class="tx-block">${catRows}</div>
    <div class="tx-head">Authors</div>
    <div class="tx-block">${authorRows}</div>
    ${entRows ? `<div class="tx-head">Referenced</div><div class="tx-block">${entRows}</div>` : ''}
  `;
  document.querySelectorAll('.tx-row').forEach(el => {
    el.addEventListener('click', () => {
      setFilter({ kind: el.dataset.kind, value: el.dataset.val || null, label: el.dataset.label || 'All' });
    });
  });
}

// ----- Render: right context panel -------------------------------
function renderContext(){
  // Online block
  const onlineHTML = State.presence.map(p => {
    const cls = p.status === 'online' ? 'online' : (p.status === 'recent' ? 'recent' : 'offline');
    const ago = p.age_s != null
      ? (p.age_s < 60 ? Math.floor(p.age_s)+'s' :
         p.age_s < 3600 ? Math.floor(p.age_s/60)+'m' :
         p.age_s < 86400 ? Math.floor(p.age_s/3600)+'h' : '—')
      : 'never';
    return `<div class="pr-row">
      <div class="pr-avatar ${cls}" style="background-image:url(${ACTOR_IMG[p.slug]||''})"></div>
      <div class="pr-lbl">${esc(p.slug.replace('-claude','').toUpperCase())}-CLAUDE</div>
      <div class="pr-ago">${esc(ago)}</div>
    </div>`;
  }).join('');

  // Activity sparkline
  const sparkSVG = sparklineSVG(State.stream.map(p => p.n));
  const lastHour = State.stream.slice(-60).reduce((s,p) => s + p.n, 0);

  // Trending tags
  const tagCount = new Map();
  for (const m of State.memories.slice(0, 30)){
    for (const t of (m.tags || [])) tagCount.set(t, (tagCount.get(t)||0) + 1);
  }
  const trendingHTML = Array.from(tagCount.entries()).sort((a,b) => b[1]-a[1]).slice(0, 7).map(([t, n]) =>
    `<a class="tg" data-tag="${esc(t)}" href="javascript:void(0)">#${esc(t)} <span>${n}</span></a>`
  ).join('');

  // Latest decisions
  const decHTML = State.decisions.slice(0, 3).map(d => `
    <div class="dec-row" data-dec="${esc(d.id)}">
      <div class="dec-title">${esc(trunc(d.summary, 70))}</div>
      <div class="dec-meta">${esc((d.written_by||'').replace('-claude',''))} · ${esc(timeAgo(d.created_at))}</div>
    </div>
  `).join('');

  document.getElementById('context').innerHTML = `
    <section class="ctx">
      <div class="ctx-head">Online</div>
      ${onlineHTML || '<div class="ctx-empty">no presence data</div>'}
    </section>
    <section class="ctx">
      <div class="ctx-head">Activity · last hour</div>
      <div class="ctx-big">${lastHour}<small>events</small></div>
      ${sparkSVG}
    </section>
    <section class="ctx">
      <div class="ctx-head">Trending tags</div>
      <div class="ctx-tags">${trendingHTML || '<div class="ctx-empty">no tags yet</div>'}</div>
    </section>
    <section class="ctx">
      <div class="ctx-head">Recent decisions</div>
      ${decHTML || '<div class="ctx-empty">no decisions yet</div>'}
    </section>
    <section class="ctx universe-portal">
      <div class="ctx-head">Universe</div>
      <a class="univ-card" href="/universe">
        <div class="univ-vis"></div>
        <div class="univ-cta">
          <div class="univ-title">Open the memory cloud</div>
          <div class="univ-sub">A spatial view of every cluster</div>
          <div class="univ-arrow">→</div>
        </div>
      </a>
    </section>
  `;
  // Trending tag click
  document.querySelectorAll('.ctx-tags .tg').forEach(el => {
    el.addEventListener('click', () => setFilter({ kind: 'tag', value: el.dataset.tag, label: '#' + el.dataset.tag }));
  });
  // Decision click
  document.querySelectorAll('.dec-row').forEach(el => {
    el.addEventListener('click', () => {
      const d = State.decisions.find(x => x.id === el.dataset.dec);
      if (d) openDecisionReader(d);
    });
  });
}
function sparklineSVG(values){
  if (!values || !values.length) return '';
  const W = 240, H = 38;
  const max = Math.max(1, ...values);
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * W;
    const y = H - (v / max) * (H - 2) - 1;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const line = 'M ' + pts.join(' L ');
  const area = line + ` L ${W},${H} L 0,${H} Z`;
  return `<svg viewBox="0 0 ${W} ${H}" class="ctx-spark" preserveAspectRatio="none">
    <path d="${area}" fill="rgba(255,112,96,0.18)"/>
    <path d="${line}" fill="none" stroke="#ff7060" stroke-width="1.3"/>
  </svg>`;
}

// ----- Filter / search state changes -----------------------------
function setFilter(f){
  State.filter = f;
  document.getElementById('filterChip').classList.toggle('show', f.kind !== 'all');
  document.getElementById('filterLbl').textContent = f.label || 'All';
  renderFeed();
  renderTaxonomy();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
function clearFilter(){
  setFilter({ kind: 'all', value: null, label: 'All memories' });
  document.getElementById('searchInput').value = '';
  State.search = '';
  renderFeed();
}
window.__lib = { clearFilter, setFilter };

function setupSearch(){
  const inp = document.getElementById('searchInput');
  if (!inp) return;
  inp.addEventListener('input', (e) => {
    State.search = e.target.value;
    renderFeed();
  });
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'k'){
      e.preventDefault();
      inp.focus(); inp.select();
    }
    if (e.key === 'Escape' && document.activeElement === inp){
      inp.value = ''; State.search = ''; renderFeed(); inp.blur();
    }
  });
}

function toggleSaved(id){
  if (State.saved.has(id)) State.saved.delete(id);
  else State.saved.add(id);
  persistSaved();
  renderFeed();
  renderTaxonomy();
}

// ----- Reader (memory detail drawer) -----------------------------
async function openReader(id){
  const dr = document.getElementById('reader');
  const body = document.getElementById('readerBody');
  body.innerHTML = `<div class="rd-kind">Memory · loading</div>`;
  dr.classList.add('open');
  try {
    const [r, allM] = await Promise.all([
      fetch('/api/memory/' + id),
      Promise.resolve(State.memories),
    ]);
    if (!r.ok) throw new Error(r.status);
    const m = await r.json();
    const tagSet = new Set(m.tags || []);
    const neighbours = allM
      .filter(x => x.id !== m.id)
      .map(x => ({ ...x, overlap: (x.tags || []).filter(t => tagSet.has(t)) }))
      .filter(x => x.overlap.length >= 2)
      .sort((a,b) => b.overlap.length - a.overlap.length)
      .slice(0, 6);
    const cat = guessCategory(m);
    const heroImg = CAT_HERO[cat];
    const tagsHTML = (m.tags || []).map(t => `<span class="tag">#${esc(t)}</span>`).join('');
    const entHTML = (m.entity_slugs || []).map(s => `<a class="rel-row ent" data-ent="${esc(s)}" href="javascript:void(0)"><span class="dot"></span><span>${esc(s)}</span><span class="ovl">entity</span></a>`).join('');
    const neighHTML = neighbours.map(n => `<a class="rel-row mem" data-mid="${esc(n.id)}" href="javascript:void(0)"><span class="dot"></span><span>${esc(trunc(n.content, 64))}</span><span class="ovl">${n.overlap.length} shared</span></a>`).join('');
    const saved = State.saved.has(m.id);
    body.innerHTML = `
      <div class="rd-hero" style="background-image:url(${heroImg})">
        <div class="rd-hero-tint"></div>
        <div class="rd-eyebrow">${cat.toUpperCase()} · ${esc(timeAgo(m.created_at))} · imp ${m.importance}</div>
      </div>
      <button class="rd-save ${saved ? 'on' : ''}" id="rdSave">${saved ? '★ Saved' : '☆ Save'}</button>
      <h2 class="rd-headline">${esc(trunc(m.content, 220))}</h2>
      <div class="rd-meta">
        <span class="k">by</span><span class="v">${esc(m.written_by || '')}</span>
        <span class="k">at</span><span class="v">${esc(new Date(m.created_at).toLocaleString('en-CA', {hour12:false}))}</span>
        <span class="k">id</span><span class="v">${esc(m.id)}</span>
      </div>
      <div class="rd-body">${esc(m.content)}</div>
      ${tagsHTML ? `<div class="rd-tags">${tagsHTML}</div>` : ''}
      ${(entHTML || neighHTML) ? `<div class="rd-rel">
        <div class="rd-rel-head">Relation map</div>
        ${entHTML}
        ${neighHTML}
      </div>` : ''}
    `;
    body.querySelector('#rdSave')?.addEventListener('click', () => {
      toggleSaved(m.id);
      const sv = State.saved.has(m.id);
      body.querySelector('#rdSave').textContent = sv ? '★ Saved' : '☆ Save';
      body.querySelector('#rdSave').classList.toggle('on', sv);
    });
    body.querySelectorAll('.rel-row.mem').forEach(el => el.addEventListener('click', () => openReader(el.dataset.mid)));
    body.querySelectorAll('.rel-row.ent').forEach(el => el.addEventListener('click', () => {
      closeReader();
      setFilter({ kind: 'entity', value: el.dataset.ent, label: '@' + el.dataset.ent });
    }));
  } catch (e) {
    body.innerHTML = `<div class="rd-kind">Memory</div><div class="rd-body">load failed</div>`;
  }
}
function openDecisionReader(d){
  const dr = document.getElementById('reader');
  const body = document.getElementById('readerBody');
  body.innerHTML = `
    <div class="rd-hero" style="background-image:url(${CAT_HERO.core})"><div class="rd-hero-tint"></div>
      <div class="rd-eyebrow">DECISION · ${esc(timeAgo(d.created_at))}</div></div>
    <h2 class="rd-headline">${esc(d.summary || '')}</h2>
    <div class="rd-meta"><span class="k">by</span><span class="v">${esc(d.written_by || '')}</span></div>
    <div class="rd-body">${esc(d.rationale || '')}</div>
    ${d.alternatives ? `<div class="rd-rel"><div class="rd-rel-head">Alternatives considered</div>${(d.alternatives || []).map(a => `<div class="rel-row"><span class="dot"></span><span>${esc(a)}</span></div>`).join('')}</div>` : ''}
  `;
  dr.classList.add('open');
}
function closeReader(){
  document.getElementById('reader').classList.remove('open');
}
document.getElementById('readerClose')?.addEventListener('click', closeReader);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeReader(); });

// ----- SSE — refresh on activity ---------------------------------
function connectSSE(){
  const es = new EventSource('/events');
  es.addEventListener('activity', () => { loadAll(); });
  es.onerror = () => { es.close(); setTimeout(connectSSE, 3000); };
}

function renderShelves(){
  const el = document.getElementById('shelves');
  if (!el) return;
  const cats = State.clusters.filter(c => c.slug !== 'core');
  const activeSlug = State.filter.kind === 'category' ? State.filter.value : null;
  el.innerHTML = cats.map(c => `
    <article class="shelf ${activeSlug === c.slug ? 'active' : ''}" data-slug="${esc(c.slug)}" data-label="${esc(c.label)}">
      <img src="${CAT_HERO[c.slug] || ''}" alt="" loading="lazy" decoding="async">
      <span class="shelf-count">${c.nodes}</span>
      <div class="shelf-body">
        <div class="shelf-cat" style="color:${CAT_COLORS[c.slug]||'#e8d49a'}">${esc(c.label.toUpperCase())}</div>
        <div class="shelf-title">${esc(c.sub || '')}</div>
      </div>
    </article>
  `).join('');
  el.querySelectorAll('.shelf').forEach(s => {
    s.addEventListener('click', () => {
      const slug = s.dataset.slug;
      // Toggle: if already filtered to this category, clear
      if (State.filter.kind === 'category' && State.filter.value === slug){
        setFilter({ kind: 'all', value: null, label: 'All memories' });
      } else {
        setFilter({ kind: 'category', value: slug, label: s.dataset.label });
      }
    });
  });
}

function renderAll(){
  renderShelves();
  renderFeed();
  renderTaxonomy();
  renderContext();
}

// Boot
setupSearch();
loadAll().then(() => connectSSE());
setInterval(loadAll, 60000);

})();
