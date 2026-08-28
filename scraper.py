"""
scraper.py — video downloader with Cloudflare bypass

Architecture:
  Layer 1: DrissionPage (real Chrome, no webdriver signals)
            → CloudflareBypasser logic inlined
            → network listener catches mp4/m3u8/mpd as they load
            → browser-intercept path for IP/token-bound CDNs
  Layer 2: curl_cffi (Chrome TLS fingerprint) for non-CF sites
  Layer 3: requests fallback
  Layer 4: yt-dlp fallback if all extraction fails

Install deps (run once):
  pip install DrissionPage curl_cffi yt-dlp
Chrome must be installed — DrissionPage drives your real Chrome.
"""

import os, sys, re, time, shutil, base64, subprocess, threading
from urllib.parse import urlparse, urljoin, unquote

# ── curl_cffi / requests backend ──────────────────────────────────────────────
try:
    from curl_cffi import requests as cffi_requests
    IMPERSONATE = "chrome124"
    def _make_session(referer=""):
        s = cffi_requests.Session(impersonate=IMPERSONATE)
        if referer:
            s.headers["Referer"] = referer
        return s
    def _raw_get(url, headers, stream=False, timeout=30):
        return cffi_requests.get(url, headers=headers, stream=stream,
                                 timeout=timeout, impersonate=IMPERSONATE,
                                 allow_redirects=True)
    USING_CFFI = True
except ImportError:
    import requests as _req
    def _make_session(referer=""):
        s = _req.Session()
        if referer:
            s.headers["Referer"] = referer
        return s
    def _raw_get(url, headers, stream=False, timeout=30):
        return _req.get(url, headers=headers, stream=stream,
                        timeout=timeout, allow_redirects=True)
    USING_CFFI = False

OUTPUT_DIR  = "videos"
MAX_RETRIES = 3
MIN_MB      = 2
YTDLP_TIMEOUT  = 3600   # 1 hour — yt-dlp can be slow on large files
FFMPEG_TIMEOUT = 3600   # same
STREAM_TIMEOUT = 30     # connect+read timeout per chunk window for _raw_get

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

LOGO = r"""
 ______   ______   ______    ________   ______   ______
/_____/\ /_____/\ /_____/\  /_______/\ /_____/\ /_____/\
\::::_\/_\:::__\/ \:::_ \ \ \::: _  \ \\:::_ \ \\::::_\/_
 \:\/___/\\:\ \  __\:(_) ) )_\::(_)  \ \\:(_) \ \\:\/___/\
  \_::._\:\\:\ \/_/\\: __ `\ \\:: __  \ \\: ___\/ \::___\/_
    /____\:\\:\_\ \ \\ \ `\ \ \\:.\ \  \ \\ \ \    \:\____/\
    \_____\/ \_____\/ \_\/ \_\/ \__\/\__\/ \_\/     \_____\/
"""

MEDIA_RE = re.compile(
    r'https?://[^\s"\'<>{}\[\]]+\.(?:mp4|m3u8|mpd)(?:[?#][^\s"\'<>]*)?',
    re.I
)

# Analytics/tracking domains to skip (but still mine for mu= params)
SKIP_DOMAINS_RE = re.compile(
    r'jwpltx\.com|google-analytics|doubleclick|googlesyndication'
    r'|facebook\.com|twitter\.com|scorecardresearch|omtrdc\.net',
    re.I
)

# Pipe-delimited token signatures — always IP-bound, direct download = 403
TOKEN_BOUND_RE = re.compile(r'\|\d{9,10}\|[0-9a-f]{16,}', re.I)

# ── Cached tool checks (avoid shutil.which on every call) ─────────────────────
_FFMPEG_AVAILABLE: bool | None = None
_YTDLP_AVAILABLE: bool | None  = None

def ffmpeg_ok() -> bool:
    global _FFMPEG_AVAILABLE
    if _FFMPEG_AVAILABLE is None:
        _FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
    return _FFMPEG_AVAILABLE

