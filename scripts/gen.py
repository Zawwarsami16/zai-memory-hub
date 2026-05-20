#!/usr/bin/env python3
"""Replicate asset generator for the ZAI Memory Hub dashboard.

Usage:
  ./gen.py all        -- generate the standard ZAI asset set
  ./gen.py <id> "prompt"   -- generate a single image with id (saved as id.jpg)
  ./gen.py list       -- list jobs sent in this run
Spend gets appended to scripts/replicate-spend.log.
"""
import json, os, sys, time, urllib.request, urllib.error, urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = Path(os.environ.get("REPLICATE_TOKEN_PATH", "./auth/replicate.token"))
OUT = ROOT / "dashboard" / "static" / "gen"
LOG = ROOT / "scripts" / "replicate-spend.log"
OUT.mkdir(parents=True, exist_ok=True)

if not TOKEN_PATH.exists():
    print("ERROR: token missing at", TOKEN_PATH, file=sys.stderr); sys.exit(1)
TOKEN = TOKEN_PATH.read_text().strip()

# Flux 1.1 Pro pricing: ~$0.04 per image at 1024
# Flux Schnell: ~$0.003 per image — much cheaper, lower quality
# We default to Flux 1.1 Pro for hero assets, Schnell for quick tests
MODEL_PRO     = "black-forest-labs/flux-1.1-pro"
MODEL_SCHNELL = "black-forest-labs/flux-schnell"
MODEL_DEV     = "black-forest-labs/flux-dev"

PRICE = {  # USD approx, used for spend log
    MODEL_PRO:     0.040,
    MODEL_SCHNELL: 0.003,
    MODEL_DEV:     0.025,
}

def api(method, path, body=None):
    url = "https://api.replicate.com" + path
    data = None
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Prefer": "wait=30",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise SystemExit(f"HTTP {e.code}: {body}")


def generate(slug, prompt, model=MODEL_PRO, aspect="1:1", seed=None, guidance=3.0):
    """Submit a generation, wait for completion, save image to OUT/slug.jpg."""
    print(f"[gen] {slug:<22} model={model}  ar={aspect}", flush=True)
    input_blob = {"prompt": prompt, "aspect_ratio": aspect, "output_format": "jpg", "safety_tolerance": 5}
    if model == MODEL_PRO:
        input_blob["prompt_upsampling"] = False
        input_blob["output_quality"] = 92
    elif model == MODEL_SCHNELL:
        input_blob["num_outputs"] = 1
        input_blob["go_fast"] = True
    elif model == MODEL_DEV:
        input_blob["guidance"] = guidance
    if seed is not None:
        input_blob["seed"] = seed

    pred = api("POST", f"/v1/models/{model}/predictions", {"input": input_blob})
    # If still in progress, poll
    while pred.get("status") in ("starting", "processing"):
        time.sleep(2)
        pred = api("GET", f"/v1/predictions/{pred['id']}")
    status = pred.get("status")
    if status != "succeeded":
        print(f"  FAILED: status={status}, error={pred.get('error')}")
        return None

    out = pred.get("output")
    if isinstance(out, list):
        out = out[0] if out else None
    if not out:
        print("  no output URL"); return None

    # Download the image
    dest = OUT / f"{slug}.jpg"
    with urllib.request.urlopen(out, timeout=120) as r, open(dest, "wb") as f:
        f.write(r.read())
    sz = dest.stat().st_size
    print(f"  ok   {dest.name}  {sz/1024:.0f} KB")
    cost = PRICE.get(model, 0)
    with open(LOG, "a") as lf:
        lf.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  {model:<35}  ${cost:>5.3f}  {slug:<24}  {sz/1024:.0f}KB\n")
    return str(dest)


# ===== ASSET SET =================================================
# Style anchors used across every prompt for a coherent look.
STYLE_ANCHOR = (
    "cinematic photorealistic render, deep crimson red and dark gold palette, "
    "Hubble Space Telescope astrophotography lighting, dramatic rim glow, "
    "high detail, no text, no watermark, no logos, no people, centered subject, "
    "studio quality, premium tech brand aesthetic, dark background"
)

