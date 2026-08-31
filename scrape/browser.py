"""Real-Chrome layer: Cloudflare bypass, network interception, and the
token-bound CDN intercept-and-download path. Everything that needs an
actual browser lives here.

Browser priority:
  1. DrissionPage (headless stealth) — fastest, no visible window
  2. DrissionPage (headed) — fallback if headless is fingerprinted
  3. Playwright (headless) — fallback if DrissionPage fails entirely
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from urllib.parse import unquote

from . import config
from .config import (UA, YTDLP_TIMEOUT, FFMPEG_TIMEOUT, MIN_MB, ffmpeg_hdr_block,
                      AD_BLOCK_CDP_PATTERNS, is_ad_domain)
from .patterns import MEDIA_RE, SKIP_DOMAINS_RE, IFRAME_RE, IFRAME_SKIP_RE, SOURCES_RE
from .extractors import find_m3u8_in_packed_js
from .ui import cprint, cprint_url
from .ytdlp import ffmpeg_ok, ytdlp_ok, yt_fmt_args, build_ytdlp_generic_cmd, run_ytdlp_rainbow
from .logging_setup import debug_event
from .downloader import safe_filename

import logging
_cf_log = logging.getLogger("CFBypass")

# Fingerprint patches for sites that block plain headless Chrome (navigator.webdriver,
# empty plugins list, etc). Applied via driver.add_init_js() after the page is created.
STEALTH_JS = """
(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    const makePlugin = (name, filename, desc, mimeTypes) => {
        const plugin = Object.create(Plugin.prototype);
        Object.defineProperty(plugin, 'name',        { get: () => name });
        Object.defineProperty(plugin, 'filename',    { get: () => filename });
        Object.defineProperty(plugin, 'description', { get: () => desc });
        Object.defineProperty(plugin, 'length',      { get: () => mimeTypes.length });
        mimeTypes.forEach((mt, i) => {
            const m = Object.create(MimeType.prototype);
            Object.defineProperty(m, 'type',        { get: () => mt.type });
            Object.defineProperty(m, 'suffixes',    { get: () => mt.suffixes });
            Object.defineProperty(m, 'description', { get: () => mt.desc });
            plugin[i] = m;
        });
        return plugin;
    };

    const fakePlugins = [
        makePlugin('PDF Viewer',        'internal-pdf-viewer',  'Portable Document Format', [
            { type: 'application/pdf', suffixes: 'pdf', desc: '' },
            { type: 'text/pdf',        suffixes: 'pdf', desc: '' },
        ]),
        makePlugin('Chrome PDF Viewer', 'internal-pdf-viewer',  'Portable Document Format', [
            { type: 'application/pdf', suffixes: 'pdf', desc: '' },
        ]),
        makePlugin('Chromium PDF Viewer','internal-pdf-viewer', 'Portable Document Format', [
            { type: 'application/pdf', suffixes: 'pdf', desc: '' },
        ]),
        makePlugin('Microsoft Edge PDF Viewer','internal-pdf-viewer','Portable Document Format',[
            { type: 'application/pdf', suffixes: 'pdf', desc: '' },
        ]),
        makePlugin('WebKit built-in PDF','internal-pdf-viewer', 'Portable Document Format', [
            { type: 'application/pdf', suffixes: 'pdf', desc: '' },
        ]),
    ];

    const pluginArray = Object.create(PluginArray.prototype);
    fakePlugins.forEach((p, i) => { pluginArray[i] = p; });
    Object.defineProperty(pluginArray, 'length', { get: () => fakePlugins.length });
    pluginArray.item      = (i) => fakePlugins[i] ?? null;
    pluginArray.namedItem = (n) => fakePlugins.find(p => p.name === n) ?? null;
    pluginArray.refresh   = () => {};
    Object.defineProperty(navigator, 'plugins', { get: () => pluginArray });

    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    if (!window.chrome) window.chrome = { runtime: {} };

    const origQuery = window.Permissions?.prototype?.query;
    if (origQuery) {
        window.Permissions.prototype.query = function(params) {
            if (params?.name === 'notifications')
                return Promise.resolve({ state: 'prompt', onchange: null });
            return origQuery.call(this, params);
        };
    }

    if (screen.width === 0) {
        Object.defineProperty(screen, 'width',       { get: () => 1920 });
        Object.defineProperty(screen, 'height',      { get: () => 1080 });
        Object.defineProperty(screen, 'availWidth',  { get: () => 1920 });
        Object.defineProperty(screen, 'availHeight', { get: () => 1040 });
    }
})();
"""

# Same patches for Playwright (injected via add_init_script)
_PLAYWRIGHT_STEALTH_JS = STEALTH_JS


def chrome_opts(headless: bool = True, stealth: bool = False):
    from DrissionPage import ChromiumOptions
    import sys as _sys, os as _os
    opts = ChromiumOptions()
    _in_container = (_sys.platform != "win32"
                     and (_os.path.exists("/.dockerenv")
                          or _os.environ.get("CI") == "true"))
    if _in_container:
        opts.set_argument("--no-sandbox")
    opts.set_argument("--disable-blink-features=AutomationControlled")
    opts.set_argument(f"--user-agent={UA}")
    opts.set_argument("--disable-infobars")
    opts.set_argument("--disable-extensions")
    if stealth:
        opts.set_argument("--window-size=1920,1080")
        opts.set_argument("--disable-dev-shm-usage")
        if headless:
            opts.set_argument("--headless=new")
        else:
            opts.headless(False)
    else:
        opts.headless(headless)
    return opts


def page_is_blocked(html: str) -> bool:
    """Heuristic: did the site serve us a blocked/empty page?"""
    if not html or len(html.strip()) < 500:
        return True
    lower = html.lower()
    markers = [
        "enable javascript",
        "you need to enable javascript",
        "access denied",
        "403 forbidden",
        "robot check",
        "are you a robot",
        "browser not supported",
        "suspicious activity",
    ]
    return any(m in lower for m in markers)


# ── Browser fetch (layer 2) ───────────────────────────────────────────────────
def _drission_fetch_with_opts(site: str, opts, stealth: bool = False) -> tuple[str | None, str | None, dict | None, str | None]:
    """Inner fetch — one DrissionPage Chrome session with the given options.

    Returns (html, video_url, cf_session, player_referer). player_referer is
    the iframe URL the media was actually found in/under (e.g. an embed
    player page), or None when the media was found on the top-level site
    itself. Callers should use player_referer (when set) as the Referer for
    any CDN request — the CDN commonly locks to the embed page, not the
    hosting site.
    """
    from DrissionPage import ChromiumPage
    captured = {"url": None}
    player_referer = None
    driver = ChromiumPage(addr_or_opts=opts)
    if stealth:
        driver.add_init_js(STEALTH_JS)
    listener = start_listener(driver)
    try:
        try:
            driver.set.blocked_urls(AD_BLOCK_CDP_PATTERNS)
        except Exception as e:
            _cf_log.debug('ad-block setup failed: %s', e)
        driver.get(site)
        time.sleep(3)
        cf_bypass(driver)

        try:
            driver.wait.load_complete(timeout=15)
        except Exception:
            pass
        time.sleep(1)

        # Check the already-loaded page HTML first — it's instant and, for
        # a lot of sites, the media URL is sitting right there in a <video>/
        # <source> tag or a packed-JS blob. Only fall through to the
        # network-listener poll (which can take up to its full timeout)
        # when the static HTML doesn't have the answer.
        html = driver.html
        m = MEDIA_RE.search(html) or SOURCES_RE.search(html)
        if m:
            captured["url"] = m.group(1) if m.lastindex else m.group(0)
            cprint_url("browser", "Found in HTML", captured["url"])
        if not captured["url"]:
            packed_url = find_m3u8_in_packed_js(html)
            if packed_url:
                captured["url"] = packed_url
                cprint_url("browser", "Found in packed JS", packed_url)

        if not captured["url"]:
            poll_listener(listener, captured, timeout=15)
            html = driver.html

        cf_session = get_cf_session(driver)
        if cf_session:
            _cf_log.info('CF session captured (%d cookies)', len(cf_session['cookies']))

        if not captured["url"]:
            for m in IFRAME_RE.finditer(html):
                src = m.group(1).strip()
                if not src.startswith("http") or IFRAME_SKIP_RE.search(src):
                    continue
                print(f"[browser] Checking iframe: {src}")
                driver.get(src)
                time.sleep(3)
                poll_listener(listener, captured, timeout=15)
                if captured["url"]:
                    player_referer = src
                    break
                fm = MEDIA_RE.search(driver.html)
                if fm:
                    captured["url"] = fm.group(0)
                    player_referer = src
                    cprint_url("browser", "Found in iframe HTML", captured["url"])
                    break

        return html, captured["url"], cf_session, player_referer

    except Exception as e:
        print(f"[browser] Error: {e}")
        return None, None, None, None
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def drission_fetch(site: str, out_fmt: str = "mp4") -> tuple:
    """Returns (html, video_url, cf_session, player_referer).

    Tries DrissionPage headless → headed → Playwright as successive fallbacks.
    cf_session is non-None when Chrome obtained a cf_clearance cookie.
    player_referer is the embed/iframe page the media was actually found in
    (None when found directly on the top-level site) — use it as the Referer
    for any CDN request instead of the top-level site when set.

    When Playwright captures a media URL behind a CF challenge it downloads
    in-session and returns PLAYWRIGHT_DOWNLOADED as the video_url sentinel —
    the pipeline must check for this and skip any further download attempts.
    """
    import importlib.util

    has_drission = importlib.util.find_spec("DrissionPage") is not None
    has_playwright = importlib.util.find_spec("playwright") is not None

    html, video_url, cf_session, player_referer = None, None, None, None

    if has_drission:
        print("[browser] Launching Chrome (headless, stealth)...")
        html, video_url, cf_session, player_referer = _drission_fetch_with_opts(
            site, chrome_opts(headless=True, stealth=True), stealth=True
        )

        if page_is_blocked(html) and video_url is None:
            cprint("[browser] Headless blocked — retrying in headed mode...", 208)
            html, video_url, cf_session, player_referer = _drission_fetch_with_opts(
                site, chrome_opts(headless=False, stealth=True), stealth=True
            )
    else:
        cprint("[browser] DrissionPage not installed — pip install DrissionPage", 196)

    # Playwright fallback: kick in when DrissionPage isn't installed or came back empty
    if (not has_drission or (page_is_blocked(html) and video_url is None)) and has_playwright:
        cprint("[browser] Falling back to Playwright...", 208)
        pw_html, pw_url, pw_session, pw_referer = playwright_fetch(site, out_fmt=out_fmt)
        if pw_html and not page_is_blocked(pw_html):
            html, video_url, cf_session, player_referer = pw_html, pw_url, pw_session, pw_referer
        elif pw_url == PLAYWRIGHT_DOWNLOADED:
            # Already downloaded in-session — propagate sentinel immediately
            return pw_html, PLAYWRIGHT_DOWNLOADED, pw_session, pw_referer
    elif not has_drission and not has_playwright:
        cprint("[browser] Neither DrissionPage nor Playwright installed — browser layer skipped", 196)

    return html, video_url, cf_session, player_referer


# ── Playwright fetch ──────────────────────────────────────────────────────────
def _playwright_launch_args() -> list[str]:
    import sys as _sys
    args = ["--disable-blink-features=AutomationControlled"]
    if _sys.platform != "win32" and (
        os.path.exists("/.dockerenv") or os.environ.get("CI") == "true"
    ):
        args.append("--no-sandbox")
    return args


def _playwright_download_in_session(page, cdn_url: str, referer: str,
                                     out_fmt: str) -> bool:
    """Download cdn_url via ffmpeg using headers extracted from the live
    Playwright session. Must be called before browser.close() so the
    session cookies/fingerprint are still valid for the CDN request.

    ffmpeg receives the cookies as a header string — same transport as the
    browser used, no TLS fingerprint mismatch.
    """
    if not ffmpeg_ok():
        cprint("[playwright] ffmpeg not found — cannot download in-session", 196)
        return False

    try:
        # Grab all cookies for the CDN domain and format as Cookie: header
        from urllib.parse import urlparse as _up
        cdn_domain = _up(cdn_url).netloc
        all_cookies = page.context.cookies()
        cookie_str = "; ".join(
            f"{c['name']}={c['value']}"
            for c in all_cookies
            if cdn_domain.endswith(c.get("domain", "").lstrip("."))
            or c.get("domain", "").lstrip(".") in cdn_domain
        )
    except Exception:
        cookie_str = ""

    os.makedirs(config.dir_for(out_fmt), exist_ok=True)
    target_ext = f".{out_fmt}" if out_fmt else ".mp4"
    out_path = safe_filename(cdn_url, 1, ext=target_ext)
    tmp = out_path + ".part" + target_ext

    # Build ffmpeg headers block — same origin/referer the browser used
    hdr = (
        f"User-Agent: {UA}\r\n"
        f"Referer: {referer}\r\n"
        f"Origin: {referer.split('/')[0]}//{referer.split('/')[2]}\r\n"
    )
    if cookie_str:
        hdr += f"Cookie: {cookie_str}\r\n"

    cmd = ["ffmpeg", "-y", "-headers", hdr, "-i", cdn_url, "-c", "copy", tmp]
    cprint(f"[playwright] Downloading via ffmpeg in-session → {out_path}", 46)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired:
        if os.path.exists(tmp):
            os.remove(tmp)
        cprint("[playwright] ffmpeg timed out", 196)
        return False

    if r.returncode == 0 and os.path.exists(tmp):
        size = os.path.getsize(tmp)
        if size >= MIN_MB * 1024 * 1024:
            os.replace(tmp, out_path)
            cprint(f"[playwright] SAVED: {out_path} ({size/1024/1024:.1f} MB)", 46)
            return True
        os.remove(tmp)
        cprint("[playwright] File too small — likely an error page", 196)
    else:
        err = r.stderr.decode(errors="replace").strip().splitlines()[-3:]
        cprint(f"[playwright] ffmpeg failed: {' | '.join(err)}", 196)
    return False


# Sentinel returned by drission_fetch when Playwright already completed the download.
PLAYWRIGHT_DOWNLOADED = "__playwright_downloaded__"


def playwright_fetch(site: str, out_fmt: str = "mp4") -> tuple[str | None, str | None, dict | None, str | None]:
    """Playwright-based fetch — headless Chromium with stealth patches.

    Used as a fallback when DrissionPage fails or isn't installed.

    Key difference from the DrissionPage path: when a media URL is captured
    AND the site had a CF challenge (meaning yt-dlp handoff would 403), we
    download inside the same browser session via ffmpeg before closing the
    browser. This avoids the TLS fingerprint mismatch that causes CF to reject
    the cookie when yt-dlp makes a plain urllib request with it.

    Returns (html, video_url, cf_session, player_referer). player_referer is
    the URL of the frame that actually served the captured media (e.g. an
    embed player iframe) when it differs from the top-level site — CDN
    tokens are frequently locked to that embed page, not the hosting site.

      (html, PLAYWRIGHT_DOWNLOADED, cf_session, player_referer) — downloaded in-session
      (html, video_url, cf_session, player_referer)             — media URL found, no CF
      (html, None, cf_session, None)                            — page loaded, no media found
      (None, None, None, None)                                  — hard failure
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        cprint("[playwright] Not installed — pip install playwright && playwright install chromium", 196)
        return None, None, None, None

    captured = {"url": None, "referer": None}

    def _on_response(response):
        """Listen on responses (not requests) so we get the final resolved URL
        after any redirects — important for HLS manifests that redirect."""
        url = response.url
        if SKIP_DOMAINS_RE.search(url):
            return
        if MEDIA_RE.search(url) and not captured["url"]:
            captured["url"] = url
            # The frame that actually issued the request — if this came from
            # an embedded player iframe, that's the URL the CDN token expects
            # as Referer/Origin, not the top-level page.
            try:
                frame_url = response.frame.url
                if frame_url and frame_url != site:
                    captured["referer"] = frame_url
            except Exception:
                pass
            cprint_url("playwright", "Captured", url, 45)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=_playwright_launch_args(),
            )
            ctx = browser.new_context(
                user_agent=UA,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )
            ctx.add_init_script(_PLAYWRIGHT_STEALTH_JS)
            page = ctx.new_page()
            page.on("response", _on_response)
            page.route("**/*", lambda route: route.abort()
                       if is_ad_domain(route.request.url) else route.continue_())

            try:
                page.goto(site, wait_until="domcontentloaded", timeout=30_000)
            except PWTimeout:
                pass

            # Give CF time to auto-solve (JS challenge) or let the page load media
            deadline = time.time() + 20
            while not captured["url"] and time.time() < deadline:
                time.sleep(0.5)
                try:
                    html_tick = page.content()
                    m = MEDIA_RE.search(html_tick) or SOURCES_RE.search(html_tick)
                    if m:
                        captured["url"] = m.group(1) if m.lastindex else m.group(0)
                        cprint_url("playwright", "Found in HTML", captured["url"])
                except Exception:
                    pass

            html = page.content()

            # Try iframes if nothing found yet
            if not captured["url"]:
                for m in IFRAME_RE.finditer(html):
                    src = m.group(1).strip()
                    if not src.startswith("http") or IFRAME_SKIP_RE.search(src):
                        continue
                    try:
                        page.goto(src, wait_until="domcontentloaded", timeout=20_000)
                        time.sleep(3)
                        if captured["url"]:
                            if not captured["referer"]:
                                captured["referer"] = src
                            break
                        fm = MEDIA_RE.search(page.content())
                        if fm:
                            captured["url"] = fm.group(0)
                            captured["referer"] = src
                            break
                    except Exception:
                        pass

            # Pull CF cookies before closing
            cookies_raw = ctx.cookies()
            has_clearance = any(c["name"] == "cf_clearance" for c in cookies_raw)
            cf_session = {
                "cookies": {c["name"]: c["value"] for c in cookies_raw},
                "ua": UA,
            } if has_clearance else None

            media_url = captured["url"]
            player_referer = captured["referer"]

            # If we have CF clearance + a media URL: download NOW inside this
            # session. Closing the browser and handing the cookie to yt-dlp
            # fails because CF binds clearance to the browser's TLS fingerprint.
            if media_url and has_clearance:
                cprint("[playwright] CF session active — downloading in-session to avoid TLS mismatch", 51)
                ok = _playwright_download_in_session(page, media_url, player_referer or site, out_fmt)
                browser.close()
                return html, PLAYWRIGHT_DOWNLOADED if ok else None, cf_session, player_referer

            browser.close()
            return html, media_url, cf_session, player_referer

    except Exception as e:
        cprint(f"[playwright] Error: {e}", 196)
        return None, None, None, None


