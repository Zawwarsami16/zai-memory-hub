// ZAI Memory Hub — WebGL bubble cloud, bloom postprocessing, GSAP camera.
// Loaded as ES module from index.  Co-operates with the rest of the dashboard
// (which lives in the IIFE inside the HTML) through window.Cloud3D.
//
// Expectation: a <canvas id="cloud3d"></canvas> exists at full viewport size,
// stacked above the 2D layers (dust, stars) but below all DOM HUD chrome.

import * as THREE from 'three';
import { EffectComposer }   from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass }       from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass }  from 'three/addons/postprocessing/UnrealBloomPass.js';
import { ShaderPass }       from 'three/addons/postprocessing/ShaderPass.js';
import { SMAAPass }         from 'three/addons/postprocessing/SMAAPass.js';
import { OutputPass }       from 'three/addons/postprocessing/OutputPass.js';

// ---- Bubble shader -----------------------------------------------
// Fresnel-rim sphere with subtle internal flow and time-varying glow.
const BUBBLE_VS = /* glsl */`
varying vec3 vNormal;
varying vec3 vViewDir;
varying vec3 vPos;
void main(){
  vNormal = normalize(normalMatrix * normal);
  vec4 worldPos = modelMatrix * vec4(position, 1.0);
  vec4 mvPos = viewMatrix * worldPos;
  vViewDir = normalize(-mvPos.xyz);
  vPos = position;
  gl_Position = projectionMatrix * mvPos;
}
`;

const BUBBLE_FS = /* glsl */`
uniform vec3  uColor;
uniform vec3  uColor2;
uniform float uTime;
uniform float uHover;
uniform float uSelected;
uniform float uIntensity;
uniform sampler2D uMap;
uniform float uHasMap;
varying vec3 vNormal;
varying vec3 vViewDir;
varying vec3 vPos;

float hash(vec3 p){ return fract(sin(dot(p, vec3(127.1,311.7,74.7))) * 43758.5453); }
float vnoise(vec3 p){
  vec3 i = floor(p), f = fract(p);
  f = f*f*(3.0-2.0*f);
  return mix(
    mix(mix(hash(i),hash(i+vec3(1,0,0)),f.x), mix(hash(i+vec3(0,1,0)),hash(i+vec3(1,1,0)),f.x), f.y),
    mix(mix(hash(i+vec3(0,0,1)),hash(i+vec3(1,0,1)),f.x), mix(hash(i+vec3(0,1,1)),hash(i+vec3(1,1,1)),f.x), f.y),
    f.z);
}

void main(){
  // Fresnel: 0 at front, 1 at silhouette
  float fres = pow(1.0 - clamp(dot(vNormal, vViewDir), 0.0, 1.0), 2.0);
  // Texture disc projection — view-space normal X/Y => 2D UV
  // vNormal.z > 0 means front-facing; we fade the texture on the side.
  vec2 discUv = vNormal.xy * 0.42 + 0.5;
  // Add gentle drift/pulse to the UVs so the interior image breathes
  discUv += vec2(sin(uTime*0.13)*0.006, cos(uTime*0.17)*0.006);
  vec4 tex = texture2D(uMap, discUv);
  float frontMask = smoothstep(-0.05, 0.55, vNormal.z);
  // Interior color: if has-map mix the texture in, else fall back to noise
  float n  = vnoise(vPos * 2.5 + vec3(uTime*0.18, uTime*0.13, uTime*0.10));
  float n2 = vnoise(vPos * 5.0 - vec3(uTime*0.27, uTime*0.09, uTime*0.14));
  vec3 noisedInner = mix(uColor*0.55, uColor2*0.85, n) + uColor * n2 * 0.3;
  vec3 inner = mix(noisedInner, tex.rgb * 1.20 + uColor * 0.05, uHasMap * frontMask);
  // Rim glow over everything (lighter so the texture interior reads clearly)
  vec3 rim = mix(uColor, vec3(1.0,0.92,0.78), 0.30) * (0.95 + uHover*0.55) * fres;
  rim *= (1.0 + uSelected * 0.5);
  vec3 col = inner * (1.0 - fres*0.38) + rim;
  col *= uIntensity;
  float alpha = mix(0.80, 1.0, fres);
  gl_FragColor = vec4(col, alpha);
}
`;

