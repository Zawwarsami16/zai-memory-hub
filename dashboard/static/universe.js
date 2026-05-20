// ZAI Memory Hub — Dedicated /universe page interaction layer
//
// Lives alongside cloud3d.js (which handles WebGL rendering).  This
// file drives:  data loading → bubble positioning → click/hover →
// breadcrumb navigation → sub-constellation of memory orbs → memory
// reader drawer.  Standalone — no rails, no devices, no top tabs.

(() => {
"use strict";

// ----- Palette ---------------------------------------------------
const RED = '#dc2626', RED_HOT = '#ff3a3a', RED_WARM = '#ff7060';
const GOLD = '#e8d49a', FG = '#f5ecdb', FG_SOFT = '#d4c3a0', FG_DIM = '#8a7a6a';

// ----- Canvases --------------------------------------------------
const cv = { dust: document.getElementById('dust'), stars: document.getElementById('stars'), cloud: document.getElementById('cloud') };
const ctx = { dust: cv.dust.getContext('2d'), stars: cv.stars.getContext('2d'), cloud: cv.cloud.getContext('2d') };
let W = 0, H = 0, DPR = Math.min(window.devicePixelRatio || 1, 2);

function sizeAll(){
  W = window.innerWidth; H = window.innerHeight;
  for (const k of ['dust','stars','cloud']){
    const c = cv[k];
    c.width  = Math.floor(W * DPR);
    c.height = Math.floor(H * DPR);
    c.style.width = W + 'px';
    c.style.height = H + 'px';
    ctx[k].setTransform(DPR,0,0,DPR,0,0);
  }
}
sizeAll();
window.addEventListener('resize', () => { sizeAll(); seedStars(); seedDust(); layoutBubbles(); pushToCloud3D(); });

// ----- Dust + Stars ---------------------------------------------
let dust = [], stars = [], comets = [];
function seedDust(){
  const N = Math.round((W*H)/14000); dust = [];
  for (let i=0;i<N;i++){
    const d = Math.random();
    dust.push({x:Math.random()*W,y:Math.random()*H,vx:0.04+d*0.32,vy:(Math.random()-0.5)*0.04,
      r:0.4+d*1.3,a:0.05+d*0.30,tw:Math.random()*Math.PI*2,tws:0.2+Math.random()*0.6,
      hue:Math.random()<0.55?'warm':(Math.random()<0.4?'red':'cream')});
  }
}
function seedStars(){
  const N = Math.round((W*H)/6800); stars = [];
  for (let i=0;i<N;i++){
    const d = Math.random();
    stars.push({x:Math.random()*W,y:Math.random()*H,r:0.2+d*1.5,v:0.03+d*0.18,a:0.20+d*0.55,
      tw:Math.random()*Math.PI*2,tws:0.4+Math.random()*1.4,
      hue:Math.random()<0.08?'gold':(Math.random()<0.05?'red':'white'),
      spike: d>0.92 && Math.random()<0.4});
  }
}
seedDust(); seedStars();

function drawDust(dt,t){
  const g = ctx.dust; g.clearRect(0,0,W,H); g.globalCompositeOperation='lighter';
  for (const d of dust){
    d.x -= d.vx*dt*0.04; d.y += d.vy*dt*0.04;
    if (d.x<-4){ d.x = W+4; d.y = Math.random()*H; }
    if (d.y<-4||d.y>H+4) d.y = Math.random()*H;
    const f = 0.55 + 0.45*Math.sin(d.tw + t*0.001*d.tws);
    const a = d.a*f;
    let col;
    if (d.hue==='warm') col = `rgba(255,160,110,${a})`;
    else if (d.hue==='red') col = `rgba(255,80,70,${a})`;
    else col = `rgba(245,236,219,${a})`;
    const r = d.r*(1+f*0.4);
    const gg = g.createRadialGradient(d.x,d.y,0,d.x,d.y,r*5);
    gg.addColorStop(0,col); gg.addColorStop(1, col.replace(/[\d.]+\)$/, '0)'));
    g.fillStyle = gg; g.beginPath(); g.arc(d.x,d.y,r*5,0,Math.PI*2); g.fill();
  }
  g.globalCompositeOperation = 'source-over';
}
function maybeComet(t){
  if (t-(window.__lc||0) < 3500) return;
  if (Math.random()>0.28){ window.__lc = t; return; }
  window.__lc = t;
  const fromTop = Math.random()<0.5;
  const x0 = fromTop ? Math.random()*W*0.6+W*0.2 : (Math.random()<0.5?-40:W+40);
  const y0 = fromTop ? -40 : Math.random()*H*0.5;
  const dx = fromTop ? (Math.random()-0.3)*0.6 : (x0<0?1:-1);
  const dy = fromTop ? 1 : Math.random()*0.5+0.3;
  const s = 1.2+Math.random()*0.8;
  comets.push({x:x0,y:y0,vx:dx*s,vy:dy*s,trail:[],life:0,max:2400+Math.random()*1200,
    hue:Math.random()<0.5?'gold':'warm'});
}
function drawComets(dt){
  const g = ctx.stars;
  for (let i=comets.length-1;i>=0;i--){
    const c = comets[i]; c.life += dt;
    if (c.life>c.max||c.x<-80||c.x>W+80||c.y>H+80){ comets.splice(i,1); continue; }
    c.x += c.vx*dt*0.18; c.y += c.vy*dt*0.18;
    c.trail.push([c.x,c.y]); if (c.trail.length>22) c.trail.shift();
    const cA = c.hue==='gold' ? '232,212,154' : '255,160,110';
    for (let k=0;k<c.trail.length;k++){
      const [tx,ty] = c.trail[k]; const ka = k/c.trail.length;
      g.fillStyle = `rgba(${cA},${ka*0.5})`;
      g.beginPath(); g.arc(tx,ty,0.8+ka*1.4,0,Math.PI*2); g.fill();
    }
    const hg = g.createRadialGradient(c.x,c.y,0,c.x,c.y,10);
    hg.addColorStop(0,'rgba(255,255,255,1)');
    hg.addColorStop(0.3,`rgba(${cA},0.8)`);
    hg.addColorStop(1,`rgba(${cA},0)`);
    g.fillStyle = hg; g.beginPath(); g.arc(c.x,c.y,10,0,Math.PI*2); g.fill();
    g.fillStyle = '#fff'; g.beginPath(); g.arc(c.x,c.y,1.6,0,Math.PI*2); g.fill();
  }
}
function drawStars(dt,t){
  const g = ctx.stars; g.clearRect(0,0,W,H);
  maybeComet(t); drawComets(dt);
  for (const s of stars){
    s.x -= s.v*dt*0.04;
    if (s.x<-3){ s.x = W+3; s.y = Math.random()*H; }
    const f = 0.6+0.4*Math.sin(s.tw + t*0.001*s.tws);
    const a = s.a*f;
    let col;
    if (s.hue==='gold') col = `rgba(232,212,154,${a})`;
    else if (s.hue==='red') col = `rgba(255,90,90,${a})`;
    else col = `rgba(245,236,219,${a})`;
    g.fillStyle = col; g.beginPath(); g.arc(s.x,s.y,s.r,0,Math.PI*2); g.fill();
    if (s.r>1.1){
      const hg = g.createRadialGradient(s.x,s.y,0,s.x,s.y,s.r*5);
      hg.addColorStop(0, col.replace(/[\d.]+\)$/, (a*0.45)+')'));
      hg.addColorStop(1, col.replace(/[\d.]+\)$/, '0)'));
      g.fillStyle = hg; g.beginPath(); g.arc(s.x,s.y,s.r*5,0,Math.PI*2); g.fill();
    }
    if (s.spike){
      g.strokeStyle = col.replace(/[\d.]+\)$/, (a*0.85)+')');
      g.lineWidth = 0.6;
      const L = s.r*9*f;
      g.beginPath(); g.moveTo(s.x-L,s.y); g.lineTo(s.x+L,s.y); g.moveTo(s.x,s.y-L); g.lineTo(s.x,s.y+L); g.stroke();
    }
  }
}