def ytdlp_ok() -> bool:
    global _YTDLP_AVAILABLE
    if _YTDLP_AVAILABLE is None:
        _YTDLP_AVAILABLE = shutil.which("yt-dlp") is not None
    return _YTDLP_AVAILABLE


def is_token_bound(url: str) -> bool:
    return bool(TOKEN_BOUND_RE.search(url))

def _extract_media_url(raw_url: str):
    """
    Given a captured network URL, return the real media URL or None.
    Handles:
      1. URL is mp4/m3u8/mpd directly
      2. JWPlayer analytics ping with mu= param containing the real URL
    """
    # JWPlayer ping and other analytics — check mu= param first
    if SKIP_DOMAINS_RE.search(raw_url):
        mu = re.search(r'[?&]mu=([^&]+)', raw_url)
        if mu:
            media = unquote(mu.group(1))
            if MEDIA_RE.search(media):
                print(f"[listen] Extracted mu= media URL: {media}")
                return media
        return None
    if MEDIA_RE.search(raw_url):
        return raw_url
    return None


# ── inlined CloudflareBypasser ────────────────────────────────────────────────

def _cf_bypass(driver, max_attempts=10):
    import logging
    log = logging.getLogger("CFBypass")

    def _is_cf_page(d):
        title = d.title or ""
        return ("just a moment" in title.lower() or
                "checking your browser" in title.lower() or
                "cloudflare" in title.lower())

    def _click_verify(d):
        try:
            for iframe in d.get_frames():
                try:
                    cb = iframe.ele("tag:input@type=checkbox", timeout=1)
                    if cb:
                        cb.click()
                        log.info("Clicked Turnstile checkbox inside iframe")
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        try:
            cb = d.ele("tag:input@type=checkbox", timeout=1)
            if cb:
                cb.click()
                log.info("Clicked checkbox (direct)")
                return True
        except Exception:
            pass
        return False

    log.info("Starting CF bypass")
    for attempt in range(1, max_attempts + 1):
        if not _is_cf_page(driver):
            log.info("CF page gone — bypass succeeded")
            return True
        log.info(f"Attempt {attempt}: CF page detected, trying to click...")
        _click_verify(driver)
        time.sleep(2)

    log.warning("CF bypass: max attempts reached, proceeding anyway")
    return False


# ── shared Chrome options factory ─────────────────────────────────────────────

def _chrome_opts():
    from DrissionPage import ChromiumOptions
    opts = ChromiumOptions()
    opts.set_argument("--no-sandbox")
    opts.set_argument("--disable-blink-features=AutomationControlled")
    opts.set_argument(f"--user-agent={UA}")
    opts.headless(True)
    return opts


# ── DrissionPage network listener (4.1.x API) ────────────────────────────────
#
# Confirmed API from version 4.1.1.4:
#   listen.start(targets=None, is_regex=None, method=None, res_type=None)
#   listen.wait(count=1, timeout=None, fit_count=True, raise_err=None)
#   listen.steps(count=None, timeout=None, gap=1)  — generator
#
# Strategy: start() with no filter (catch everything), then wait() with a
# short timeout in a loop, check each packet's URL ourselves.

def _start_listener(driver, captured: dict, lock: threading.Lock):
    """Start listener with no filter — we'll check URLs ourselves."""
    try:
        listener = driver.listen
        listener.start()  # no targets filter — catch all requests
        print("[listen] Network listener started")
        return listener
    except Exception as e:
        print(f"[listen] Listener unavailable: {e}")
        return None