def playwright_intercept_and_download(site: str, out_fmt: str = "mp4") -> bool:
    """Full intercept-and-download via Playwright — used by the intercept layer
    when DrissionPage intercept finds nothing. Keeps the browser alive for
    the download so the CF session stays valid.
    """
    html, result, cf_session, player_referer = playwright_fetch(site, out_fmt=out_fmt)
    if result == PLAYWRIGHT_DOWNLOADED:
        return True
    if result and result != PLAYWRIGHT_DOWNLOADED:
        # Got a media URL without CF challenge — download it normally
        if ffmpeg_ok():
            os.makedirs(config.dir_for(out_fmt), exist_ok=True)
            target_ext = f".{out_fmt}" if out_fmt else ".mp4"
            out_path = safe_filename(result, 1, ext=target_ext)
            cmd = ["ffmpeg", "-y", "-headers", ffmpeg_hdr_block(player_referer or site),
                   "-i", result, "-c", "copy", out_path]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT)
                if r.returncode == 0:
                    cprint(f"[playwright] SAVED: {out_path}", 46)
                    return True
            except subprocess.TimeoutExpired:
                pass
    return False


def cf_bypass(driver, max_wait: int = 30) -> bool:
    """Handle Cloudflare Turnstile — both automatic (JS) and interactive (checkbox).

    IMPORTANT: only returns True when cf_clearance cookie is actually present.
    CF can navigate away from the challenge page BEFORE the cookie lands (managed
    challenge mode) — trusting page navigation alone causes a false positive where
    we report success but hand yt-dlp an empty cookie jar.
    """
    def _is_cf(d):
        try:
            title = (d.title or '').lower()
            return ('just a moment' in title
                    or 'checking your browser' in title
                    or 'performing security verification' in title
                    or 'cloudflare' in title)
        except Exception:
            return False

    def _has_clearance(d):
        try:
            return any(c.get('name') == 'cf_clearance'
                       for c in (d.cookies() or []))
        except Exception:
            return False

    def _try_click_turnstile(d):
        try:
            frame = d.get_frame('@src^https://challenges.cloudflare.com/cdn-cgi',
                                timeout=3)
            if frame:
                for sel in ['css:.mark', 'tag:input', 'css:input[type="checkbox"]']:
                    try:
                        el = frame.ele(sel, timeout=2)
                        if el:
                            el.click()
                            _cf_log.info('Turnstile checkbox clicked via %s', sel)
                            return True
                    except Exception:
                        pass
        except Exception as _e:
            _cf_log.debug('get_frame primary error: %s', _e)

        try:
            for frame in (d.get_frames() or []):
                try:
                    src = frame.attr('src') or ''
                except Exception:
                    src = ''
                if 'challenges.cloudflare.com' not in src:
                    continue
                for sel in ['css:.mark', 'tag:input', 'css:input[type="checkbox"]']:
                    try:
                        el = frame.ele(sel, timeout=1)
                        if el:
                            el.click()
                            _cf_log.info('Turnstile checkbox clicked (fallback) via %s', sel)
                            return True
                    except Exception:
                        pass
        except Exception as _e2:
            _cf_log.debug('_try_click_turnstile fallback error: %s', _e2)

        return False

    # Not on a CF page — nothing to do, but don't claim clearance we don't have
    if not _is_cf(driver):
        return _has_clearance(driver)

    _cf_log.info('Cloudflare challenge detected — waiting for cf_clearance cookie')
    deadline = time.time() + max_wait
    _clicked = False

    while time.time() < deadline:
        # Cookie present = genuine clearance, regardless of page state
        if _has_clearance(driver):
            _cf_log.info('cf_clearance obtained — challenge cleared')
            return True

        # Don't short-circuit on page navigation alone: for managed challenges
        # CF navigates away BEFORE the cookie lands, producing a false positive.
        # We keep polling until the cookie appears or we time out.

        if not _clicked:
            try:
                html_lower = (driver.html or '').lower()
                is_managed = ('verify you are human' in html_lower
                              or 'cf-turnstile' in html_lower
                              or 'challenges.cloudflare.com' in html_lower)
            except Exception:
                is_managed = False
            if is_managed:
                time.sleep(1)
                _clicked = _try_click_turnstile(driver)
                if _clicked:
                    _cf_log.info('Waiting for clearance after checkbox click...')

        time.sleep(1)

    _cf_log.warning('cf_clearance not obtained within %ds timeout', max_wait)
    return False