// ----- Cluster data ----------------------------------------------
const CORE = { id:'core', x:0, y:0, r:120, label:'Core Memory', sub:'Everything Connected', nodes:0, color:RED };
let CATS = [];
const ORBIT_R_BASE = 0.34;  // bigger on /universe because we have more room

function layoutBubbles(){
  CORE.x = W/2; CORE.y = H/2;
  const R = Math.min(W, H) * ORBIT_R_BASE;
  CATS.forEach((c, i) => {
    const ang = (i / CATS.length) * Math.PI * 2 - Math.PI/2;
    c.ang = ang;
    c.x = CORE.x + Math.cos(ang) * R;
    c.y = CORE.y + Math.sin(ang) * R;
    c.r = 64 + Math.min(28, Math.log2((c.nodes||1)+1) * 5);
  });
}
function pushToCloud3D(){
  if (!window.Cloud3D || !CATS.length) return;
  window.Cloud3D.setBubbles(CATS.map(c => ({...c})), {...CORE, r: CORE.r});
}
window.addEventListener('cloud3d-ready', () => pushToCloud3D());

async function loadClusters(){
  const r = await fetch('/api/clusters'); if (!r.ok) return;
  const list = await r.json();
  const coreEntry = list.find(c => c.slug === 'core');
  if (coreEntry) CORE.nodes = coreEntry.nodes;
  CATS = list.filter(c => c.slug !== 'core');
  layoutBubbles();
  pushToCloud3D();
}

