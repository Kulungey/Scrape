"""The scrape pipeline itself: walks

    URL -> platform detection -> direct HTTP -> extractor chain
        -> browser / Cloudflare -> network interception -> yt-dlp -> download

logging each stage and falling through to the next on failure.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

from . import config
from .config import base_headers, make_session
from .patterns import TOKEN_BOUND_RE
from .extractors import DEFAULT_CHAIN, extract_media_from_player
from .browser import drission_fetch, browser_intercept_and_download
from .downloader import safe_filename, download_file
from .ui import cprint, cprint_url
from .ytdlp import is_youtube, is_twitter, ytdlp_youtube, ytdlp_twitter, ytdlp_download, ytdlp_ok


def _simple_fetch(site: str) -> tuple:
    """Layer 1: plain HTTP fetch with a Chrome-shaped TLS/header fingerprint.
    No browser, no JS execution — cheapest layer, tried first."""
    sess = make_session(site)
    sess.headers.update(base_headers(site))
    root = f"{urlparse(site).scheme}://{urlparse(site).netloc}"
    if root.rstrip("/") != site.rstrip("/"):
        try:
            sess.get(root, timeout=15, allow_redirects=True)
        except Exception:
            pass
    resp = sess.get(site, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    return resp.text, None


def _intercept(player_or_site: str, site: str, out_fmt: str) -> bool:
    """browser_intercept_and_download, gated on --no-browser."""
    if not config.ALLOW_BROWSER:
        cprint("[!] Skipping browser intercept (--no-browser)", 208)
        return False
    return browser_intercept_and_download(player_or_site, site, out_fmt)


def _ytdlp_fallback(url: str, referer: str, out_fmt: str) -> bool:
    """ytdlp_download, gated on --no-ytdlp."""
    if not config.ALLOW_YTDLP:
        cprint("[!] Skipping yt-dlp (--no-ytdlp)", 208)
        return False
    return ytdlp_download(url, referer, out_fmt)


def scrape(site: str, out_fmt: str = "mp4") -> None:
    # YouTube shortcut
    if is_youtube(site):
        if not config.ALLOW_YTDLP:
            raise SystemExit("[!] YouTube requires yt-dlp, but --no-ytdlp is set.")
        cprint("[yt] YouTube detected — routing to yt-dlp", 226)
        sys.exit(0 if ytdlp_youtube(site, out_fmt) else 1)

    # Twitter/X shortcut — HTML scraping can't get the CDN URL without login;
    # yt-dlp's native Twitter extractor handles the GraphQL auth internally
    if is_twitter(site):
        if not config.ALLOW_YTDLP:
            raise SystemExit("[!] X.com/Twitter requires yt-dlp, but --no-ytdlp is set.")
        cprint("[twitter] X.com detected — routing to yt-dlp", 39)
        sys.exit(0 if ytdlp_twitter(site, out_fmt) else 1)

    # Layer 1: direct fetch
    html, video_url = None, None
    cprint(f"[1] Direct fetch: {site}", 118)
    try:
        html, video_url = _simple_fetch(site)
        cprint("[1] OK", 118)
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "?")
        cprint(f"[1] Failed (HTTP {status}) — browser mode", 196)

    # Layer 2: real Chrome + CF bypass
    if html is None:
        if not config.ALLOW_BROWSER:
            raise SystemExit("[!] Direct fetch failed and --no-browser is set.")
        cprint("[2] Browser mode...", 82)
        html, video_url = drission_fetch(site)
        if html is None:
            raise SystemExit("[!] All fetch modes failed.")

    # Layer 3: extractor chain — direct HTML match, then iframe/player
    cdn_referer = site
    player_iframe_url = None

    if not video_url:
        cprint("[3] Scanning HTML...", 154)
        for extractor in DEFAULT_CHAIN:
            result = extractor.extract(html, site)
            if result is None:
                continue
            if result.url:
                video_url = result.url
                break
            if result.referer:  # iframe extractor found a player page, not media yet
                player_iframe_url = result.referer
                cprint_url("3", "Player", player_iframe_url, 154)
                cdn_referer = player_iframe_url
                player_base = (f"{urlparse(player_iframe_url).scheme}://"
                               f"{urlparse(player_iframe_url).netloc}")
                try:
                    sess = make_session(site)
                    sess.headers.update(base_headers(site))
                    presp = sess.get(player_iframe_url, timeout=20, allow_redirects=True)
                    presp.raise_for_status()
                    media = extract_media_from_player(presp.text, player_base)
                    video_url = media.get("video")
                    if not video_url and media.get("player_url"):
                        presp2 = sess.get(media["player_url"], timeout=20, allow_redirects=True)
                        video_url = extract_media_from_player(presp2.text, player_base).get("video")
                except Exception as e:
                    cprint(f"[3] Player fetch error: {e}", 196)
                break

    # Token-bound URL detected
    if video_url and TOKEN_BOUND_RE.search(video_url):
        cprint("[!] Token-bound — browser intercept", 208)
        sys.exit(0 if _intercept(player_iframe_url or site, site, out_fmt) else 1)

    # No URL found — intercept then yt-dlp
    if not video_url:
        cprint("[!] No media URL — trying intercept...", 208)
        if _intercept(player_iframe_url or site, site, out_fmt):
            sys.exit(0)
        cprint("[!] Trying yt-dlp...", 208)
        sys.exit(0 if _ytdlp_fallback(site, site, out_fmt) else 1)

    # CDN domain mismatch — try intercept first
    if urlparse(video_url).netloc != urlparse(cdn_referer).netloc:
        cprint("[DL] CDN domain mismatch — trying intercept first...", 213)
        if _intercept(player_iframe_url or cdn_referer, site, out_fmt):
            cprint(f"\nDONE — {os.path.abspath(config.OUTPUT_DIR)}", 118)
            sys.exit(0)
        if config.ALLOW_YTDLP and ytdlp_ok():
            cprint("[DL] Intercept failed — trying yt-dlp on original URL...", 213)
            if _ytdlp_fallback(site, site, out_fmt):
                cprint(f"\nDONE — {os.path.abspath(config.OUTPUT_DIR)}", 118)
                sys.exit(0)
        cprint("[DL] Falling back to direct download...", 213)

    # Direct download
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    target_ext = f".{out_fmt}" if out_fmt else ".mp4"
    out_video = safe_filename(video_url, 1, ext=target_ext)
    cprint_url("DL", "Fetching", video_url, 213)
    cprint(f"[DL] Referer  -> {urlparse(cdn_referer).netloc}", 245)
    cprint(f"[DL] Output   -> {out_video}", 245)
    print(download_file(video_url, out_video, cdn_referer))
    cprint(f"\nDONE — {os.path.abspath(config.OUTPUT_DIR)}", 118)