def _poll_listener(listener, captured: dict, lock: threading.Lock, timeout=15):
    """
    Call listener.wait() in a loop until we get a media URL or timeout.
    Checks each packet URL through _extract_media_url() which handles
    both direct media URLs and JWPlayer analytics pings with mu= params.
    """
    if listener is None:
        return
    deadline = time.time() + timeout
    while not captured["url"] and time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            packet = listener.wait(count=1, timeout=min(2, remaining),
                                   fit_count=True, raise_err=False)
            if packet is None:
                continue
            packets = packet if isinstance(packet, (list, tuple)) else [packet]
            for p in packets:
                raw_url = getattr(p, "url", "") or ""
                media_url = _extract_media_url(raw_url)
                if media_url:
                    with lock:
                        if not captured["url"]:
                            captured["url"] = media_url
                            print(f"[listen] Captured: {media_url}")
                    return
        except Exception:
            time.sleep(0.3)


# ── DrissionPage browser fetch ────────────────────────────────────────────────

def _drission_fetch(site: str):
    try:
        from DrissionPage import ChromiumPage
    except ImportError:
        print("[browser] DrissionPage not installed — pip install DrissionPage")
        return None, None

    print("[browser] Launching Chrome (DrissionPage)...")
    captured = {"url": None}
    lock = threading.Lock()
    driver = ChromiumPage(addr_or_opts=_chrome_opts())
    listener = _start_listener(driver, captured, lock)

    try:
        print(f"[browser] Navigating to {site}")
        driver.get(site)
        time.sleep(3)
        _cf_bypass(driver)
        time.sleep(2)

        # Poll for intercepted media — longer window for slow embeds
        _poll_listener(listener, captured, lock, timeout=15)

        html = driver.html
        media_url = captured["url"]

        if not media_url:
            m = MEDIA_RE.search(html)
            if m:
                media_url = m.group(0)
                print(f"[browser] Found media in page HTML: {media_url}")

        if not media_url:
            iframes = re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.I)
            for src in iframes:
                if src.startswith("http") and not re.search(
                        r'google|facebook|disqus', src, re.I):
                    print(f"[browser] Checking iframe: {src}")
                    driver.get(src)
                    time.sleep(3)
                    _poll_listener(listener, captured, lock, timeout=15)
                    if captured["url"]:
                        media_url = captured["url"]
                        break
                    frame_html = driver.html
                    m = MEDIA_RE.search(frame_html)
                    if m:
                        media_url = m.group(0)
                        print(f"[browser] Found media in iframe: {media_url}")
                        break

        return html, media_url

    except Exception as e:
        print(f"[browser] Error: {e}")
        return None, None
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# ── Browser-intercept CDN download (IP/token-bound) ───────────────────────────