// ----- Constellation rotation + parallax ------------------------
const Motion = { angle:0, angVel:0.000014, px:0, py:0, tpx:0, tpy:0 };
window.addEventListener('mousemove', (e) => {
  const cx=W/2, cy=H/2;
  Motion.tpx = (e.clientX-cx)/cx * 22;
  Motion.tpy = -(e.clientY-cy)/cy * 18;
});
function tickConstellation(dt){
  if (!CATS.length) return;
  Motion.angle += dt * Motion.angVel;
  Motion.px += (Motion.tpx - Motion.px) * 0.05;
  Motion.py += (Motion.tpy - Motion.py) * 0.05;
  const R = Math.min(W,H) * ORBIT_R_BASE;
  CATS.forEach((c) => {
    const ang = c.ang + Motion.angle;
    c.x = CORE.x + Math.cos(ang)*R + Motion.px;
    c.y = CORE.y + Math.sin(ang)*R + Motion.py;
  });
  CORE.dispX = CORE.x + Motion.px;
  CORE.dispY = CORE.y + Motion.py;
  if (window.Cloud3D?.updatePositions){
    window.Cloud3D.updatePositions(CATS, { ...CORE, x: CORE.dispX, y: CORE.dispY, id: CORE.id });
    // Cross-arcs are deterministic now (no random per rebuild), so we can
    // re-project them at a high cadence so they stay glued to the rotating
    // bubbles instead of snapping every few seconds.
    if (!Motion._la || performance.now()-Motion._la > 250){
      Motion._la = performance.now();
      window.Cloud3D.rebuildArcsFor(CATS, { ...CORE, x: CORE.dispX, y: CORE.dispY });
    }
  }
}