// ---- Core (centerpiece) shader -- denser internal swirl + brighter
const CORE_FS = /* glsl */`
uniform vec3  uColor;
uniform vec3  uColor2;
uniform float uTime;
uniform float uHover;
uniform float uIntensity;
uniform sampler2D uMap;
uniform float uHasMap;
varying vec3 vNormal;
varying vec3 vViewDir;
varying vec3 vPos;

float hash(vec3 p){ return fract(sin(dot(p, vec3(127.1,311.7,74.7))) * 43758.5453); }
float vnoise(vec3 p){
  vec3 i = floor(p), f = fract(p);
  f = f*f*(3.0-2.0*f);
  return mix(
    mix(mix(hash(i),hash(i+vec3(1,0,0)),f.x), mix(hash(i+vec3(0,1,0)),hash(i+vec3(1,1,0)),f.x), f.y),
    mix(mix(hash(i+vec3(0,0,1)),hash(i+vec3(1,0,1)),f.x), mix(hash(i+vec3(0,1,1)),hash(i+vec3(1,1,1)),f.x), f.y),
    f.z);
}

void main(){
  float fres = pow(1.0 - clamp(dot(vNormal, vViewDir), 0.0, 1.0), 2.0);
  vec2 discUv = vNormal.xy * 0.42 + 0.5;
  discUv += vec2(sin(uTime*0.21)*0.008, cos(uTime*0.24)*0.008);
  vec4 tex = texture2D(uMap, discUv);
  float frontMask = smoothstep(-0.05, 0.55, vNormal.z);
  float pulse = 0.5 + 0.5 * sin(uTime * 1.2);
  float n1 = vnoise(vPos * 1.8 + vec3(uTime*0.32, uTime*0.21, uTime*0.18));
  float n2 = vnoise(vPos * 4.2 - vec3(uTime*0.40, uTime*0.18, uTime*0.27));
  vec3 noisedInner = mix(uColor*0.55, uColor2*0.85, n1) * (0.7 + 0.6*pulse) + uColor * n2 * 0.4;
  vec3 inner = mix(noisedInner, tex.rgb * 1.18 + uColor * 0.10, uHasMap * frontMask);
  vec3 rim = mix(uColor, vec3(1.0,0.95,0.82), 0.35) * (1.1 + uHover*0.4) * fres;
  vec3 col = inner * (1.0 - fres*0.40) + rim;
  col *= uIntensity;
  float alpha = mix(0.86, 1.0, fres);
  gl_FragColor = vec4(col, alpha);
}
`;

// ---- Spoke / line additive shader
const LINE_FS = /* glsl */`
uniform vec3 uColor;
uniform float uHot;
varying float vAlpha;
void main(){
  gl_FragColor = vec4(uColor * (1.5 + uHot*0.8), vAlpha);
}
`;
const LINE_VS = /* glsl */`
attribute float aAlpha;
varying float vAlpha;
void main(){
  vAlpha = aAlpha;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`;