LIBRARY_VIDEOS = [
    # Hero loop for the Living Memory home page header
    ("lib_video_hero", (
        "Cinematic looping shot of an open leather-bound book on a dark "
        "wooden desk, single warm amber candle flickering on the left, "
        "dust motes drifting slowly through warm light, pages occasionally "
        "turning by themselves, deep crimson nebula faintly visible "
        "through an arched window in the background, atmospheric, "
        "premium editorial photography, no text on the pages readable, "
        "no people, 5 second seamless loop, slow gentle motion, "
        "warm cream and crimson palette."
    ), "16:9"),
    # Block hover micro-loops — same aspect, very short, subtle motion
    ("loop_hacking", (
        "Vintage green-phosphor CRT terminal screen in a dark room, "
        "lines of glowing cyan code cascade slowly downward, soft scanline "
        "flicker, no readable text just abstract symbols, dim crimson "
        "ambient light on the side, atmospheric, photographic close-up, "
        "no people, seamless 3 second loop, slow downward motion."
    ), "16:9"),
    ("loop_philosophy", (
        "Close-up of an open leather-bound book on a dark desk, "
        "pages gently turning by themselves one by one in warm "
        "amber candlelight, dust motes drifting through the light, "
        "no readable text, photographic editorial close-up, deep "
        "crimson and cream palette, seamless 3 second loop, slow motion."
    ), "16:9"),
    ("loop_crypto", (
        "Abstract liquidity grid visualization, vertical columns of "
        "glowing crimson and amber price ladders pulsing with subtle "
        "data flow, dark background, no readable numbers, futuristic "
        "trading terminal aesthetic, photographic, seamless 3 second "
        "loop, slow upward and downward pulse."
    ), "16:9"),
    ("loop_infra", (
        "Close-up of a dark server rack with rows of small red and "
        "amber status LEDs breathing slowly in and out, fiber optic "
        "cables glowing dimly with data flow, dark deep crimson ambient "
        "lighting, photographic, no readable text, seamless 3 second "
        "loop, slow breathing motion."
    ), "16:9"),
]

LIBRARY_ASSETS = [
    ("lib_hero_today", (
        "Open editorial reading-room hero photograph — a single warm "
        "amber pool of light falling on a leather-bound notebook resting "
        "on a dark wooden desk, faint crimson nebula glow in the deep "
        "background, photographic editorial quality, premium magazine "
        "feel, no people, no readable text, cinematic composition, "
        "depth of field, 16:9. Warm cream and deep crimson palette."
    ), "16:9"),
    ("lib_hero_archive", (
        "Photograph of an infinite library shelf vanishing into deep "
        "crimson nebula in the upper background, leather-bound volumes "
        "glowing with subtle inner light, single warm amber lamp on the "
        "left third, atmospheric depth, editorial magazine aesthetic, "
        "no people, no readable text, premium cinematic, 16:9."
    ), "16:9"),
    ("lib_hero_window", (
        "Photograph of a single arched window opening onto a vast "
        "crimson nebula, faint warm interior light suggests a quiet "
        "study room facing infinity, editorial photographic quality, "
        "no people, no text, premium, contemplative, 16:9."
    ), "16:9"),
]