// ----- Labels (live-projected from 3D) --------------------------
function drawLabels(){
  const g = ctx.cloud; g.clearRect(0,0,W,H);
  // Sub-bubbles take priority if active
  if (SubBubbles.orbits.length){ drawSubBubbles(performance.now()); return; }
  if (!CATS.length) return;
  // CORE label
  let cx = CORE.x, cy = CORE.y;
  if (window.Cloud3D?.getBubbleScreenPos){
    const sp = window.Cloud3D.getBubbleScreenPos(CORE.id);
    if (sp){ cx = sp.x; cy = sp.y; }
  }
  g.textAlign = 'center'; g.textBaseline = 'middle';
  g.font = "16px 'JetBrains Mono', monospace";
  g.fillStyle = '#f5dca3';
  g.fillText('CORE MEMORY', cx, cy - 10);
  g.font = "12px 'Inter', sans-serif";
  g.fillStyle = FG_SOFT;
  g.fillText('Everything Connected', cx, cy + 10);
  g.font = "28px 'JetBrains Mono', monospace";
  g.fillStyle = '#ffffff';
  g.fillText(String(CORE.nodes), cx, cy + 38);
  // Cat labels
  for (const c of CATS){
    let x = c.x, y = c.y;
    if (window.Cloud3D?.getBubbleScreenPos){
      const sp = window.Cloud3D.getBubbleScreenPos(c.id);
      if (sp){ x = sp.x; y = sp.y; }
    }
    const r = c.r || 60;
    const colA = hexToRgb(c.color || RED);
    const isHot = hoverCat === c.slug;
    // Count badge above
    const badgeY = y - r - 20;
    g.font = "bold 12px 'JetBrains Mono', monospace";
    const btw = g.measureText(String(c.nodes)).width;
    const bw = btw + 22, bh = 24;
    g.fillStyle = 'rgba(8,3,6,0.92)';
    roundRect(g, x-bw/2, badgeY-bh/2, bw, bh, 12, true, false);
    g.strokeStyle = `rgba(${colA},${isHot?1:0.9})`;
    g.lineWidth = 1.1;
    roundRect(g, x-bw/2, badgeY-bh/2, bw, bh, 12, false, true);
    g.fillStyle = `rgba(${colA},1)`;
    g.beginPath(); g.arc(x-bw/2+9, badgeY, 2.8, 0, Math.PI*2); g.fill();
    g.fillStyle = '#fff';
    g.font = "bold 11px 'JetBrains Mono', monospace";
    g.fillText(String(c.nodes), x+5, badgeY);
    // Label below
    const ly = y + r + 20;
    g.font = "13px 'JetBrains Mono', monospace";
    g.fillStyle = 'rgba(8,3,6,0.85)';
    const t1w = g.measureText(c.label.toUpperCase()).width;
    g.fillRect(x-t1w/2-7, ly-9, t1w+14, 17);
    g.fillStyle = `rgba(${colA},${isHot?1:0.95})`;
    g.fillText(c.label.toUpperCase(), x, ly);
    g.font = "11px 'Inter', sans-serif";
    g.fillStyle = FG_DIM;
    g.fillText(c.sub || '', x, ly+16);
    g.font = "10px 'JetBrains Mono', monospace";
    g.fillStyle = FG_DIM;
    g.fillText((c.nodes||0) + ' nodes in cluster', x, ly+34);
  }
}
function roundRect(g,x,y,w,h,r,fill,stroke){
  g.beginPath();
  g.moveTo(x+r,y);
  g.lineTo(x+w-r,y); g.quadraticCurveTo(x+w,y,x+w,y+r);
  g.lineTo(x+w,y+h-r); g.quadraticCurveTo(x+w,y+h,x+w-r,y+h);
  g.lineTo(x+r,y+h); g.quadraticCurveTo(x,y+h,x,y+h-r);
  g.lineTo(x,y+r); g.quadraticCurveTo(x,y,x+r,y);
  g.closePath();
  if (fill) g.fill();
  if (stroke) g.stroke();
}
function hexToRgb(hex){
  const h = hex.replace('#','');
  return `${parseInt(h.slice(0,2),16)},${parseInt(h.slice(2,4),16)},${parseInt(h.slice(4,6),16)}`;
}