// ---- Module ------------------------------------------------------
export async function initCloud3D(canvas, opts = {}){
  const renderer = new THREE.WebGLRenderer({
    canvas,
    antialias: false,            // SMAA in postprocess handles it
    alpha: true,
    powerPreference: 'high-performance',
  });
  renderer.setClearColor(0x000000, 0);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;

  // ---- Texture loader: category interior images become bubble textures
  const texLoader = new THREE.TextureLoader();
  function loadTex(url){
    return new Promise((resolve) => {
      texLoader.load(url, (t) => {
        t.colorSpace = THREE.SRGBColorSpace;
        t.minFilter = THREE.LinearMipmapLinearFilter;
        t.magFilter = THREE.LinearFilter;
        t.anisotropy = 4;
        resolve(t);
      }, undefined, () => resolve(null));
    });
  }
  const TEX = {};
  const CAT_TEX_MAP = {
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
  // dummy 1x1 white for bubbles without a texture
  const DUMMY = new THREE.DataTexture(new Uint8Array([255,255,255,255]), 1, 1, THREE.RGBAFormat);
  DUMMY.needsUpdate = true;
  for (const [slug, url] of Object.entries(CAT_TEX_MAP)){
    TEX[slug] = await loadTex(url);
  }

  const scene = new THREE.Scene();
  // Orthographic camera mapped 1:1 to screen pixels makes it easy to
  // place bubbles where the rest of the 2D HUD already expects them.
  const cam = new THREE.OrthographicCamera(-1, 1, 1, -1, -2000, 2000);
  cam.position.z = 600;
  cam.lookAt(0, 0, 0);

  // ---- bubble pool
  const View3D = { active: false };
  const bubbles = new Map();   // slug -> { mesh, mat, target, lastR }
  let CORE_REF = null;
  function makeBubble(slug, color, radius, isCore){
    const colorVec  = new THREE.Color(color);
    const color2Vec = new THREE.Color(isCore ? '#ffaa70' : color).offsetHSL(0, -0.15, 0.05);
    const tex = TEX[slug] || null;
    const uniforms = {
      uColor: { value: new THREE.Vector3(colorVec.r, colorVec.g, colorVec.b) },
      uColor2:{ value: new THREE.Vector3(color2Vec.r, color2Vec.g, color2Vec.b) },
      uTime:  { value: 0 },
      uHover: { value: 0 },
      uSelected: { value: 0 },
      uIntensity: { value: 1.0 },
      uMap:   { value: tex || DUMMY },
      uHasMap:{ value: tex ? 1.0 : 0.0 },
    };
    const geo = new THREE.SphereGeometry(radius, 64, 48);
    const mat = new THREE.ShaderMaterial({
      vertexShader: BUBBLE_VS,
      fragmentShader: isCore ? CORE_FS : BUBBLE_FS,
      uniforms,
      transparent: true,
      depthWrite: false,
      blending: THREE.NormalBlending,
    });
    const mesh = new THREE.Mesh(geo, mat);
    mesh.userData = { slug, uniforms, isCore, radius };
    scene.add(mesh);
    return mesh;
  }

  // ---- spokes (lines from CORE to each bubble)
  const spokeGroup = new THREE.Group();
  scene.add(spokeGroup);
  function rebuildSpokes(catList, coreState){
    while (spokeGroup.children.length){
      const c = spokeGroup.children.pop();
      c.geometry?.dispose(); c.material?.dispose();
    }
    if (!coreState) return;
    for (const c of catList){
      const colorVec = new THREE.Color(c.color);
      // Cylindrical line with multiple passes for glow — easier as a TubeGeometry
      const ax = new THREE.Vector3(coreState.x, coreState.y, 0);
      const bx = new THREE.Vector3(c.x, c.y, 0);
      // We'll update positions each frame because bubbles wobble
      const positions = new Float32Array(6);
      positions[0] = ax.x; positions[1] = ax.y; positions[2] = 0;
      positions[3] = bx.x; positions[4] = bx.y; positions[5] = 0;
      const alphas = new Float32Array([0.85, 0.45]);  // brighter near core
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
      geo.setAttribute('aAlpha', new THREE.BufferAttribute(alphas, 1));
      const mat = new THREE.ShaderMaterial({
        vertexShader: LINE_VS,
        fragmentShader: LINE_FS,
        uniforms: {
          uColor: { value: new THREE.Vector3(colorVec.r, colorVec.g, colorVec.b) },
          uHot:   { value: 0 },
        },
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      });
      const line = new THREE.Line(geo, mat);
      line.userData = { slug: c.slug };
      spokeGroup.add(line);
    }
  }
  function refreshSpokePositions(coreState, catList){
    for (const line of spokeGroup.children){
      const c = catList.find(x => x.slug === line.userData.slug);
      if (!c) continue;
      const arr = line.geometry.attributes.position.array;
      arr[0] = coreState.x; arr[1] = coreState.y;
      arr[3] = c.x;         arr[4] = c.y;
      line.geometry.attributes.position.needsUpdate = true;
    }
  }

  // ---- Cross-chord arcs between adjacent categories (curved lines)
  // Stable definitions: bend factor + color picked once per arc, only
  // endpoint positions re-projected per frame.  This stops the cross-arc
  // pattern from flickering when we update during rotation.
  const arcGroup = new THREE.Group();
  scene.add(arcGroup);
  const arcDefs = [];   // { aSlug, bSlug, bend, useGold }
  function defineArcs(catList){
    arcDefs.length = 0;
    if (!catList || catList.length < 2) return;
    const N = catList.length;
    // Deterministic seeded pattern — each (i, off) pair gets a consistent
    // bend + color choice based on its indices, never re-randomized.
    function hash(i){ return Math.abs(Math.sin(i * 91.137) * 43758.5453) % 1; }
    let idx = 0;
    for (let i = 0; i < N; i++){
      for (let off = 1; off <= 3; off++){
        const j = (i + off) % N;
        const h = hash(idx++);
        arcDefs.push({
          aSlug: catList[i].slug, bSlug: catList[j].slug,
          bend:  0.35 + 0.20 * h,
          useGold: h < 0.5,
        });
      }
    }
  }
  function rebuildArcs(catList, coreState){
    // Re-projects existing arcDefs onto current bubble positions.
    // Creates meshes the first time, then just updates them.
    if (!arcDefs.length && catList && catList.length >= 2) defineArcs(catList);
    if (!arcDefs.length || !coreState) return;
    // Ensure we have one Line per arcDef
    while (arcGroup.children.length < arcDefs.length){
      const def = arcDefs[arcGroup.children.length];
      const positions = new Float32Array(41 * 3);
      const alphas    = new Float32Array(41);
      for (let k = 0; k < 41; k++){
        const u = k / 40;
        const sag = 1 - 4*(u-0.5)*(u-0.5);
        alphas[k] = 0.12 + 0.18 * sag;
      }
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
      geo.setAttribute('aAlpha', new THREE.BufferAttribute(alphas, 1));
      const col = def.useGold ? new THREE.Color('#e8d49a') : new THREE.Color('#dc2626');
      const mat = new THREE.ShaderMaterial({
        vertexShader: LINE_VS,
        fragmentShader: LINE_FS,
        uniforms: { uColor: { value: new THREE.Vector3(col.r, col.g, col.b) }, uHot: { value: 0 } },
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      });
      const line = new THREE.Line(geo, mat);
      line.userData = { def };
      arcGroup.add(line);
    }
    while (arcGroup.children.length > arcDefs.length){
      const c = arcGroup.children.pop();
      c.geometry?.dispose(); c.material?.dispose();
    }
    // Update every line's position attribute
    const byS = new Map();
    for (const c of catList) byS.set(c.slug, c);
    for (let i = 0; i < arcDefs.length; i++){
      const def = arcDefs[i];
      const a = byS.get(def.aSlug), b = byS.get(def.bSlug);
      if (!a || !b) continue;
      const line = arcGroup.children[i];
      const arr = line.geometry.attributes.position.array;
      const mx = (a.x + b.x)/2, my = (a.y + b.y)/2;
      const cpX = mx + (coreState.x - mx) * def.bend;
      const cpY = my + (coreState.y - my) * def.bend;
      const ax = a.x, ay = a.y, bx = b.x, by = b.y;
      for (let k = 0; k < 41; k++){
        const u = k / 40, oneMu = 1 - u;
        const x = oneMu*oneMu*ax + 2*oneMu*u*cpX + u*u*bx;
        const y = oneMu*oneMu*ay + 2*oneMu*u*cpY + u*u*by;
        arr[k*3]   = x;
        arr[k*3+1] = y;
        arr[k*3+2] = 0;
      }
      line.geometry.attributes.position.needsUpdate = true;
    }
  }

  // ---- Particle web (radial dots filling the orbit)
  let webPoints = null;
  function rebuildWeb(coreState, orbitR){
    if (webPoints){
      webPoints.geometry.dispose(); webPoints.material.dispose();
      scene.remove(webPoints);
    }
    const N = 220;
    const positions = new Float32Array(N*3);
    const angSpeed  = new Float32Array(N);
    const rads      = new Float32Array(N);
    const angs      = new Float32Array(N);
    const cols      = new Float32Array(N*3);
    const sizes     = new Float32Array(N);
    for (let i = 0; i < N; i++){
      angs[i] = Math.random()*Math.PI*2;
      rads[i] = orbitR * (0.08 + Math.random()*1.05);
      angSpeed[i] = (Math.random()-0.5) * 0.00012;
      const x = coreState.x + Math.cos(angs[i]) * rads[i];
      const y = coreState.y + Math.sin(angs[i]) * rads[i];
      positions[i*3] = x; positions[i*3+1] = y; positions[i*3+2] = 0;
      if (Math.random() < 0.65){ cols[i*3]=180/255; cols[i*3+1]=30/255; cols[i*3+2]=30/255; }
      else { cols[i*3]=190/255; cols[i*3+1]=170/255; cols[i*3+2]=120/255; }
      sizes[i] = 0.8 + Math.random()*1.8;
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geo.setAttribute('aColor',    new THREE.BufferAttribute(cols, 3));
    geo.setAttribute('aSize',     new THREE.BufferAttribute(sizes, 1));
    geo.userData = { angs, rads, angSpeed, coreState };

    const mat = new THREE.ShaderMaterial({
      uniforms: { uTime: { value: 0 } },
      vertexShader: /* glsl */`
        attribute vec3 aColor;
        attribute float aSize;
        varying vec3 vColor;
        void main(){
          vColor = aColor;
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
          gl_PointSize = aSize * 2.0;
        }
      `,
      fragmentShader: /* glsl */`
        varying vec3 vColor;
        void main(){
          vec2 c = gl_PointCoord - 0.5;
          float d = length(c) * 2.0;
          float a = smoothstep(1.0, 0.0, d);
          gl_FragColor = vec4(vColor * (0.9 + a*0.4), a*0.45);
        }
      `,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    webPoints = new THREE.Points(geo, mat);
    scene.add(webPoints);
  }
  function tickWeb(){
    if (!webPoints) return;
    const { angs, rads, angSpeed, coreState } = webPoints.geometry.userData;
    const arr = webPoints.geometry.attributes.position.array;
    const N = angs.length;
    for (let i = 0; i < N; i++){
      angs[i] += angSpeed[i];
      arr[i*3]   = coreState.x + Math.cos(angs[i]) * rads[i];
      arr[i*3+1] = coreState.y + Math.sin(angs[i]) * rads[i];
    }
    webPoints.geometry.attributes.position.needsUpdate = true;
  }

  // ---- Flow particles (continuous bead travel on spokes)
  const flow = [];
  function spawnFlow(catList, coreState){
    if (!catList.length) return;
    const c = catList[(Math.random()*catList.length)|0];
    const inward = Math.random() < 0.4;
    flow.push({ cat: c, t: 0, life: 1200+Math.random()*900, inward });
  }
  let flowMesh = null, flowMax = 80;
  function rebuildFlowMesh(){
    if (flowMesh){
      flowMesh.geometry.dispose(); flowMesh.material.dispose();
      scene.remove(flowMesh);
    }
    const positions = new Float32Array(flowMax*3);
    const colors    = new Float32Array(flowMax*3);
    const sizes     = new Float32Array(flowMax);
    const alphas    = new Float32Array(flowMax);
    for (let i = 0; i < flowMax; i++){ alphas[i] = 0; sizes[i] = 0; }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geo.setAttribute('aColor', new THREE.BufferAttribute(colors, 3));
    geo.setAttribute('aSize',  new THREE.BufferAttribute(sizes, 1));
    geo.setAttribute('aAlpha', new THREE.BufferAttribute(alphas, 1));
    const mat = new THREE.ShaderMaterial({
      uniforms: {},
      vertexShader: /* glsl */`
        attribute vec3 aColor; attribute float aSize; attribute float aAlpha;
        varying vec3 vColor; varying float vAlpha;
        void main(){
          vColor = aColor; vAlpha = aAlpha;
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
          gl_PointSize = aSize * 2.6;
        }
      `,
      fragmentShader: /* glsl */`
        varying vec3 vColor; varying float vAlpha;
        void main(){
          vec2 c = gl_PointCoord - 0.5;
          float d = length(c) * 2.0;
          float a = smoothstep(1.0, 0.0, d) * vAlpha;
          gl_FragColor = vec4(vColor * (1.4 + a*0.6), a);
        }
      `,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    flowMesh = new THREE.Points(geo, mat);
    scene.add(flowMesh);
  }
  function tickFlow(dt, coreState){
    if (!flowMesh) return;
    const arr  = flowMesh.geometry.attributes.position.array;
    const cols = flowMesh.geometry.attributes.aColor.array;
    const sz   = flowMesh.geometry.attributes.aSize.array;
    const alphas = flowMesh.geometry.attributes.aAlpha.array;
    for (let i = flow.length-1; i >= 0; i--){
      const p = flow[i]; p.t += dt;
      if (p.t >= p.life){ flow.splice(i,1); }
    }
    // Fill the buffer with current flows, the rest invisible
    for (let i = 0; i < flowMax; i++){
      if (i < flow.length){
        const p = flow[i];
        const pr = p.t / p.life;
        const e  = 1 - Math.pow(1 - pr, 2);
        const from = p.inward ? p.cat : coreState;
        const to   = p.inward ? coreState : p.cat;
        arr[i*3]   = from.x + (to.x - from.x)*e;
        arr[i*3+1] = from.y + (to.y - from.y)*e;
        arr[i*3+2] = 0;
        const col = new THREE.Color(p.cat.color);
        cols[i*3] = col.r; cols[i*3+1] = col.g; cols[i*3+2] = col.b;
        sz[i] = 3.0;
        let a = 1;
        if (pr < 0.1) a = pr * 10;
        else if (pr > 0.85) a = (1 - pr) * 7;
        alphas[i] = a;
      } else {
        alphas[i] = 0;
      }
    }
    flowMesh.geometry.attributes.position.needsUpdate = true;
    flowMesh.geometry.attributes.aColor.needsUpdate = true;
    flowMesh.geometry.attributes.aSize.needsUpdate  = true;
    flowMesh.geometry.attributes.aAlpha.needsUpdate = true;
  }

  // ---- Postprocessing chain  (bloom + SMAA + output)
  const composer = new EffectComposer(renderer);
  composer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  const renderPass = new RenderPass(scene, cam);
  composer.addPass(renderPass);
  const bloomPass = new UnrealBloomPass(new THREE.Vector2(1, 1), 0.9, 0.5, 0.35);
  bloomPass.threshold = 0.35;       // higher threshold so only bright rims bloom, not interiors
  bloomPass.strength  = 0.85;
  bloomPass.radius    = 0.55;
  composer.addPass(bloomPass);
  // chromatic aberration (cinematic edge fringe)
  const CHROMA = {
    uniforms: {
      tDiffuse: { value: null },
      uAmount:  { value: 0.0018 },
      uResolution: { value: new THREE.Vector2(1,1) },
    },
    vertexShader: /* glsl */`
      varying vec2 vUv;
      void main(){ vUv = uv; gl_Position = vec4(position, 1.0); }
    `,
    fragmentShader: /* glsl */`
      uniform sampler2D tDiffuse;
      uniform float uAmount;
      uniform vec2  uResolution;
      varying vec2 vUv;
      void main(){
        vec2 dir = vUv - vec2(0.5);
        float r = texture2D(tDiffuse, vUv + dir * uAmount).r;
        float g = texture2D(tDiffuse, vUv                ).g;
        float b = texture2D(tDiffuse, vUv - dir * uAmount).b;
        float a = texture2D(tDiffuse, vUv).a;
        gl_FragColor = vec4(r,g,b,a);
      }
    `,
  };
  const chromaPass = new ShaderPass(CHROMA);
  composer.addPass(chromaPass);
  // vignette
  const VIGNETTE = {
    uniforms: { tDiffuse:{value:null}, uAmt:{value: 0.22}, uSoft:{value: 0.85} },
    vertexShader: /* glsl */`varying vec2 vUv; void main(){ vUv=uv; gl_Position=vec4(position,1.0); }`,
    fragmentShader: /* glsl */`
      uniform sampler2D tDiffuse; uniform float uAmt, uSoft; varying vec2 vUv;
      void main(){
        vec4 c = texture2D(tDiffuse, vUv);
        float d = distance(vUv, vec2(0.5)) / 0.7071;
        float v = smoothstep(uSoft, 1.0, d);
        c.rgb *= 1.0 - v * uAmt;
        gl_FragColor = c;
      }
    `,
  };
  composer.addPass(new ShaderPass(VIGNETTE));
  // film grain — subtle modulated noise so it doesn't look digital
  const GRAIN = {
    uniforms: { tDiffuse:{value:null}, uTime:{value:0}, uAmt:{value:0.05} },
    vertexShader: /* glsl */`varying vec2 vUv; void main(){ vUv=uv; gl_Position=vec4(position,1.0); }`,
    fragmentShader: /* glsl */`
      uniform sampler2D tDiffuse; uniform float uTime; uniform float uAmt; varying vec2 vUv;
      float rnd(vec2 p){ return fract(sin(dot(p, vec2(12.9898,78.233))) * 43758.5453); }
      void main(){
        vec4 c = texture2D(tDiffuse, vUv);
        float n = rnd(vUv * 1024.0 + uTime) - 0.5;
        // luminance-modulated: less grain in pure dark, more in midtones
        float lum = dot(c.rgb, vec3(0.299, 0.587, 0.114));
        float mod = smoothstep(0.04, 0.55, lum) * (1.0 - smoothstep(0.85, 1.0, lum));
        c.rgb += vec3(n) * uAmt * mod;
        gl_FragColor = c;
      }
    `,
  };
  const grainPass = new ShaderPass(GRAIN);
  composer.addPass(grainPass);
  const smaa = new SMAAPass();
  composer.addPass(smaa);
  composer.addPass(new OutputPass());

  // ---- Resize logic
  function resize(){
    const w = window.innerWidth, h = window.innerHeight;
    renderer.setSize(w, h, false);
    composer.setSize(w, h);
    bloomPass.setSize(w, h);
    smaa.setSize(w, h);
    chromaPass.uniforms.uResolution.value.set(w, h);
    // Orthographic camera in pixel coords, origin at center
    cam.left = -w/2; cam.right = w/2; cam.top = h/2; cam.bottom = -h/2;
    cam.updateProjectionMatrix();
  }
  window.addEventListener('resize', resize);
  resize();

  // ---- Public API
  const API = {
    scene, camera: cam, renderer, composer, bloomPass, chromaPass,

    setBubbles(catList, coreState){
      // Remove old bubbles not in new list
      const present = new Set([coreState.id, ...catList.map(c => c.id)]);
      for (const [slug, info] of Array.from(bubbles)){
        if (!present.has(slug)){ scene.remove(info.mesh); info.mesh.geometry.dispose(); info.mesh.material.dispose(); bubbles.delete(slug); }
      }
      // Create / update bubbles
      function ensure(state, isCore){
        let info = bubbles.get(state.id);
        if (!info){
          const mesh = makeBubble(state.id, state.color, state.r || 30, isCore);
          info = { mesh, target: { x: state.x, y: state.y, r: state.r } };
          bubbles.set(state.id, info);
        }
        info.target = { x: state.x, y: state.y, r: state.r };
        info.mesh.position.set(state.x - window.innerWidth/2, -(state.y - window.innerHeight/2), 0);
        // (camera is centered on screen origin; we offset positions to match)
        // Scale instead of rebuilding geometry
        const scale = (state.r || 30) / (info.mesh.userData.radius || 30);
        info.mesh.scale.setScalar(scale);
      }
      ensure(coreState, true);
      CORE_REF = coreState;
      for (const c of catList) ensure(c, false);
      // Update spokes + arcs + web
      rebuildSpokes(catList.map(c => ({...c, x: c.x - window.innerWidth/2, y: -(c.y - window.innerHeight/2)})),
                    {x: coreState.x - window.innerWidth/2, y: -(coreState.y - window.innerHeight/2)});
      rebuildArcs(catList.map(c => ({...c, x: c.x - window.innerWidth/2, y: -(c.y - window.innerHeight/2)})),
                  {x: coreState.x - window.innerWidth/2, y: -(coreState.y - window.innerHeight/2)});
      const orbitR = Math.hypot((catList[0]?.x || 0) - coreState.x, (catList[0]?.y || 0) - coreState.y);
      rebuildWeb({x: coreState.x - window.innerWidth/2, y: -(coreState.y - window.innerHeight/2)}, orbitR);
      rebuildFlowMesh();
    },

    // Lightweight per-frame position update: mesh.position + spoke endpoints.
    // Cross-arcs need a separate rotation update because they sag toward CORE.
    updatePositions(catList, coreState){
      const sx = window.innerWidth/2, sy = window.innerHeight/2;
      const cx = coreState.x - sx, cy = -(coreState.y - sy);
      // Move bubble meshes
      for (const c of catList){
        const info = bubbles.get(c.id);
        if (!info) continue;
        info.mesh.position.set(c.x - sx, -(c.y - sy), 0);
      }
      const coreInfo = bubbles.get(coreState.id);
      if (coreInfo) coreInfo.mesh.position.set(cx, cy, 0);
      // Move spoke endpoints
      for (const line of spokeGroup.children){
        const c = catList.find(x => x.slug === line.userData.slug);
        if (!c) continue;
        const arr = line.geometry.attributes.position.array;
        arr[0] = cx; arr[1] = cy;
        arr[3] = c.x - sx; arr[4] = -(c.y - sy);
        line.geometry.attributes.position.needsUpdate = true;
      }
      // Update web particles' anchor so radial dots follow CORE
      if (webPoints){
        webPoints.geometry.userData.coreState.x = cx;
        webPoints.geometry.userData.coreState.y = cy;
      }
    },

    // Rebuild cross-chord arcs when rotation accumulates enough.
    rebuildArcsFor(catList, coreState){
      const sx = window.innerWidth/2, sy = window.innerHeight/2;
      const projected = catList.map(c => ({...c, x: c.x - sx, y: -(c.y - sy), color: c.color}));
      const proj = { x: coreState.x - sx, y: -(coreState.y - sy) };
      rebuildArcs(projected, proj);
    },

    // Parallax: smoothly offset the whole 3D group by a small vector
    setParallax(dx, dy){
      cam.position.x = dx;
      cam.position.y = dy;
      cam.updateProjectionMatrix();
    },

    // 3D MODE: tilt the entire constellation group around the X axis so
    //          it reads as a ring viewed from above + a depth perturbation
    //          per bubble so they overlap.  Yaw rotates around Y.
    setView3D(active){
      View3D.active = active;
      if (!active){
        scene.rotation.set(0, 0, 0);
        // restore bubble z
        for (const [_, info] of bubbles){
          if (info.mesh.userData.isCore) continue;
          info.mesh.position.z = 0;
        }
      } else {
        // give each non-core bubble a stable z perturbation
        let i = 0;
        for (const [_, info] of bubbles){
          if (info.mesh.userData.isCore) continue;
          info.mesh.position.z = Math.sin(i * 2.3) * 80;
          i++;
        }
        // initial tilt
        scene.rotation.x = -0.35;
      }
    },
    setView3DAngles(tilt, yaw){
      if (!View3D.active) return;
      scene.rotation.x = -0.35 + tilt;
      scene.rotation.y = yaw;
    },

    // Project a bubble's current world position back into screen pixel
    // coords (accounts for scene rotation in 3D mode, camera parallax, etc).
    // Returns null if the bubble doesn't exist or is behind camera.
    getBubbleScreenPos(id){
      const info = bubbles.get(id);
      if (!info) return null;
      const v = new THREE.Vector3();
      info.mesh.getWorldPosition(v);
      v.project(cam);
      return {
        x: (v.x + 1) * 0.5 * window.innerWidth,
        y: (-v.y + 1) * 0.5 * window.innerHeight,
        scale: 1.0 / Math.max(0.1, 1 - (v.z * 0.4)),  // closer = bigger label
      };
    },

    setHover(slug){
      for (const [s, info] of bubbles){
        info.mesh.userData.uniforms.uHover.value = (s === slug) ? 1.0 : 0.0;
      }
      // Boost spoke matching the hovered bubble
      for (const line of spokeGroup.children){
        line.material.uniforms.uHot.value = (line.userData.slug === slug) ? 1.0 : 0.0;
      }
    },

    setSelected(slug){
      for (const [s, info] of bubbles){
        info.mesh.userData.uniforms.uSelected.value = (s === slug) ? 1.0 : 0.0;
      }
    },

    spawnFlowFor(catList, coreState){
      // accept positions in screen px, convert to centered
      const c = catList[(Math.random()*catList.length)|0];
      if (!c) return;
      flow.push({
        cat: { ...c, x: c.x - window.innerWidth/2, y: -(c.y - window.innerHeight/2), color: c.color },
        t: 0, life: 1200+Math.random()*900,
        inward: Math.random() < 0.4,
      });
    },

    raycast(clientX, clientY){
      // Convert client (x,y) to centered, find which bubble (in screen px)
      const x = clientX - window.innerWidth/2;
      const y = -(clientY - window.innerHeight/2);
      // pick the bubble whose center is closest within scaled radius
      let best = null, bestD = Infinity;
      for (const [s, info] of bubbles){
        const dx = x - info.mesh.position.x;
        const dy = y - info.mesh.position.y;
        const rEff = (info.mesh.userData.radius * info.mesh.scale.x) + 6;
        const d = dx*dx + dy*dy;
        if (d <= rEff*rEff && d < bestD){ bestD = d; best = s; }
      }
      return best;
    },

    cameraZoomTo(targetWorldXY, scaleDuration = 0.55){
      // GSAP optional; fall back to immediate if not available
      const gsap = window.gsap;
      const tx = targetWorldXY.x - window.innerWidth/2;
      const ty = -(targetWorldXY.y - window.innerHeight/2);
      if (gsap){
        gsap.to(cam, { duration: scaleDuration, ease: 'power3.inOut',
          onUpdate: () => {},
        });
        gsap.to(cam.position, { duration: scaleDuration, x: tx, y: ty, ease: 'power3.inOut',
          onUpdate: () => { cam.lookAt(tx, ty, 0); cam.updateProjectionMatrix(); } });
        // Zoom in by tightening orthographic frustum via cam.zoom
        gsap.to(cam, { duration: scaleDuration, zoom: 2.4, ease: 'power3.inOut',
          onUpdate: () => cam.updateProjectionMatrix() });
        gsap.to(bloomPass, { duration: scaleDuration, strength: 2.1, radius: 0.85, ease: 'power3.inOut' });
        gsap.to(chromaPass.uniforms.uAmount, { duration: scaleDuration*0.6, value: 0.005, ease: 'power3.inOut', yoyo: true, repeat: 1 });
      } else {
        cam.position.set(tx, ty, cam.position.z);
        cam.zoom = 2.4; cam.updateProjectionMatrix();
      }
    },
    cameraReset(duration = 0.55){
      const gsap = window.gsap;
      if (gsap){
        gsap.to(cam.position, { duration, x: 0, y: 0, ease: 'power3.inOut',
          onUpdate: () => { cam.lookAt(0,0,0); cam.updateProjectionMatrix(); } });
        gsap.to(cam, { duration, zoom: 1.0, ease: 'power3.inOut',
          onUpdate: () => cam.updateProjectionMatrix() });
        gsap.to(bloomPass, { duration, strength: 0.85, radius: 0.55, ease: 'power3.inOut' });
      } else {
        cam.position.set(0,0,cam.position.z);
        cam.zoom = 1; cam.updateProjectionMatrix();
      }
    },

    // FOCUS BUBBLE — true cinematic dolly: target bubble scales up to
    // fill the stage while every other bubble, the web, and the spokes
    // fade to near-zero opacity.  Camera moves to the target.  Use this
    // to give the impression you've actually flown INTO a category
    // before the zoom card slides in on top.
    focusBubble(id){
      const target = bubbles.get(id);
      if (!target) return;
      const gsap = window.gsap;
      const tx = target.mesh.position.x;
      const ty = target.mesh.position.y;
      if (gsap){
        // Tuned for legibility — was 3.4 zoom + 2.4 scale + 1.9 bloom
        // (compounded too brightly, washed the image out).
        gsap.to(cam.position, { duration: 0.7, x: tx, y: ty, ease: 'power3.inOut',
          onUpdate: () => { cam.lookAt(tx, ty, 0); cam.updateProjectionMatrix(); } });
        gsap.to(cam, { duration: 0.7, zoom: 2.0, ease: 'power3.inOut',
          onUpdate: () => cam.updateProjectionMatrix() });
        gsap.to(bloomPass, { duration: 0.7, strength: 1.05, radius: 0.65, ease: 'power3.inOut' });
        // Also drop the target's own intensity slightly so the texture
        // reads instead of glowing white-hot
        gsap.to(target.mesh.material.uniforms.uIntensity, { duration: 0.7, value: 0.92, ease: 'power3.inOut' });
        gsap.to(target.mesh.scale, { duration: 0.7, x: 1.6, y: 1.6, z: 1.6, ease: 'power3.inOut' });
        // Fade everything else
        for (const [slug, info] of bubbles){
          if (slug === id) continue;
          gsap.to(info.mesh.material.uniforms.uIntensity, { duration: 0.5, value: 0.20, ease: 'power2.in' });
        }
        if (webPoints) gsap.to(webPoints.material, { duration: 0.5, opacity: 0.18, ease: 'power2.in' });
        spokeGroup.children.forEach(line => gsap.to(line.material, { duration: 0.5, opacity: 0.14, ease: 'power2.in' }));
        arcGroup.children.forEach(line => gsap.to(line.material, { duration: 0.5, opacity: 0.10, ease: 'power2.in' }));
      } else {
        cam.position.set(tx, ty, cam.position.z);
        cam.zoom = 2.0; cam.updateProjectionMatrix();
        target.mesh.scale.setScalar(1.6);
      }
    },
    unfocusBubble(){
      const gsap = window.gsap;
      if (gsap){
        gsap.to(cam.position, { duration: 0.6, x: 0, y: 0, ease: 'power3.inOut',
          onUpdate: () => { cam.lookAt(0,0,0); cam.updateProjectionMatrix(); } });
        gsap.to(cam, { duration: 0.6, zoom: 1.0, ease: 'power3.inOut',
          onUpdate: () => cam.updateProjectionMatrix() });
        gsap.to(bloomPass, { duration: 0.6, strength: 0.85, radius: 0.55, ease: 'power3.inOut' });
        for (const [_, info] of bubbles){
          gsap.to(info.mesh.scale, { duration: 0.6, x: 1, y: 1, z: 1, ease: 'power3.inOut' });
          gsap.to(info.mesh.material.uniforms.uIntensity, { duration: 0.6, value: 1.0, ease: 'power3.out' });
          gsap.to(info.mesh.material.uniforms.uSelected, { duration: 0.4, value: 0.0, ease: 'power3.out' });
        }
        if (webPoints) gsap.to(webPoints.material, { duration: 0.6, opacity: 1, ease: 'power3.out' });
        spokeGroup.children.forEach(line => gsap.to(line.material, { duration: 0.6, opacity: 1, ease: 'power3.out' }));
        arcGroup.children.forEach(line => gsap.to(line.material, { duration: 0.6, opacity: 1, ease: 'power3.out' }));
      } else {
        cam.position.set(0,0,cam.position.z);
        cam.zoom = 1; cam.updateProjectionMatrix();
        for (const [_, info] of bubbles) info.mesh.scale.setScalar(1);
      }
    },
  };

  // ---- Frame loop
  let lastT = performance.now(), lastFlow = 0;
  function tick(now){
    const dt = Math.min(now - lastT, 48); lastT = now;
    // Update uniforms
    for (const [_, info] of bubbles){
      info.mesh.userData.uniforms.uTime.value = now * 0.001;
    }
    grainPass.uniforms.uTime.value = now * 0.001;
    // Web rotation
    tickWeb();
    // Flow particles
    if (CORE_REF && now - lastFlow > 180){
      lastFlow = now;
      API.spawnFlowFor(window.__zai?.CATS || [], CORE_REF);
    }
    tickFlow(dt, CORE_REF ? { x: CORE_REF.x - window.innerWidth/2, y: -(CORE_REF.y - window.innerHeight/2) } : { x: 0, y: 0 });
    composer.render();
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  return API;
}