def _browser_intercept_and_download(player_url: str, site_referer: str,
                                    out_fmt: str = "mp4") -> bool:
    """
    Open player_url in real Chrome → CF Worker issues token bound to our IP.
    Poll network listener for the CDN request in-flight.
    Download with yt-dlp --cookies-from-browser chrome, fall back to ffmpeg.
    """
    try:
        from DrissionPage import ChromiumPage
    except ImportError:
        print("[intercept] DrissionPage not installed.")
        return False

    print(f"[intercept] Opening player in Chrome: {player_url}")
    intercepted: dict = {"url": None}
    lock = threading.Lock()
    driver = ChromiumPage(addr_or_opts=_chrome_opts())
    listener = _start_listener(driver, intercepted, lock)

    try:
        driver.get(player_url)
        time.sleep(3)
        _cf_bypass(driver)

        # Nudge play button while polling for CDN request
        deadline = time.time() + 20
        while not intercepted["url"] and time.time() < deadline:
            # Poll one tick
            _poll_listener(listener, intercepted, lock, timeout=1)
            if intercepted["url"]:
                break
            # Try clicking play
            try:
                for sel in ("tag:button@class*=play", "tag:button@aria-label*=play",
                            "css:.play-button", "css:[class*='play']"):
                    try:
                        btn = driver.ele(sel, timeout=0.5)
                        if btn:
                            btn.click()
                            print(f"[intercept] Clicked play: {sel}")
                            time.sleep(2)
                            break
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(0.5)

        # Last resort: scan the page HTML for a media URL
        if not intercepted["url"]:
            html = driver.html
            m = MEDIA_RE.search(html)
            if m:
                intercepted["url"] = m.group(0)
                print(f"[intercept] Found media in player HTML: {intercepted['url']}")

        cdn_url = intercepted["url"]
        if not cdn_url:
            print("[intercept] No CDN URL captured.")
            return False

        print(f"[intercept] CDN URL: {cdn_url}")
        os.makedirs(OUTPUT_DIR, exist_ok=True)

        # yt-dlp: try direct (no cookie loop — DPAPI failures are noisy and useless)
        is_audio_only = out_fmt in ("mp3", "aac", "flac", "opus", "m4a", "wav")
        if ytdlp_ok():
            out_tmpl = os.path.join(OUTPUT_DIR, "%(title)s.%(ext)s")
            yt_fmt_args = (
                ["--extract-audio", "--audio-format", out_fmt, "--audio-quality", "0"]
                if is_audio_only else
                (["--merge-output-format", out_fmt] if out_fmt else [])
            )
            cmd = (
                ["yt-dlp",
                 "--referer", player_url,
                 "--add-header", f"User-Agent:{UA}",
                 "--extractor-args", "generic:impersonate",
                 "--no-warnings", "--progress",
                 "-f", "bestvideo+bestaudio/best" if ffmpeg_ok() else "best",
                 "-o", out_tmpl]
                + yt_fmt_args
                + [cdn_url]
            )
            print(f"[yt-dlp] Attempting direct download...")
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=YTDLP_TIMEOUT)
                if r.returncode == 0:
                    return True
                # Failed — fall through to ffmpeg silently
                print("[intercept] yt-dlp failed, falling back to ffmpeg...")
            except subprocess.TimeoutExpired:
                print(f"[intercept] yt-dlp timed out, falling back to ffmpeg...")

        # ffmpeg fallback — output to requested format, not the CDN's ext
        if ffmpeg_ok():
            origin = f"{urlparse(player_url).scheme}://{urlparse(player_url).netloc}"
            hdr_block = (
                f"Referer: {player_url}\r\n"
                f"Origin: {origin}\r\n"
                f"User-Agent: {UA}\r\n"
                f"Sec-Fetch-Dest: video\r\n"
                f"Sec-Fetch-Mode: no-cors\r\n"
                f"Sec-Fetch-Site: cross-site\r\n"
            )
            target_ext = f".{out_fmt}" if out_fmt else ".mp4"
            out_path = safe_filename(cdn_url, 1, ext=target_ext)
            tmp = out_path + ".part" + target_ext
            cmd_ff = ["ffmpeg", "-y", "-headers", hdr_block,
                      "-i", cdn_url, "-c", "copy", tmp]
            try:
                r = subprocess.run(cmd_ff, capture_output=True, timeout=FFMPEG_TIMEOUT)
            except subprocess.TimeoutExpired:
                if os.path.exists(tmp):
                    os.remove(tmp)
                print(f"[intercept] ffmpeg timed out after {FFMPEG_TIMEOUT}s")
                return False
            if r.returncode == 0 and os.path.exists(tmp):
                size = os.path.getsize(tmp)
                if size >= MIN_MB * 1024 * 1024:
                    os.replace(tmp, out_path)
                    print(f"[intercept] SAVED via ffmpeg: {out_path} "
                          f"({size/1024/1024:.1f} MB)")
                    return True
                os.remove(tmp)
            err = r.stderr.decode(errors="replace").strip().splitlines()[-3:]
            print(f"[intercept] ffmpeg failed: {' | '.join(err)}")

        return False

    except Exception as e:
        print(f"[intercept] Error: {e}")
        return False
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# ── curl_cffi / requests fetch ────────────────────────────────────────────────