def get_cf_session(driver) -> dict | None:
    """Extract CF clearance cookies + the exact User-Agent Chrome used.

    CF ties cf_clearance to both the cookie *and* the UA that solved the
    challenge — both must travel together into yt-dlp / curl_cffi.
    Returns None if no clearance cookie is present.
    """
    try:
        cookies = driver.cookies() or []
        if not any(c.get('name') == 'cf_clearance' for c in cookies):
            return None
        ua = driver.run_js('return navigator.userAgent') or UA
        return {
            'cookies': {c['name']: c['value'] for c in cookies},
            'ua': ua,
        }
    except Exception as e:
        _cf_log.debug('get_cf_session error: %s', e)
        return None


def start_listener(driver):
    try:
        driver.listen.start()
        return driver.listen
    except Exception as e:
        cprint(f"[listen] Unavailable: {e}", 196)
        return None


# Response Content-Types that mean "this is the media file", used as a
# fallback when a request's URL has no recognizable extension at all —
# common for tokenized/signed CDN URLs (e.g. /stream/abc123?sig=...).
# Checked ONLY for packets CDP already tagged as Media/XHR/Fetch, so this
# never pays for a header lookup on images, fonts, CSS, etc.
MEDIA_CONTENT_TYPES = (
    "video/mp4", "video/webm", "video/mp2t", "video/x-flv",
    "application/vnd.apple.mpegurl", "application/x-mpegurl",
    "application/dash+xml",
    "audio/mpeg", "audio/mp4", "audio/aac", "audio/ogg",
)