SITE_ASSETS = [
    ("site_oracle", (
        "Macro intelligence terminal control room visualized as a cathedral of "
        "data — colossal floating holographic charts of geopolitical crises, "
        "currency flows, central bank rates suspended in dark crimson void, "
        "rendered in a single bone-white and deep-crimson palette, "
        "cinematic photographic render, futuristic command center aesthetic, "
        "no actual readable text, abstract data architecture, "
        "16:9 hero composition, no people."
    ), "16:9"),
    ("site_world_model", (
        "Deep-time data lattice — a vast 150-year horizontal timeline of glowing "
        "amber and crimson nodes connected by faint silver threads, abstract "
        "regression curves draped through space, a single small ember-orange "
        "earth planet floating mid-frame, dark backdrop, photographic render, "
        "cinematic historical scope, no text."
    ), "16:9"),
    ("site_crypto_terminal", (
        "A vast crimson and gold liquidity grid — vertical price ladders, "
        "abstract orderbook depth canyons, glowing limit-order rivulets cascading "
        "downward, structural geometry of a financial market visualized as "
        "architecture in dark space, photographic render, no text, no candlestick "
        "charts, abstract structure aware visualization."
    ), "16:9"),
    ("site_zai_genesis", (
        "An opinionated reasoning lattice — a cathedral-scale network of "
        "interlocking glowing geometric chambers each containing a tiny "
        "crimson ember of light, suggesting an AI mind built room by room, "
        "deep red and obsidian palette, photographic cinematic render, no text, "
        "no humans, abstract architecture of cognition."
    ), "16:9"),
    ("site_hackers_legacy", (
        "Noir hacker CRT scene — a single vintage green-phosphor monitor "
        "glowing in a dark room, walls covered in faded printouts and circuit "
        "diagrams, single warm desk lamp throwing crimson side light, "
        "1980s underground aesthetic, photographic, atmospheric, no text "
        "readable on screen, cinematic, no people visible."
    ), "16:9"),
    ("site_about_portrait", (
        "Abstract premium portrait silhouette suggesting a thoughtful builder, "
        "head and shoulders cropped on the left third of the frame, deep crimson "
        "nebula bleeding in from the right, profile rim-lit dramatically in red "
        "and amber, photographic editorial portrait quality, dark moody, "
        "no facial features visible (silhouette only), cinematic, premium tech "
        "personality brand."
    ), "3:4"),
    ("site_og_template", (
        "Premium OG card backdrop — dark crimson nebula with a single bright "
        "core in the upper right, no text, no logos, plenty of negative space "
        "at center-left for typography to be added on top, cinematic, "
        "ultra premium tech brand aesthetic, photographic Hubble astrophotography "
        "feel, 1.91:1 social media card composition."
    ), "16:9"),
]

ASSETS = [
    # (slug, prompt, aspect)
    ("planet", (
        "A single isolated photorealistic Mars-like rocky planet, deep crimson and "
        "ember-orange surface with intricate canyons, craters, dust storms, and "
        "iron-red terrain detail, dramatic side lighting from the right creating a "
        "bright glowing rim around the silhouette, pure pitch-black space behind, "
        "no stars, no other planets, no atmosphere haze, square composition, "
        f"the planet fills 70% of the frame. {STYLE_ANCHOR}"
    ), "1:1"),
    ("zai_lockup", (
        "Premium tech brand emblem: the three letters 'ZAI' carved into glowing "
        "dark obsidian stone tablet, edges molten with crimson lava and gold heat, "
        "engraved Cinzel-style elegant serif lettering, intricate detail, soft "
        "godrays of red light emanating outward, set against deep crimson nebula "
        f"backdrop, ultra premium product hero shot. {STYLE_ANCHOR}"
    ), "16:9"),
    ("cat_coding", (
        "Abstract glowing terminal screen suspended in dark space, cascading lines "
        "of crimson red holographic code drifting upward, geometric data lattice, "
        "neon red glow, futuristic IDE aesthetic, no human, no actual text, "
        f"pure abstract data visualization. {STYLE_ANCHOR}"
    ), "1:1"),
    ("cat_web", (
        "Abstract holographic globe of light, sapphire blue glowing meridians and "
        "data connection arcs wrapping around it, soft blue plasma trails, dark "
        f"crimson nebula background, futuristic network visualization. {STYLE_ANCHOR}"
    ), "1:1"),
    ("cat_mobile", (
        "Abstract holographic floating smartphone silhouette made of emerald "
        "green light particles, soft green data streams flowing in and out, "
        f"deep dark space backdrop, futuristic UI aesthetic. {STYLE_ANCHOR}"
    ), "1:1"),
    ("cat_github", (
        "Abstract cosmic tree of branching golden light, dozens of glowing "
        "commit-nodes connected by curving paths, crimson and gold palette, "
        f"version-control as a sacred geometry, dark deep-space backdrop. {STYLE_ANCHOR}"
    ), "1:1"),
    ("cat_agents", (
        "Abstract ethereal humanoid figures made entirely of violet-purple light "
        "particles, three glowing entities suspended in dark space, their forms "
        "blurred and translucent, deep blue-purple glow surrounding them, "
        f"futuristic AI mind aesthetic. {STYLE_ANCHOR}"
    ), "1:1"),
    ("cat_planning", (
        "Abstract constellation map of glowing amber-orange node-points connected "
        "by faint dotted lines, geometric blueprint overlay, deep crimson "
        f"background, sacred architectural diagram of goals. {STYLE_ANCHOR}"
    ), "1:1"),
    ("cat_longterm", (
        "Abstract ancient glowing crystal artifact suspended in dark space, "
        "deep crimson red core with internal light pulses, fractal facets, "
        f"memory-stone aesthetic, intricate geological detail. {STYLE_ANCHOR}"
    ), "1:1"),
    ("cat_terminal", (
        "Abstract vertical streams of cyan-blue holographic data cascading "
        "in dark space, terminal command-line aesthetic, no actual text, "
        f"futuristic monitoring grid, deep ocean-blue glow. {STYLE_ANCHOR}"
    ), "1:1"),
    ("actor_vps", (
        "Abstract ethereal AI entity face emerging from crimson nebula gas, "
        "made of swirling red and gold light particles, no human features, "
        "more like a faceless mask of glowing geometry, ancient and powerful "
        f"presence, side-lit dramatically. {STYLE_ANCHOR}"
    ), "1:1"),
    ("actor_local", (
        "Abstract ethereal AI entity emerging from cool blue-violet nebula, "
        "made of swirling crystalline geometry, faceless cosmic intelligence, "
        f"dramatic rim light, deep space backdrop. {STYLE_ANCHOR}"
    ), "1:1"),
    ("actor_chat", (
        "Abstract ethereal AI entity made of pale gold filaments and crimson "
        "energy threads, weaving themselves into an abstract face form, "
        f"luminescent, faceless cosmic intelligence. {STYLE_ANCHOR}"
    ), "1:1"),
]