def _base_headers(referer=""):
    return {
        "User-Agent":                UA,
        "Referer":                   referer,
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language":           "en-US,en;q=0.9",
        "Accept-Encoding":           "gzip, deflate, br",
        "Connection":                "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest":            "document",
        "Sec-Fetch-Mode":            "navigate",
        "Sec-Fetch-Site":            "none",
        "Sec-Fetch-User":            "?1",
        "Sec-CH-UA":                 '"Chromium";v="124","Google Chrome";v="124","Not-A.Brand";v="99"',
        "Sec-CH-UA-Mobile":          "?0",
        "Sec-CH-UA-Platform":        '"Windows"',
        "Cache-Control":             "max-age=0",
    }

def _simple_fetch(site: str):
    sess = _make_session(site)
    sess.headers.update(_base_headers(site))
    root = f"{urlparse(site).scheme}://{urlparse(site).netloc}"
    # Cookie-prime the root only when site is not already the root
    if root.rstrip("/") != site.rstrip("/"):
        try:
            sess.get(root, timeout=15, allow_redirects=True)
        except Exception:
            pass
    resp = sess.get(site, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    return resp.text, None


# ── Extraction helpers ─────────────────────────────────────────────────────────

_DIRECT_RE = re.compile(
    r'(?:(?:file|src|source|href|data-src)["\s]*[:=]["\s]*|["\'])'
    r'["\']?(https?://[^\s"\'<>{}\[\]]+\.(?:mp4|m3u8|mpd)(?:[?#][^\s"\'<>]*)?)',
    re.I,
)

def find_direct_url(html: str):
    m = _DIRECT_RE.search(html)
    return m.group(1) if m else None

def b64_try(s: str):
    try:
        padded = s + "=" * (-len(s) % 4)
        decoded = base64.b64decode(padded).decode("utf-8")
        if decoded.startswith("http") or decoded.startswith("/"):
            return decoded
    except Exception:
        pass
    return None

def extract_player_url(html: str, base_url: str):
    """Return the first plausible player iframe src, trying all iframes in order."""
    SKIP = re.compile(
        r"google\.com/recaptcha|accounts\.google|facebook\.com/plugins"
        r"|twitter\.com/i/|disqus\.com", re.I)
    for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.I):
        src = m.group(1).strip()
        if src.startswith("http") and not SKIP.search(src):
            return src
    # Fallback: relative iframes resolved against base_url
    for m in re.finditer(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.I):
        src = m.group(1).strip()
        if src and not SKIP.search(src):
            resolved = urljoin(base_url, src)
            if resolved.startswith("http"):
                return resolved
    return None

def extract_media_from_player(html: str, player_base: str):
    result = {"video": None, "subtitle": None, "thumb": None, "player_url": None}
    direct = find_direct_url(html)
    if direct:
        result["video"] = direct
        return result
    m = re.search(r'data-id=["\']([^"\']+)["\']', html)
    if not m:
        for token in re.findall(r'[A-Za-z0-9+/]{30,}={0,2}', html):
            decoded = b64_try(token)
            if decoded and re.search(r'\.(mp4|m3u8|mpd)', decoded, re.I):
                result["video"] = decoded
                return result
        return result
    data_id = m.group(1)
    result["player_url"] = urljoin(player_base, data_id)
    params = {}
    for part in data_id.split("?", 1)[-1].split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            params[k] = v
    for key, target in (("vid", "video"), ("s", "subtitle"), ("i", "thumb")):
        if key in params:
            decoded = b64_try(params[key])
            if decoded:
                result[target] = decoded
    return result


# ── yt-dlp / ffmpeg helpers ───────────────────────────────────────────────────