// ----- Interaction ----------------------------------------------
let hoverCat = null;
function catAt(x,y){
  for (const c of CATS){
    const dx = x-c.x, dy = y-c.y;
    if (dx*dx+dy*dy <= (c.r+8)*(c.r+8)) return c;
  }
  return null;
}
const SubBubbles = { focusCat:null, items:[], orbits:[], hoverIdx:-1 };
function layoutSubBubbles(){
  if (!SubBubbles.focusCat){ SubBubbles.orbits=[]; return; }
  const cat = SubBubbles.focusCat;
  const items = SubBubbles.items;
  if (!items.length){ SubBubbles.orbits=[]; return; }
  const sp = window.Cloud3D?.getBubbleScreenPos ? window.Cloud3D.getBubbleScreenPos(cat.id) : null;
  const cx = sp ? sp.x : cat.x;
  const cy = sp ? sp.y : cat.y;
  const ringR = Math.min(W,H) * 0.32;
  const N = Math.min(items.length, 12);
  SubBubbles.orbits = [];
  for (let i=0;i<N;i++){
    const t = i/N;
    const ang = -Math.PI/2 + t*Math.PI*2;
    const r = 30 + (items[i].importance||3) * 3;
    SubBubbles.orbits.push({
      x: cx + Math.cos(ang)*ringR,
      y: cy + Math.sin(ang)*ringR,
      r, item: items[i], ang, phase: Math.random()*Math.PI*2,
    });
  }
}
function drawSubBubbles(t){
  const g = ctx.cloud;
  layoutSubBubbles();
  if (!SubBubbles.orbits.length) return;
  const cat = SubBubbles.focusCat;
  const sp = window.Cloud3D?.getBubbleScreenPos ? window.Cloud3D.getBubbleScreenPos(cat.id) : null;
  const cx = sp ? sp.x : cat.x;
  const cy = sp ? sp.y : cat.y;
  const colA = hexToRgb(cat.color || RED);
  for (const ob of SubBubbles.orbits){
    g.strokeStyle = `rgba(${colA},0.20)`;
    g.lineWidth = 0.7;
    g.beginPath(); g.moveTo(cx,cy); g.lineTo(ob.x,ob.y); g.stroke();
  }
  SubBubbles.orbits.forEach((ob, i) => {
    const isHot = SubBubbles.hoverIdx === i;
    const breath = 1 + 0.07*Math.sin(t*0.002 + ob.phase);
    const r = ob.r * breath * (isHot?1.25:1);
    const hg = g.createRadialGradient(ob.x,ob.y,0,ob.x,ob.y,r*3);
    hg.addColorStop(0, `rgba(${colA},${isHot?0.55:0.32})`);
    hg.addColorStop(1, `rgba(${colA},0)`);
    g.fillStyle = hg; g.beginPath(); g.arc(ob.x,ob.y,r*3,0,Math.PI*2); g.fill();
    const body = g.createRadialGradient(ob.x-r*0.3,ob.y-r*0.3,r*0.1,ob.x,ob.y,r);
    body.addColorStop(0, `rgba(255,255,255,${isHot?0.65:0.35})`);
    body.addColorStop(0.6, `rgba(${colA},${isHot?0.85:0.55})`);
    body.addColorStop(1, `rgba(${colA},0.20)`);
    g.fillStyle = body; g.beginPath(); g.arc(ob.x,ob.y,r,0,Math.PI*2); g.fill();
    g.strokeStyle = `rgba(${colA},${isHot?1:0.85})`;
    g.lineWidth = isHot?1.6:1.0;
    g.beginPath(); g.arc(ob.x,ob.y,r,0,Math.PI*2); g.stroke();
    // hover preview
    if (isHot){
      const text = (ob.item.preview||'').slice(0,80) + ((ob.item.preview||'').length>80?'…':'');
      g.font = "12px 'Inter', sans-serif";
      g.textAlign = 'center'; g.textBaseline = 'top';
      const tw = g.measureText(text).width;
      g.fillStyle = 'rgba(8,3,6,0.94)';
      g.fillRect(ob.x-tw/2-9, ob.y+r+10, tw+18, 42);
      g.strokeStyle = `rgba(${colA},0.7)`;
      g.lineWidth = 0.9;
      g.strokeRect(ob.x-tw/2-9, ob.y+r+10, tw+18, 42);
      g.fillStyle = FG;
      g.fillText(text, ob.x, ob.y+r+18);
      g.font = "10px 'JetBrains Mono', monospace";
      g.fillStyle = `rgba(${colA},0.95)`;
      g.fillText(((ob.item.written_by||'').replace('-claude','').toUpperCase() + ' · imp ' + (ob.item.importance||3)).trim(), ob.x, ob.y+r+34);
    }
  });
}