VIDEO_MODELS = {
    # Pricing approximate per ~5 second clip
    "hunyuan":  ("tencent/hunyuan-video",       2.20),
    "wan-fast": ("wavespeedai/wan-2.1-t2v-480p", 0.40),
    "ltx":      ("lightricks/ltx-video",        0.20),
}


def generate_video(slug, prompt, model_key="wan-fast", aspect="16:9", duration=5):
    """Submit a text-to-video prediction.  Returns local path to mp4."""
    if model_key not in VIDEO_MODELS:
        raise SystemExit(f"unknown video model: {model_key}")
    model, price = VIDEO_MODELS[model_key]
    print(f"[vid] {slug:<22} model={model}  ar={aspect}  ~${price:.2f}", flush=True)
    if model_key == "hunyuan":
        body = {
            "prompt": prompt,
            "video_length": 85,    # ~3.5s at 24fps
            "width": 1280, "height": 720,
            "infer_steps": 30,
        }
    elif model_key == "wan-fast":
        body = {
            "prompt": prompt,
            "frame_num": 81,       # ~3.4s @ 24fps for 480p
            "aspect_ratio": aspect,
        }
    else:  # ltx
        body = {"prompt": prompt, "length": duration}
    try:
        pred = api("POST", f"/v1/models/{model}/predictions", {"input": body})
    except SystemExit as e:
        print(f"  submit failed: {e}")
        return None
    while pred.get("status") in ("starting", "processing"):
        time.sleep(5)
        pred = api("GET", f"/v1/predictions/{pred['id']}")
    if pred.get("status") != "succeeded":
        print(f"  FAILED: status={pred.get('status')} error={pred.get('error')}")
        return None
    out = pred.get("output")
    if isinstance(out, list): out = out[0] if out else None
    if not out:
        print("  no output URL"); return None
    dest = OUT / f"{slug}.mp4"
    with urllib.request.urlopen(out, timeout=300) as r, open(dest, "wb") as f:
        f.write(r.read())
    sz = dest.stat().st_size
    print(f"  ok   {dest.name}  {sz/1024:.0f} KB")
    with open(LOG, "a") as lf:
        lf.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  {model:<40}  ${price:>5.2f}  {slug:<24}  {sz/1024:.0f}KB\n")
    return str(dest)