def ytdlp_download(url: str, referer: str, out_fmt: str = "mp4") -> bool:
    if not ytdlp_ok():
        print("[!] yt-dlp not found — pip install yt-dlp")
        return False
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    is_audio_only = out_fmt in ("mp3", "aac", "flac", "opus", "m4a", "wav")
    base_fmt = "bestvideo+bestaudio/best" if ffmpeg_ok() else "best"
    extra = (["--extract-audio", "--audio-format", out_fmt, "--audio-quality", "0"]
             if is_audio_only else
             (["--merge-output-format", out_fmt] if out_fmt else []))
    cmd = (
        ["yt-dlp",
         "--referer", referer,
         "--add-header", f"User-Agent:{UA}",
         "--extractor-args", "generic:impersonate",
         "--no-warnings", "--progress",
         "-f", base_fmt,
         "-o", os.path.join(OUTPUT_DIR, "%(title)s.%(ext)s")]
        + extra
        + [url]
    )
    print(f"[yt-dlp] {' '.join(cmd)}")
    try:
        return subprocess.run(cmd, timeout=YTDLP_TIMEOUT).returncode == 0
    except subprocess.TimeoutExpired:
        print(f"[yt-dlp] Timed out after {YTDLP_TIMEOUT}s")
        return False


# ── Direct download ───────────────────────────────────────────────────────────

def safe_filename(url: str, n: int = 1, ext: str = ".mp4") -> str:
    # strip query string for filename
    path = urlparse(url).path
    name = unquote(os.path.basename(path)) or f"video_{n}"
    # Always strip any CDN container ext (.m3u8, .mpd, etc.) and apply requested ext
    name = re.sub(r'\.(?:mp4|webm|mkv|m3u8|mpd|ts)$', '', name, flags=re.I)
    name = name or f"video_{n}"
    name += ext
    return os.path.join(OUTPUT_DIR, f"{n:02d}_{name}")

def download_file(url: str, out_path: str, referer: str, n=1, total=1) -> str:
    tag = f"[{n}/{total}]"
    if os.path.exists(out_path):
        return f"{tag} SKIP (exists): {out_path}"

    if ffmpeg_ok():
        tmp = out_path + ".part.mp4"
        origin = f"{urlparse(referer).scheme}://{urlparse(referer).netloc}"
        hdr_block = (
            f"Referer: {referer}\r\n"
            f"Origin: {origin}\r\n"
            f"User-Agent: {UA}\r\n"
            f"Sec-Fetch-Dest: video\r\n"
            f"Sec-Fetch-Mode: no-cors\r\n"
            f"Sec-Fetch-Site: cross-site\r\n"
        )
        cmd = ["ffmpeg", "-y", "-headers", hdr_block, "-i", url, "-c", "copy", tmp]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT)
            if r.returncode == 0 and os.path.exists(tmp):
                size = os.path.getsize(tmp)
                if size >= MIN_MB * 1024 * 1024:
                    os.replace(tmp, out_path)
                    return f"{tag} SAVED via ffmpeg: {out_path} ({size/1024/1024:.1f} MB)"
                os.remove(tmp)
                return f"{tag} TOO SMALL ({size/1024/1024:.2f} MB)"
            err = r.stderr.decode(errors="replace").strip().splitlines()[-3:]
            print(f"{tag} ffmpeg failed, falling back: {' | '.join(err)}")
        except subprocess.TimeoutExpired:
            if os.path.exists(tmp): os.remove(tmp)
            return f"{tag} TIMEOUT (ffmpeg > {FFMPEG_TIMEOUT}s)"

    origin = f"{urlparse(referer).scheme}://{urlparse(referer).netloc}"
    headers = {
        "User-Agent":     UA,
        "Referer":        referer,
        "Origin":         origin,
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }
    tmp = out_path + ".part"
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = _raw_get(url, headers=headers, stream=True, timeout=STREAM_TIMEOUT)
            resp.raise_for_status()
            size = 0
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(1024 * 1024):
                    f.write(chunk); size += len(chunk)
            os.replace(tmp, out_path)
            if size < MIN_MB * 1024 * 1024:
                os.remove(out_path)
                return f"{tag} TOO SMALL ({size/1024/1024:.2f} MB)"
            return f"{tag} SAVED: {out_path} ({size/1024/1024:.1f} MB)"
        except Exception as e:
            last_err = e
            if os.path.exists(tmp): os.remove(tmp)
            if attempt < MAX_RETRIES:
                wait = 2 ** attempt
                print(f"{tag} retry {attempt} ({e}), waiting {wait}s...")
                time.sleep(wait)
    return f"{tag} FAILED: {last_err}"