function subAt(x,y){
  for (let i=SubBubbles.orbits.length-1;i>=0;i--){
    const ob = SubBubbles.orbits[i];
    const dx = x-ob.x, dy = y-ob.y;
    if (dx*dx+dy*dy <= (ob.r+6)*(ob.r+6)) return i;
  }
  return -1;
}

// ----- Click / hover ---------------------------------------------
cv.cloud3d_target = document.getElementById('cloud3d');
cv.cloud3d_target.addEventListener('mousemove', (e) => {
  if (SubBubbles.orbits.length){
    const idx = subAt(e.clientX, e.clientY);
    SubBubbles.hoverIdx = idx;
    if (idx >= 0){ cv.cloud3d_target.style.cursor = 'pointer'; return; }
  }
  const c = catAt(e.clientX, e.clientY);
  hoverCat = c ? c.slug : null;
  cv.cloud3d_target.style.cursor = c ? 'pointer' : 'default';
  if (window.Cloud3D?.setHover) window.Cloud3D.setHover(hoverCat);
  const tip = document.getElementById('tip');
  if (c){
    tip.innerHTML = `<div class="head">${c.slug}</div><div class="body">${c.label}<br>${c.sub||''}<br>${c.nodes||0} nodes</div>`;
    tip.style.left = e.clientX + 'px'; tip.style.top = e.clientY + 'px';
    tip.classList.add('show');
  } else { tip.classList.remove('show'); }
});
cv.cloud3d_target.addEventListener('click', (e) => {
  if (SubBubbles.orbits.length){
    const idx = subAt(e.clientX, e.clientY);
    if (idx >= 0){ openDrawer(SubBubbles.orbits[idx].item.id); return; }
  }
  const c = catAt(e.clientX, e.clientY);
  if (c) enterBubble(c);
});

function enterBubble(cat){
  document.body.classList.add('in-bubble');
  SubBubbles.focusCat = cat;
  SubBubbles.items = [];
  const endpoint = cat === CORE ? '/api/recent?n=12' : '/api/cluster/' + cat.slug;
  fetch(endpoint).then(r => r.ok ? r.json() : null).then(d => {
    if (!d) return;
    const items = (d.items != null) ? d.items : d;
    SubBubbles.items = (items || []).slice(0, 12).map(m => ({
      id: m.id,
      preview: m.preview || (m.content || '').slice(0, 110),
      written_by: m.written_by,
      importance: m.importance || 3,
    }));
    layoutSubBubbles();
  });
  if (window.Cloud3D?.focusBubble){
    window.Cloud3D.focusBubble(cat.id);
    window.Cloud3D.setSelected(cat.id);
  }
  updateBreadcrumb([
    { label: 'Universe', cb: exitBubble },
    { label: cat.label, cb: null },
  ]);
}
function exitBubble(){
  document.body.classList.remove('in-bubble');
  SubBubbles.focusCat = null;
  SubBubbles.items = [];
  SubBubbles.orbits = [];
  SubBubbles.hoverIdx = -1;
  if (window.Cloud3D?.unfocusBubble){
    window.Cloud3D.unfocusBubble();
    window.Cloud3D.setSelected(null);
  }
  updateBreadcrumb([{ label: 'Universe', cb: null }]);
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape'){
    if (document.getElementById('drawer').classList.contains('open')) closeDrawer();
    else if (SubBubbles.focusCat) exitBubble();
  }
});