def extract_media_url(raw_url: str, packet=None) -> str | None:
    if SKIP_DOMAINS_RE.search(raw_url):
        mu = re.search(r'[?&]mu=([^&]+)', raw_url)
        if mu:
            media = unquote(mu.group(1))
            if MEDIA_RE.search(media):
                return media
        return None
    if MEDIA_RE.search(raw_url):
        return raw_url
    # URL didn't match a known extension — only bother checking the actual
    # response Content-Type for requests CDP already flagged as media-ish.
    if packet is not None and getattr(packet, "resourceType", None) in ("Media", "XHR", "Fetch"):
        try:
            ctype = (packet.response.headers.get("Content-Type") or "").lower()
        except Exception:
            ctype = ""
        if any(ctype.startswith(t) for t in MEDIA_CONTENT_TYPES):
            return raw_url
    return None


def poll_listener(listener, captured: dict, timeout: int = 15, idle_timeout: float = 3.0) -> None:
    """Poll captured network traffic for a media URL.

    Two independent cutoffs, whichever hits first:
      - `timeout`   — hard ceiling, same as before, never waited past.
      - `idle_timeout` — if the network goes quiet (no request of ANY kind,
        matching or not, for this many seconds) we stop early instead of
        sitting out the rest of `timeout` waiting for traffic that has
        evidently stopped coming.
    """
    if listener is None:
        return
    deadline = time.time() + timeout
    last_activity = time.time()
    while not captured["url"] and time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        if time.time() - last_activity > idle_timeout:
            debug_event(stage="listener_poll", result="idle_cutoff",
                        idle_timeout=idle_timeout)
            break
        try:
            packet = listener.wait(count=1, timeout=min(1, remaining),
                                   fit_count=True, raise_err=False)
            if packet is None:
                continue
            last_activity = time.time()
            for p in (packet if isinstance(packet, (list, tuple)) else [packet]):
                url = extract_media_url(getattr(p, "url", "") or "", packet=p)
                if url:
                    captured["url"] = url
                    cprint_url("listen", "Captured", url, 45)
                    return
        except Exception as e:
            debug_event(stage="listener_poll", error=str(e))
            time.sleep(0.3)


