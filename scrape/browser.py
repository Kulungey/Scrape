"""Real-Chrome layer: Cloudflare bypass, network interception, and the
token-bound CDN intercept-and-download path. Everything that needs an
actual browser lives here.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from urllib.parse import unquote

from . import config
from .config import UA, YTDLP_TIMEOUT, FFMPEG_TIMEOUT, MIN_MB, ffmpeg_hdr_block
from .patterns import MEDIA_RE, SKIP_DOMAINS_RE, IFRAME_RE, IFRAME_SKIP_RE
from .ui import cprint, cprint_url
from .ytdlp import ffmpeg_ok, ytdlp_ok, yt_fmt_args
from .downloader import safe_filename

import logging
_cf_log = logging.getLogger("CFBypass")


def chrome_opts():
    from DrissionPage import ChromiumOptions
    opts = ChromiumOptions()
    opts.set_argument("--no-sandbox")
    opts.set_argument("--disable-blink-features=AutomationControlled")
    opts.set_argument(f"--user-agent={UA}")
    opts.headless(True)
    return opts


def cf_bypass(driver, max_attempts: int = 10) -> bool:
    def _is_cf(d):
        t = (d.title or "").lower()
        return "just a moment" in t or "checking your browser" in t or "cloudflare" in t

    def _click(d):
        for src in ([d] + list(d.get_frames() or [])):
            try:
                cb = src.ele("tag:input@type=checkbox", timeout=1)
                if cb:
                    cb.click()
                    return True
            except Exception:
                pass
        return False

    for attempt in range(1, max_attempts + 1):
        if not _is_cf(driver):
            return True
        _cf_log.info(f"CF attempt {attempt}")
        _click(driver)
        time.sleep(2)
    return False


# ── Network listener helpers ──────────────────────────────────────────────────
def start_listener(driver):
    try:
        driver.listen.start()
        return driver.listen
    except Exception as e:
        cprint(f"[listen] Unavailable: {e}", 196)
        return None

def extract_media_url(raw_url: str) -> str | None:
    if SKIP_DOMAINS_RE.search(raw_url):
        mu = re.search(r'[?&]mu=([^&]+)', raw_url)
        if mu:
            media = unquote(mu.group(1))
            if MEDIA_RE.search(media):
                return media
        return None
    return raw_url if MEDIA_RE.search(raw_url) else None

def poll_listener(listener, captured: dict, timeout: int = 15) -> None:
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
            for p in (packet if isinstance(packet, (list, tuple)) else [packet]):
                url = extract_media_url(getattr(p, "url", "") or "")
                if url:
                    captured["url"] = url
                    cprint_url("listen", "Captured", url, 45)
                    return
        except Exception:
            time.sleep(0.3)


# ── Browser fetch (layer 2) ───────────────────────────────────────────────────
def drission_fetch(site: str) -> tuple:
    try:
        from DrissionPage import ChromiumPage
    except ImportError:
        cprint("[browser] DrissionPage not installed — pip install DrissionPage", 196)
        return None, None

    print("[browser] Launching Chrome...")
    captured = {"url": None}
    driver = ChromiumPage(addr_or_opts=chrome_opts())
    listener = start_listener(driver)

    try:
        driver.get(site)
        time.sleep(3)
        cf_bypass(driver)
        time.sleep(2)
        poll_listener(listener, captured, timeout=15)

        html = driver.html
        if not captured["url"]:
            m = MEDIA_RE.search(html)
            if m:
                captured["url"] = m.group(0)
                cprint_url("browser", "Found in HTML", captured["url"])

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
                    break
                fm = MEDIA_RE.search(driver.html)
                if fm:
                    captured["url"] = fm.group(0)
                    cprint_url("browser", "Found in iframe HTML", captured["url"])
                    break

        return html, captured["url"]

    except Exception as e:
        print(f"[browser] Error: {e}")
        return None, None
    finally:
        try: driver.quit()
        except Exception: pass


# ── Browser-intercept CDN download (token-bound) ──────────────────────────────
def browser_intercept_and_download(player_url: str, site_referer: str,
                                    out_fmt: str = "mp4") -> bool:
    try:
        from DrissionPage import ChromiumPage
    except ImportError:
        print("[intercept] DrissionPage not installed.")
        return False

    cprint_url("intercept", "Opening in Chrome", player_url, 208)
    captured = {"url": None}
    driver = ChromiumPage(addr_or_opts=chrome_opts())
    listener = start_listener(driver)

    try:
        driver.get(player_url)
        time.sleep(3)
        cf_bypass(driver)

        # X.com / Twitter needs extra time — CDN URL only fires after play
        is_twitter = any(h in player_url for h in ("x.com", "twitter.com", "t.co"))
        deadline = time.time() + (40 if is_twitter else 20)

        # X.com player selectors (aria-label is the most reliable, others as fallbacks)
        _XCOM_SELECTORS = [
            "css:[data-testid='videoPlayer'] video",
            "css:[data-testid='videoComponent']",
            "css:div[aria-label='Embedded video']",
            "css:div[role='progressbar']",               # timeline bar — click triggers play
            "css:video",                                  # bare <video> element
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
            # X.com often won't expose the CDN URL through network interception;
            # yt-dlp has a native Twitter extractor — try it on the original page URL.
            if is_twitter and ytdlp_ok():
                print("[intercept] No CDN URL — handing off to yt-dlp (Twitter extractor)...")
                fmt_sel, extra = yt_fmt_args(out_fmt)
                cmd = (
                    ["yt-dlp",
                     "--add-header", f"User-Agent:{UA}",
                     "--no-warnings", "--progress",
                     "-f", fmt_sel,
                     "-o", os.path.join(config.OUTPUT_DIR, "%(title)s.%(ext)s")]
                    + extra + [site_referer]          # site_referer is the original x.com URL
                )
                try:
                    result = subprocess.run(cmd, timeout=YTDLP_TIMEOUT)
                    return result.returncode == 0
                except subprocess.TimeoutExpired:
                    print("[intercept] yt-dlp timed out on Twitter URL.")
            print("[intercept] No CDN URL found.")
            return False

        cprint_url("intercept", "CDN URL", cdn_url)
        os.makedirs(config.OUTPUT_DIR, exist_ok=True)

        # Try yt-dlp first (no cookie loop — DPAPI is broken on Windows)
        if ytdlp_ok():
            fmt_sel, extra = yt_fmt_args(out_fmt)
            cmd = (
                ["yt-dlp",
                 "--referer", player_url,
                 "--add-header", f"User-Agent:{UA}",
                 "--extractor-args", "generic:impersonate",
                 "--no-warnings", "--progress",
                 "-f", fmt_sel,
                 "-o", os.path.join(config.OUTPUT_DIR, "%(title)s.%(ext)s")]
                + extra + [cdn_url]
            )
            print("[intercept] Trying yt-dlp...")
            try:
                if subprocess.run(cmd, capture_output=True,
                                  timeout=YTDLP_TIMEOUT).returncode == 0:
                    return True
                print("[intercept] yt-dlp failed, falling back to ffmpeg...")
            except subprocess.TimeoutExpired:
                print("[intercept] yt-dlp timed out, falling back to ffmpeg...")

        # ffmpeg fallback
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
