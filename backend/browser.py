"""Headless Chromium (Playwright) for Component Lab: open the homepage, find the component the user means,
record how it moves, extract what it's made of, and render Claude's rebuild for comparison.

One shared browser; one browser context per job, kept open (in memory) until the job expires.
"""

import asyncio
import io
import ipaddress
import json
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

VIEWPORT = {"width": 1280, "height": 900}
SLICE_H = 1500          # page pixels per screenshot slice sent to Claude
SLICE_SCALE = 0.7       # slices are downscaled so Claude sees them at native resolution (~0.9 MP)
MAX_SLICES = 7          # only the top ~10,500px of the page is searched
FRAME_TIMES = [0, 0.5, 1.2, 2.5]  # seconds after the component comes into view
JOB_TTL = 20 * 60
MAX_LOTTIE_BYTES = 3_000_000  # Lottie JSON is inlined into the sandbox, so keep it bounded
MAX_LOTTIE_FILES = 3
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
TOOLS_JS = (Path(__file__).resolve().parent / "page_tools.js").read_text()


class BrowserError(Exception):
    pass


_pw = None
_browser = None
_lock = asyncio.Lock()
JOBS: dict[str, dict] = {}


async def _get_browser():
    global _pw, _browser
    async with _lock:
        if _browser is None or not _browser.is_connected():
            if _pw is None:
                _pw = await async_playwright().start()
            try:
                _browser = await _pw.chromium.launch()
            except PlaywrightError as e:
                raise BrowserError(f"Couldn't start Chromium ({e.message.splitlines()[0]}). "
                                   "Run: .venv/bin/playwright install chromium")
    return _browser


async def shutdown():
    global _pw, _browser
    for job_id in list(JOBS):
        await close_job(job_id)
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()
    _browser = _pw = None


def is_public_url(url: str) -> bool:
    """Block localhost / private-network targets: this browser runs on the server."""
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


# ---------- images ----------