# ── Main ──────────────────────────────────────────────────────────────────────

# ── YouTube / yt-dlp-native detection ────────────────────────────────────────

YT_RE = re.compile(
    r'(?:https?://)?(?:www\.|m\.)?'
    r'(?:youtube\.com/(?:watch|shorts|live|embed)|youtu\.be/)',
    re.I
)

def is_youtube(url: str) -> bool:
    return bool(YT_RE.search(url))

def ytdlp_youtube(url: str, out_fmt: str = "mp4") -> bool:
    """
    Dedicated YouTube path: yt-dlp with best video up to 1080p + best audio.
    out_fmt: target container ('mp4', 'mp3', 'mkv', etc.) or '' for original.
    """
    if not ytdlp_ok():
        print("[yt] yt-dlp not found — pip install yt-dlp")
        return False
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_tmpl = os.path.join(OUTPUT_DIR, "%(title)s.%(ext)s")

    is_audio_only = out_fmt in ("mp3", "aac", "flac", "opus", "m4a", "wav")

    if is_audio_only:
        # audio-only extraction
        vid_fmt = "bestaudio/best"
        extra = ["--extract-audio", "--audio-format", out_fmt, "--audio-quality", "0"]
        merge_fmt = []
    elif out_fmt == "" :
        # original — best quality, no remux
        vid_fmt = (
            "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
            if ffmpeg_ok() else "best[height<=1080]/best"
        )
        extra = []
        merge_fmt = []
    else:
        # video container (mp4, mkv, webm, etc.)
        if ffmpeg_ok():
            vid_fmt = (
                f"bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]"
                f"/bestvideo[height<=1080]+bestaudio"
                f"/best[height<=1080]/best"
            )
        else:
            vid_fmt = "best[height<=1080]/best"
        extra = []
        merge_fmt = ["--merge-output-format", out_fmt]

    cmd = (
        ["yt-dlp", "--no-warnings", "--progress", "-f", vid_fmt]
        + merge_fmt
        + extra
        + ["-o", out_tmpl, url]
    )
    print(f"[yt] {' '.join(cmd)}")
    try:
        return subprocess.run(cmd, timeout=YTDLP_TIMEOUT).returncode == 0
    except subprocess.TimeoutExpired:
        print(f"[yt] Timed out after {YTDLP_TIMEOUT}s")
        return False