def all_library_videos():
    """Generate all library video assets — used for the hero loop etc."""
    for slug, prompt, aspect in LIBRARY_VIDEOS:
        if (OUT / f"{slug}.mp4").exists():
            print(f"[skip] {slug}.mp4 already exists"); continue
        # Try mid-tier first (wan-fast: ~$0.40 each), fall back to LTX if it fails
        result = generate_video(slug, prompt, model_key="wan-fast", aspect=aspect)
        if not result:
            print("  wan failed, retrying with ltx")
            result = generate_video(slug, prompt, model_key="ltx", aspect=aspect)
        time.sleep(15)


def all_library_assets():
    todo = LIBRARY_ASSETS
    total = 0.0; done = 0; PACE = 12
    for slug, prompt, aspect in todo:
        if (OUT / f"{slug}.jpg").exists():
            print(f"[skip] {slug} already exists"); continue
        ok = None
        for attempt in range(6):
            try:
                ok = generate(slug, prompt, model=MODEL_PRO, aspect=aspect); break
            except SystemExit as e:
                if '429' in str(e) or 'throttled' in str(e).lower():
                    wait = 15 + attempt * 5
                    print(f"  rate-limited, waiting {wait}s"); time.sleep(wait); continue
                raise
        if ok: done += 1; total += PRICE[MODEL_PRO]
        time.sleep(PACE)
    print(f"\n=== library: {done} new image(s), spend: ${total:.3f} ===")


def all_site_assets():
    todo = SITE_ASSETS
    total = 0.0
    done = 0
    PACE = 12
    for slug, prompt, aspect in todo:
        if (OUT / f"{slug}.jpg").exists():
            print(f"[skip] {slug} already exists")
            continue
        ok = None
        for attempt in range(6):
            try:
                ok = generate(slug, prompt, model=MODEL_PRO, aspect=aspect)
                break
            except SystemExit as e:
                msg = str(e)
                if '429' in msg or 'throttled' in msg.lower():
                    wait = 15 + attempt * 5
                    print(f"  rate-limited, waiting {wait}s")
                    time.sleep(wait); continue
                raise
        if ok:
            done += 1; total += PRICE[MODEL_PRO]
        time.sleep(PACE)
    print(f"\n=== site summary: {done} new image(s), spend: ${total:.3f} ===")


def all_assets():
    # Replicate throttles to 6/min + burst 1 when account credit < $5.
    # That means: one prediction in flight at a time, ~11s between starts.
    todo = ASSETS
    total = 0.0
    done = 0
    PACE = 12  # seconds between generation starts
    for slug, prompt, aspect in todo:
        if (OUT / f"{slug}.jpg").exists():
            print(f"[skip] {slug} already exists")
            continue
        ok = None
        # Retry on 429 with backoff
        for attempt in range(6):
            try:
                ok = generate(slug, prompt, model=MODEL_PRO, aspect=aspect)
                break
            except SystemExit as e:
                msg = str(e)
                if '429' in msg or 'throttled' in msg.lower():
                    wait = 15 + attempt * 5
                    print(f"  rate-limited, waiting {wait}s")
                    time.sleep(wait)
                    continue
                raise
        if ok:
            done += 1
            total += PRICE[MODEL_PRO]
        time.sleep(PACE)
    print(f"\n=== summary: {done} new image(s), approx spend this run: ${total:.3f} ===")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    cmd = sys.argv[1]
    if cmd == "all":
        all_assets()
    elif cmd == "site":
        all_site_assets()
    elif cmd == "library":
        all_library_assets()
    elif cmd == "library-vid":
        all_library_videos()
    elif cmd == "list":
        for f in sorted(OUT.iterdir()):
            print(f.name, f.stat().st_size, "bytes")
    elif cmd == "log":
        if LOG.exists():
            print(LOG.read_text())
        else:
            print("no spend log yet")
    elif len(sys.argv) >= 3:
        slug = sys.argv[1]
        prompt = sys.argv[2]
        generate(slug, prompt + ". " + STYLE_ANCHOR, model=MODEL_PRO)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