def to_jpeg(img: Image.Image, quality: int = 82) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def normalize_upload(data: bytes) -> bytes:
    """User screenshot -> JPEG no bigger than Claude's native image size (long edge 1568, ~1.15 MP)."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise BrowserError("That file isn't an image we can read. Use a PNG or JPEG screenshot.")
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.split()[-1])
        img = bg
    scale = min(1.0, 1568 / max(img.size), (1_150_000 / (img.width * img.height)) ** 0.5)
    if scale < 1:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    return to_jpeg(img)


def _shot_to_jpeg(png: bytes) -> bytes:
    return to_jpeg(Image.open(io.BytesIO(png)))


# ---------- page helpers ----------

async def _goto(page, url: str):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except PlaywrightTimeout:
        raise BrowserError(f"{url} took too long to load in the browser.")
    except PlaywrightError as e:
        raise BrowserError(f"Couldn't open {url} in the browser: {e.message.splitlines()[0]}")
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except PlaywrightTimeout:
        pass  # sites with analytics/polling never go idle; carry on
    await page.evaluate(TOOLS_JS)
    await page.evaluate("() => window.__cl.hideConsentBanners()")


async def _preload_scroll(page):
    """Scroll through the page so lazy-loaded and scroll-revealed content renders, then return to the top."""
    await page.evaluate("""async () => {
        const step = innerHeight * 0.8;
        const max = Math.min(document.documentElement.scrollHeight, 12000);
        for (let y = 0; y < max; y += step) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 200)); }
        window.scrollTo(0, 0);
    }""")
    await asyncio.sleep(1.0)


def _clip(r: dict | None, vw: int, vh: int) -> dict | None:
    if not r:
        return None
    x, y = max(0, r["x"]), max(0, r["y"])
    x2, y2 = min(vw, r["x"] + r["w"]), min(vh, r["y"] + r["h"])
    if x2 - x < 4 or y2 - y < 4:
        return None
    return {"x": x, "y": y, "width": x2 - x, "height": y2 - y}


async def _frames(page, rect_js: str, vw: int, vh: int) -> tuple[list[bytes], bytes]:
    """Screenshots of an element at FRAME_TIMES, then one while hovering its centre."""
    frames = []
    t0 = time.monotonic()
    clip = None
    for t in FRAME_TIMES:
        await asyncio.sleep(max(0, t0 + t - time.monotonic()))
        clip = _clip(await page.evaluate(rect_js), vw, vh) or clip
        frames.append(_shot_to_jpeg(await page.screenshot(clip=clip, type="png")))
    hover = frames[-1]
    if clip:
        await page.mouse.move(clip["x"] + clip["width"] / 2, clip["y"] + clip["height"] / 2)
        await asyncio.sleep(0.8)
        hover = _shot_to_jpeg(await page.screenshot(clip=clip, type="png"))
        await page.mouse.move(0, 0)
    return frames, hover


# ---------- jobs ----------

async def _expire_jobs():
    now = time.time()
    for job_id, job in list(JOBS.items()):
        if now - job["created"] > JOB_TTL:
            await close_job(job_id)


async def close_job(job_id: str):
    job = JOBS.pop(job_id, None)
    if job:
        try:
            await job["context"].close()
        except PlaywrightError:
            pass


def get_job(job_id: str) -> dict | None:
    return JOBS.get(job_id)


async def open_job(url: str) -> dict:
    """Load the homepage and take the full-page screenshot used for locating the component."""
    await _expire_jobs()
    if not is_public_url(url):
        raise BrowserError("That address points to a private or local network, which Component Lab won't open.")
    browser = await _get_browser()
    context = await browser.new_context(viewport=VIEWPORT, device_scale_factor=1, bypass_csp=True,
                                        user_agent=USER_AGENT)
    job = {"id": uuid.uuid4().hex, "context": context, "url": url, "created": time.time(), "selection": None}
    try:
        page = await context.new_page()
        job["page"] = page
        await _goto(page, url)
        await _preload_scroll(page)
        await page.evaluate("() => window.__cl.hideConsentBanners()")  # some banners appear late
        height = await page.evaluate("() => document.documentElement.scrollHeight")
        height = max(VIEWPORT["height"], min(height, SLICE_H * MAX_SLICES))
        png = await page.screenshot(full_page=True, type="png",
                                    clip={"x": 0, "y": 0, "width": VIEWPORT["width"], "height": height})
    except BaseException:
        await context.close()
        raise

    full = Image.open(io.BytesIO(png)).convert("RGB")
    slices = []
    for top in range(0, full.height, SLICE_H):
        part = full.crop((0, top, full.width, min(full.height, top + SLICE_H)))
        part = part.resize((int(part.width * SLICE_SCALE), int(part.height * SLICE_SCALE)), Image.LANCZOS)
        slices.append({"top": top, "jpeg": to_jpeg(part, quality=78), "w": part.width, "h": part.height})
    job["full"] = full
    job["slices"] = slices
    JOBS[job["id"]] = job
    return job


async def pick(job: dict, box: dict) -> dict | None:
    """Select the element best matching `box` (page coords). Returns its info plus a crop for confirmation."""
    info = await job["page"].evaluate("box => window.__cl.pickByBox(box)", box)
    if not info:
        return None
    job["selection"] = info
    r, full = info["rect"], job["full"]
    left, top = max(0, int(r["x"])), max(0, int(r["y"]))
    right, bottom = min(full.width, int(r["x"] + r["w"])), min(full.height, int(r["y"] + min(r["h"], 1600)))
    crop = full.crop((left, top, right, bottom)) if right - left > 4 and bottom - top > 4 else full.crop((0, 0, 10, 10))
    info["crop"] = to_jpeg(crop)
    return info


async def capture(job: dict) -> dict:
    """Reload the page fresh, bring the chosen component into view, record its motion and extract it."""
    page, sel = job["page"], job["selection"]
    seen_lottie = []  # Lottie files are often loaded by JS, so watch the network as well as the DOM

    def on_response(resp):
        path = urlparse(resp.url).path.lower()
        if path.endswith((".json", ".lottie")) and resp.url not in seen_lottie:
            seen_lottie.append(resp.url)

    page.on("response", on_response)
    try:
        await _goto(page, job["url"])
    finally:
        page.remove_listener("response", on_response)
    found = await page.evaluate("s => window.__cl.findBySelector(s)", sel["selector"])
    if not found:  # DOM differs on reload (e.g. randomised ids): fall back to position matching
        await _preload_scroll(page)
        found = await page.evaluate("box => window.__cl.pickByBox(box)", sel["rect"])
        if not found:
            raise BrowserError("Couldn't find the component again after reloading the page.")
    await page.evaluate("() => window.__cl.scrollTargetIntoView()")
    frames, hover = await _frames(page, "() => window.__cl.viewportRect()", VIEWPORT["width"], VIEWPORT["height"])

    # Stylesheets from other origins (CDNs) can't be read in-page; fetch them here and parse them in-page.
    css = {}
    for href in (await page.evaluate("() => window.__cl.blockedSheets()"))[:8]:
        if not is_public_url(href):
            continue
        try:
            resp = await job["context"].request.get(href, timeout=10000)
            if resp.ok:
                css[href] = (await resp.text())[:400_000]
        except PlaywrightError:
            pass
    data = await page.evaluate("css => window.__cl.extract(css)", css)
    if not data:
        raise BrowserError("Lost track of the component while extracting it.")

    # Lottie: files laid over the component first, then other ones the page loaded.
    in_component = data["media"]["lottie"]
    candidates = list(dict.fromkeys(in_component + seen_lottie))
    assets, lottie_info = await _fetch_lottie(job["context"], candidates)
    for info in lottie_info:
        info["overlapsComponent"] = info["url"] in in_component
    data["lottieFiles"] = lottie_info
    return {"frames": frames, "hover": hover, "data": data, "assets": assets}


async def _fetch_lottie(context, urls: list[str]) -> tuple[dict, list[dict]]:
    """Download Lottie JSON files (the sandbox can't: most sites send no CORS headers)."""
    assets, info = {}, []
    for url in urls:
        if len(assets) >= MAX_LOTTIE_FILES:
            break
        if not url.lower().split("?")[0].endswith(".json") or not is_public_url(url):
            continue  # .lottie (zipped) files aren't supported by lottie-web
        try:
            resp = await context.request.get(url, timeout=15000)
            body = await resp.body() if resp.ok else b""
        except PlaywrightError:
            continue
        if not body or len(body) > MAX_LOTTIE_BYTES:
            continue
        try:
            anim = json.loads(body)
        except ValueError:
            continue
        if not isinstance(anim, dict) or "layers" not in anim or "op" not in anim:
            continue  # some other JSON, not a Lottie animation
        assets[url] = anim
        fr = anim.get("fr") or 30
        info.append({"url": url, "width": anim.get("w"), "height": anim.get("h"),
                     "seconds": round((anim.get("op", 0) - anim.get("ip", 0)) / fr, 2), "layers": len(anim["layers"]),
                     "embeddedImages": sum(1 for a in anim.get("assets", []) if str(a.get("p", "")).startswith("data:"))})
    return assets, info


def inject_assets(html: str, assets: dict) -> str:
    """Pre-load fetched files as window.__clAssets[url], ahead of the component's own scripts."""
    if not assets:
        return html
    payload = json.dumps(assets, separators=(",", ":")).replace("</", "<\\/")
    tag = f"<script>window.__clAssets={payload};</script>"
    lower = html.lower()
    i = lower.find("<head")
    if i != -1:
        j = lower.find(">", i) + 1
        return html[:j] + tag + html[j:]
    return tag + html


async def render(job: dict, html: str, width: float, assets: dict | None = None) -> dict:
    """Render a rebuilt component like the sandbox would and capture the same frames as the original."""
    vw = int(max(360, min(VIEWPORT["width"], round(width))))
    page = await job["context"].new_page()
    await page.set_viewport_size({"width": vw, "height": VIEWPORT["height"]})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)[:300]))
    try:
        try:
            await page.set_content(inject_assets(html, assets or {}), wait_until="load", timeout=20000)
        except PlaywrightTimeout:
            pass
        rect_js = """() => {
            const el = document.getElementById('component-root') || document.body;
            const r = el.getBoundingClientRect();
            return { x: r.left, y: r.top, w: r.width, h: r.height };
        }"""
        frames, hover = await _frames(page, rect_js, vw, VIEWPORT["height"])
    finally:
        await page.close()
    return {"frames": frames, "hover": hover, "errors": errors[:5]}