def scrape(site: str, out_fmt: str = "mp4"):
    html, video_url = None, None

    # YouTube shortcut — hand straight to yt-dlp, skip all scraping layers
    if is_youtube(site):
        print("[yt] YouTube URL detected — routing to yt-dlp directly")
        ok = ytdlp_youtube(site, out_fmt)
        sys.exit(0 if ok else 1)

    # Layer 1: fast direct fetch
    print(f"[1] Trying direct fetch: {site}")
    try:
        html, video_url = _simple_fetch(site)
        print("[1] Direct fetch succeeded")
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "?")
        print(f"[1] Direct fetch failed (HTTP {status}) — switching to browser mode")
        html = None

    # Layer 2: real Chrome + CF bypass
    if html is None:
        print("[2] Browser mode (real Chrome + CF bypass)...")
        html, video_url = _drission_fetch(site)
        if html is None:
            raise SystemExit("[!] Both fetch modes failed. Giving up.")

    # Layer 3: scan HTML for media URL
    if not video_url:
        print("[3] Scanning page HTML for media URL...")
        video_url = find_direct_url(html)

    cdn_referer = site
    player_iframe_url = None

    if not video_url:
        print("[3] Scanning for player iframe...")
        player_iframe_url = extract_player_url(html, site)
        if player_iframe_url:
            print(f"[3] Player URL: {player_iframe_url}")
            cdn_referer = player_iframe_url
            player_base = (f"{urlparse(player_iframe_url).scheme}://"
                           f"{urlparse(player_iframe_url).netloc}")
            try:
                sess = _make_session(site)
                sess.headers.update(_base_headers(site))
                presp = sess.get(player_iframe_url, timeout=20, allow_redirects=True)
                presp.raise_for_status()
                media = extract_media_from_player(presp.text, player_base)
                print(f"[3] Player result: {media}")
                video_url = media.get("video")
                if not video_url and media.get("player_url"):
                    presp2 = sess.get(media["player_url"], timeout=20, allow_redirects=True)
                    media2 = extract_media_from_player(presp2.text, player_base)
                    video_url = media2.get("video")
            except Exception as e:
                print(f"[3] Player fetch error: {e}")

    # Token-bound: pipe-signature detected — must intercept from real browser
    if video_url and is_token_bound(video_url):
        print(f"[!] Token-bound URL detected: {video_url}")
        print("[!] Routing to browser-intercept path...")
        intercept_target = player_iframe_url or site
        ok = _browser_intercept_and_download(intercept_target, site, out_fmt)
        sys.exit(0 if ok else 1)

    # No URL at all
    if not video_url:
        print("[!] No media URL found — trying browser intercept on player...")
        intercept_target = player_iframe_url or site
        ok = _browser_intercept_and_download(intercept_target, site, out_fmt)
        if ok:
            sys.exit(0)
        print("[!] Trying yt-dlp on main URL...")
        ok = ytdlp_download(site, site, out_fmt)
        sys.exit(0 if ok else 1)

    # CDN domain != player domain → possible token binding without pipe sig
    video_netloc  = urlparse(video_url).netloc
    player_netloc = urlparse(cdn_referer).netloc
    if video_netloc != player_netloc:
        print(f"[DL] CDN ({video_netloc}) != player ({player_netloc}) — trying intercept first...")
        intercept_target = player_iframe_url or cdn_referer
        ok = _browser_intercept_and_download(intercept_target, site, out_fmt)
        if ok:
            print(f"\nDONE — {os.path.abspath(OUTPUT_DIR)}")
            sys.exit(0)
        print("[DL] Intercept failed, falling back to direct download...")

    # Direct download (ffmpeg → requests)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ext = f".{out_fmt}" if out_fmt else None   # None = keep URL's original ext
    out_video = safe_filename(video_url, 1, ext=ext or ".mp4")
    print(f"\n[DL] {video_url}")
    print(f"[DL] Referer → {cdn_referer}")
    print(f"[DL] Output  → {out_video}")
    result = download_file(video_url, out_video, cdn_referer)
    print(result)
    print(f"\nDONE — {os.path.abspath(OUTPUT_DIR)}")


def _pick_format() -> str:
    """
    Ask the user which output format they want.
    Returns an ext string like 'mp4', 'mp3', or '' for original.
    """
    options = ["mp4", "mp3", "mkv", "webm", "original"]
    print("\nOutput format:")
    for i, opt in enumerate(options, 1):
        label = f"{opt}  <- keeps original container/quality" if opt == "original" else opt
        print(f"  {i}. {label}")
    raw = input("Choice [1]: ").strip()
    if not raw or raw == "1":
        return "mp4"
    if raw.isdigit():
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return "" if options[idx] == "original" else options[idx]
    # typed a custom extension like 'avi', 'flac', etc.
    return raw.lstrip(".")


if __name__ == "__main__":
    print(LOGO)
    backend = "curl_cffi+Chrome" if USING_CFFI else "requests+Chrome"
    print(f"[http] Backend: {backend}\n")
    site = sys.argv[1] if len(sys.argv) > 1 else input("Site URL: ").strip()
    if not site:
        raise SystemExit("No URL.")
    fmt = _pick_format()
    scrape(site, fmt)