// ----- Breadcrumb -----------------------------------------------
function updateBreadcrumb(segments){
  let el = document.getElementById('breadcrumb');
  if (!el){
    el = document.createElement('div');
    el.id = 'breadcrumb';
    document.body.appendChild(el);
  }
  el.innerHTML = '';
  segments.forEach((seg, i) => {
    if (i > 0){
      const sep = document.createElement('span');
      sep.className = 'crumb-sep'; sep.textContent = '›';
      el.appendChild(sep);
    }
    const item = document.createElement(seg.cb ? 'a' : 'span');
    item.className = 'crumb' + (seg.cb ? '' : ' final');
    item.textContent = seg.label;
    if (seg.cb){ item.addEventListener('click', seg.cb); item.href = 'javascript:void(0)'; }
    el.appendChild(item);
  });
}
updateBreadcrumb([{ label: 'Universe', cb: null }]);

// ----- Memory drawer --------------------------------------------
async function openDrawer(mid){
  const dr = document.getElementById('drawer');
  const body = document.getElementById('drawerBody');
  body.innerHTML = '<div class="kind">Memory · loading</div>';
  dr.classList.add('open'); dr.setAttribute('aria-hidden','false');
  try{
    const r = await fetch('/api/memory/' + mid);
    if (!r.ok) throw new Error('http '+r.status);
    const m = await r.json();
    const ents = (m.entity_slugs||[]).map(s => `<span class="tag">${esc(s)}</span>`).join('');
    const tags = (m.tags||[]).map(t => `<span class="tag">${esc(t)}</span>`).join('');
    body.innerHTML = `
      <div class="kind">Memory · imp ${m.importance}</div>
      <h2>${esc(truncate(m.content, 110))}${m.content.length>110?'…':''}</h2>
      <div class="meta">
        <span class="k">by</span><span class="v">${esc(m.written_by||'')}</span>
        <span class="k">at</span><span class="v">${esc(new Date(m.created_at).toLocaleString('en-CA',{hour12:false}))}</span>
        <span class="k">id</span><span class="v">${esc(m.id)}</span>
      </div>
      <div class="body">${esc(m.content)}</div>
      ${ents ? '<div class="tags">'+ents+'</div>' : ''}
      ${tags ? '<div class="tags" style="margin-top:8px">'+tags+'</div>' : ''}
    `;
  } catch(e){ body.innerHTML = '<div class="kind">Memory</div><div class="body">load failed</div>'; }
}
function closeDrawer(){ const dr = document.getElementById('drawer'); dr.classList.remove('open'); dr.setAttribute('aria-hidden','true'); }
document.getElementById('drawerClose').addEventListener('click', closeDrawer);
function truncate(s,n){ s = s||''; return s.length>n ? s.slice(0,n) : s; }
function esc(s){ if (s==null) return ''; return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }

// ----- Main loop -------------------------------------------------
let lastT = performance.now();
function frame(now){
  const dt = Math.min(now - lastT, 48);
  lastT = now;
  drawDust(dt, now);
  drawStars(dt, now);
  if (CATS.length){
    tickConstellation(dt);
    drawLabels();
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// ----- SSE  (light: just refresh data on activity)
function connectSSE(){
  const es = new EventSource('/events');
  es.addEventListener('activity', () => loadClusters());
  es.onerror = () => { es.close(); setTimeout(connectSSE, 3000); };
}

// Bootstrap
(async () => {
  await loadClusters();
  connectSSE();
  setInterval(loadClusters, 45000);
})();

})();