# ── Browser-intercept CDN download (token-bound) ──────────────────────────────
def browser_intercept_and_download(player_url: str, site_referer: str,
                                    out_fmt: str = "mp4",
                                    headless: bool = True,
                                    cf_session: dict | None = None) -> bool:
    try:
        from DrissionPage import ChromiumPage
    except ImportError:
        print("[intercept] DrissionPage not installed.")
        return False

    cprint_url("intercept", "Opening in Chrome", player_url, 208)
    captured = {"url": None}
    opts = chrome_opts(headless=headless, stealth=True)
    driver = ChromiumPage(addr_or_opts=opts)
    driver.add_init_js(STEALTH_JS)
    listener = start_listener(driver)
    try:
        driver.set.blocked_urls(AD_BLOCK_CDP_PATTERNS)
    except Exception as e:
        _cf_log.debug('ad-block setup failed: %s', e)

    try:
        if cf_session and cf_session.get('cookies'):
            from urllib.parse import urlparse as _up
            _parsed = _up(player_url)
            _netloc = _parsed.netloc
            root = f"{_parsed.scheme}://{_netloc}"
            cprint(f"[intercept] Injecting {len(cf_session['cookies'])} CF cookies...", 51)
            try:
                driver.get(root)
                time.sleep(2)
                # Use DrissionPage's set.cookies() (goes through CDP Network.setCookie)
                # which correctly sets HttpOnly cookies — unlike document.cookie
                # which the browser silently ignores for HttpOnly values.
                cookie_list = [
                    {
                        "name": name,
                        "value": str(value),
                        "domain": _netloc,
                        "path": "/",
                    }
                    for name, value in cf_session['cookies'].items()
                ]
                driver.set.cookies(cookie_list)
                cprint("[intercept] Cookies set — navigating to video page", 51)
            except Exception as _ce:
                cprint(f"[intercept] Cookie injection error: {_ce}", 196)
        else:
            cprint("[intercept] No CF session available — cold start", 208)

        driver.get(player_url)
        time.sleep(3)
        cf_bypass(driver)

        try:
            driver.wait.load_complete(timeout=15)
        except Exception:
            pass
        time.sleep(1)

        is_twitter = any(h in player_url for h in ("x.com", "twitter.com", "t.co"))
        deadline = time.time() + (40 if is_twitter else 20)

        _XCOM_SELECTORS = [
            "css:[data-testid='videoPlayer'] video",
            "css:[data-testid='videoComponent']",
            "css:div[aria-label='Embedded video']",
            "css:div[role='progressbar']",
            "css:video",
        ]

        _GENERIC_SELECTORS = [
            "tag:button@class*=play", "tag:button@aria-label*=play",
            "css:.play-button", "css:[class*='play']",
        ]

        selectors = (_XCOM_SELECTORS + _GENERIC_SELECTORS) if is_twitter else _GENERIC_SELECTORS

        clicked_once = False
        while not captured["url"] and time.time() < deadline:
            poll_listener(listener, captured, timeout=1)
            if captured["url"]:
                break
            if not clicked_once:
                for sel in selectors:
                    try:
                        btn = driver.ele(sel, timeout=0.5)
                        if btn:
                            btn.click()
                            print(f"[intercept] Clicked: {sel}")
                            time.sleep(2)
                            clicked_once = True
                            break
                    except Exception:
                        pass
            time.sleep(0.5)

        if not captured["url"]:
            fm = MEDIA_RE.search(driver.html)
            if fm:
                captured["url"] = fm.group(0)
                cprint_url("intercept", "Found in HTML", captured["url"])

        cdn_url = captured["url"]
        if not cdn_url:
            if is_twitter and ytdlp_ok():
                print("[intercept] No CDN URL — handing off to yt-dlp (Twitter extractor)...")
                fmt_sel, extra = yt_fmt_args(out_fmt)
                cmd = (
                    ["yt-dlp",
                     "--add-header", f"User-Agent:{UA}",
                     "--impersonate", "chrome",
                     "--no-warnings", "--progress",
                     "-f", fmt_sel,
                     "-o", os.path.join(config.dir_for(out_fmt), "%(title)s.%(ext)s")]
                    + extra + [site_referer]
                )
                try:
                    result = subprocess.run(cmd, timeout=YTDLP_TIMEOUT)
                    return result.returncode == 0
                except subprocess.TimeoutExpired:
                    print("[intercept] yt-dlp timed out on Twitter URL.")
            print("[intercept] No CDN URL found.")
            return False

        cprint_url("intercept", "CDN URL", cdn_url)
        os.makedirs(config.dir_for(out_fmt), exist_ok=True)

        if ytdlp_ok():
            cmd = build_ytdlp_generic_cmd(cdn_url, out_fmt, referer=player_url,
                                          progress_flag="--newline")
            print("[intercept] Trying yt-dlp...")
            if run_ytdlp_rainbow(cmd, timeout=YTDLP_TIMEOUT):
                return True
            print("[intercept] yt-dlp failed, falling back to ffmpeg...")

        if ffmpeg_ok():
            target_ext = f".{out_fmt}" if out_fmt else ".mp4"
            out_path = safe_filename(cdn_url, 1, ext=target_ext)
            tmp = out_path + ".part" + target_ext
            cmd_ff = ["ffmpeg", "-y", "-headers", ffmpeg_hdr_block(player_url),
                      "-i", cdn_url, "-c", "copy", tmp]
            try:
                r = subprocess.run(cmd_ff, capture_output=True, timeout=FFMPEG_TIMEOUT)
            except subprocess.TimeoutExpired:
                if os.path.exists(tmp): os.remove(tmp)
                print("[intercept] ffmpeg timed out")
                return False
            if r.returncode == 0 and os.path.exists(tmp):
                size = os.path.getsize(tmp)
                if size >= MIN_MB * 1024 * 1024:
                    os.replace(tmp, out_path)
                    print(f"[intercept] SAVED: {out_path} ({size/1024/1024:.1f} MB)")
                    return True
                os.remove(tmp)
            err = r.stderr.decode(errors="replace").strip().splitlines()[-3:]
            print(f"[intercept] ffmpeg failed: {' | '.join(err)}")

        return False

    except Exception as e:
        print(f"[intercept] Error: {e}")
        return False
    finally:
        try: driver.quit()
        except Exception: pass
