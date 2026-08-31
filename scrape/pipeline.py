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
from .config import base_headers, make_session, resolve_redirect
from .patterns import TOKEN_BOUND_RE
from .extractors import DEFAULT_CHAIN, extract_media_from_player
from .browser import (drission_fetch, browser_intercept_and_download, get_cf_session,
                       playwright_intercept_and_download, PLAYWRIGHT_DOWNLOADED,
                       page_is_blocked)
from .solverr import solverr_fetch
from .docker_manager import ensure_solverr_ready
from .downloader import safe_filename, download_file
from .ui import cprint, cprint_url, print_mascot_success, print_mascot_fail
from .ytdlp import (is_youtube, is_twitter, is_vimeo, is_dailymotion,
                    is_reddit, is_tiktok, is_twitch, is_spotify,
                    ytdlp_youtube, ytdlp_twitter, ytdlp_vimeo,
                    spotdl_download, spotdl_ok,
                    ytdlp_download, ytdlp_ok, ytdlp_probe)

# Domains with a known iframe embed that yt-dlp natively understands — worth
# a probe on the iframe URL itself even when the *hosting* page is unknown.
KNOWN_EMBED_HOSTS = ("player.vimeo.com", "vimeo.com", "dailymotion.com",
                     "player.twitch.tv")


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


def _intercept(player_or_site: str, site: str, out_fmt: str,
               cf_session: dict | None = None) -> bool:
    """browser_intercept_and_download, gated on --no-browser.
    Tries DrissionPage headless → headed → Playwright intercept in order.
    cf_session — pre-inject CF cookies so the new Chrome session skips the challenge.
    """
    if not config.ALLOW_BROWSER:
        cprint("[!] Skipping browser intercept (--no-browser)", 208)
        return False
    if browser_intercept_and_download(player_or_site, site, out_fmt,
                                      headless=True, cf_session=cf_session):
        return True
    cprint("[intercept] Headless intercept found nothing — retrying headed...", 208)
    if browser_intercept_and_download(player_or_site, site, out_fmt,
                                      headless=False, cf_session=cf_session):
        return True
    # DrissionPage exhausted — try Playwright which downloads in-session,
    # avoiding the TLS fingerprint mismatch that kills CF-gated cookie handoffs.
    cprint("[intercept] DrissionPage exhausted — trying Playwright intercept...", 208)
    return playwright_intercept_and_download(player_or_site, out_fmt=out_fmt)


def _ytdlp_fallback(url: str, referer: str, out_fmt: str,
                    cf_session: dict | None = None) -> bool:
    """ytdlp_download, gated on --no-ytdlp.

    cf_session — if provided, cookies are written to a temp Netscape cookie
    file and passed to yt-dlp via --cookies so it rides the same CF-cleared
    session the browser already established.
    """
    if not config.ALLOW_YTDLP:
        cprint("[!] Skipping yt-dlp (--no-ytdlp)", 208)
        return False
    return ytdlp_download(url, referer, out_fmt, cf_session=cf_session)


def scrape(site: str, out_fmt: str = "mp4") -> None:
    # Step 0: resolve shortlinks/redirects (Reddit /s/ share links, TikTok
    # vm.tiktok.com, bit.ly, t.co, etc.) to their final URL *before* any
    # platform detection or yt-dlp probing — both match against the literal
    # string given to them and never follow a redirect first.
    resolved = resolve_redirect(site)
    if resolved != site:
        cprint(f"[0] Resolved redirect -> {urlparse(resolved).netloc}{urlparse(resolved).path}", 245)
        site = resolved

    # Spotify shortcut — yt-dlp has no Spotify extractor; spotdl handles it
    # by matching the track on YouTube Music and downloading with full metadata.
    if is_spotify(site):
        cprint("[spotify] Spotify detected — routing to spotdl", 83)
        if not spotdl_ok():
            cprint("[spotify] spotdl not installed — pip install spotdl", 196)
            sys.exit(1)
        # Spotify is always audio; ignore non-audio format choices gracefully
        audio_fmt = out_fmt if out_fmt in {"mp3", "flac", "ogg", "opus", "m4a", "wav"} else "mp3"
        if out_fmt != audio_fmt:
            cprint(f"[spotify] {out_fmt!r} is a video format — using mp3 for audio", 208)
        ok = spotdl_download(site, audio_fmt)
        if ok:
            print_mascot_success()
            cprint(f"\nDONE — {os.path.abspath(config.dir_for(audio_fmt))}", 118)
        else:
            print_mascot_fail()
            cprint("\n[!] Spotify download failed.", 196)
        sys.exit(0 if ok else 1)

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

    # Fast-path shortcuts for well-known platforms that yt-dlp handles
    # natively — skip the probe overhead entirely for these.
    # Vimeo: try yt-dlp with cookie escalation first (handles public + logged-in
    # private videos).  If all cookie attempts fail (no Vimeo session in any
    # browser), fall through to the browser intercept layer — DrissionPage can
    # open the page, trigger playback, and sniff the CDN URL from network traffic.
    if is_vimeo(site):
        if config.ALLOW_YTDLP and ytdlp_vimeo(site, out_fmt):
            sys.exit(0)
        if not config.ALLOW_BROWSER:
            raise SystemExit("[!] Vimeo yt-dlp failed and --no-browser is set.")
        cprint("[vimeo] yt-dlp exhausted — falling through to browser intercept", 208)
        sys.exit(0 if _intercept(site, site, out_fmt) else 1)

    # TikTok: yt-dlp's native TikTok extractor scrapes the page's embedded
    # JSON blob, and TikTok periodically restructures that page specifically
    # to break scrapers ("Unable to extract universal data for rehydration"
    # is the recurring symptom). Rather than depend on that one path, fall
    # through to browser intercept on failure — DrissionPage plays the page
    # for real and sniffs the CDN URL off actual network traffic, which
    # doesn't care what shape TikTok's page JSON is in.
    if is_tiktok(site):
        if config.ALLOW_YTDLP and _ytdlp_fallback(site, site, out_fmt):
            sys.exit(0)
        if not config.ALLOW_BROWSER:
            raise SystemExit("[!] TikTok yt-dlp failed and --no-browser is set.")
        cprint("[tiktok] yt-dlp exhausted — falling through to browser intercept", 213)
        sys.exit(0 if _intercept(site, site, out_fmt) else 1)

    _KNOWN_PLATFORMS = (
        (is_dailymotion,  "Dailymotion",  208),
        (is_reddit,       "Reddit",       208),
        (is_twitch,       "Twitch",       141),
    )
    for detector, label, col in _KNOWN_PLATFORMS:
        if detector(site):
            if not config.ALLOW_YTDLP:
                raise SystemExit(f"[!] {label} requires yt-dlp, but --no-ytdlp is set.")
            cprint(f"[{label.lower()}] {label} detected — routing to yt-dlp", col)
            sys.exit(0 if _ytdlp_fallback(site, site, out_fmt) else 1)

    # ── Unknown site pipeline ────────────────────────────────────────────────
    # Order: yt-dlp probe → browser (fetch + intercept) → extractor chain
    #        → direct HTTP fetch (last resort — cheapest but least capable;
    #          most JS-heavy sites return HTTP 0 on a plain GET anyway).

    # Step 1: generic yt-dlp probe — catches Bilibili, playlists, HLS/DASH
    # streams, and any of yt-dlp's ~1800 other native extractors.
    if config.ALLOW_YTDLP and ytdlp_ok() and ytdlp_probe(site):
        cprint("[probe] yt-dlp recognizes this site — handing off", 226)
        sys.exit(0 if _ytdlp_fallback(site, site, out_fmt) else 1)

    # Step 2: browser fetch — handles Cloudflare, JS-rendered pages, and
    # anything a plain HTTP GET can't reach.  Try this before direct fetch
    # because most sites that need the browser return HTTP 0 to curl anyway.
    html, video_url = None, None
    cdn_referer = site
    player_iframe_url = None
    cf_session = None

    if config.ALLOW_BROWSER:
        cprint("[2] Browser mode...", 82)
        html, video_url, cf_session, browser_referer = drission_fetch(site, out_fmt=out_fmt)
        if browser_referer:
            # Media was found via an embed/iframe page (e.g. a third-party
            # player) rather than the top-level site — CDN tokens are
            # commonly locked to that embed page, so use it as Referer for
            # every downstream intercept/download attempt instead of `site`.
            cdn_referer = browser_referer
            player_iframe_url = browser_referer

        # Playwright already downloaded in-session (CF TLS-fingerprint binding)
        if video_url == PLAYWRIGHT_DOWNLOADED:
            cprint(f"\nDONE — {os.path.abspath(config.dir_for(out_fmt))}", 118)
            sys.exit(0)

        if cf_session and not video_url:
            # Browser cleared CF but didn't find media — try yt-dlp with cookies.
            # NOTE: this only works for sites that don't bind clearance to the
            # browser's TLS fingerprint. For those (like missav.to) the yt-dlp
            # attempt will 403 and we fall through to the Playwright intercept
            # which downloads in-session and avoids the mismatch entirely.
            cprint("[2] CF cleared — trying yt-dlp with session cookies...", 51)
            if _ytdlp_fallback(site, site, out_fmt, cf_session=cf_session):
                cprint(f"\nDONE — {os.path.abspath(config.dir_for(out_fmt))}", 118)
                sys.exit(0)

        # Step 2b: Solverr fallback. Only kicks in when the Chromium stack
        # (DrissionPage + Playwright) came back blocked or empty, and only
        # if a Solverr/Byparr container is actually running on localhost —
        # otherwise this is skipped instantly and nothing changes.
        if not video_url and page_is_blocked(html) and ensure_solverr_ready():
            cprint("[2b] Chromium stack blocked — trying Solverr...", 208)
            sv_html, sv_cf_session, sv_err = solverr_fetch(site)
            if sv_err:
                cprint(f"[2b] Solverr: {sv_err}", 196)
            elif sv_html and not page_is_blocked(sv_html):
                html = sv_html
                cf_session = sv_cf_session or cf_session
                if cf_session and not video_url:
                    cprint("[2b] Solverr cleared CF — trying yt-dlp with session cookies...", 51)
                    if _ytdlp_fallback(site, site, out_fmt, cf_session=cf_session):
                        cprint(f"\nDONE — {os.path.abspath(config.dir_for(out_fmt))}", 118)
                        sys.exit(0)

    # Step 3: extractor chain on browser HTML (if we got any)
    if html and not video_url:
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

                # Known embed host (Vimeo, Dailymotion, Twitch clips, etc.)?
                # Probe it directly with the page's Referer before falling
                # back to raw HTML parsing.
                if (config.ALLOW_YTDLP and ytdlp_ok()
                        and any(h in player_iframe_url for h in KNOWN_EMBED_HOSTS)
                        and ytdlp_probe(player_iframe_url, referer=site)):
                    cprint("[probe] Embedded player recognized — handing off", 226)
                    sys.exit(0 if ytdlp_download(player_iframe_url, site, out_fmt) else 1)

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

    # Token-bound URL detected — must intercept regardless of how we got here
    if video_url and TOKEN_BOUND_RE.search(video_url):
        cprint("[!] Token-bound — browser intercept", 208)
        sys.exit(0 if _intercept(player_iframe_url or site, site, out_fmt, cf_session=cf_session) else 1)

    # No URL yet — try browser network intercept
    if not video_url:
        cprint("[!] No media URL — trying browser intercept...", 208)
        if _intercept(player_iframe_url or site, site, out_fmt, cf_session=cf_session):
            sys.exit(0)

        # Step 4: direct HTTP fetch — last resort; works for simple pages
        # that serve plain HTML without JS gating (rare but it does happen).
        cprint("[4] Direct fetch (last resort)...", 118)
        try:
            html, video_url = _simple_fetch(site)
            cprint("[4] OK", 118)
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", "?")
            cprint(f"[4] Failed (HTTP {status})", 196)

        if html and not video_url:
            cprint("[4] Scanning HTML...", 154)
            for extractor in DEFAULT_CHAIN:
                result = extractor.extract(html, site)
                if result and result.url:
                    video_url = result.url
                    break

        if not video_url:
            cprint("[!] All layers failed.", 196)
            sys.exit(1)

    # CDN domain mismatch — try intercept first, then direct download
    if urlparse(video_url).netloc != urlparse(cdn_referer).netloc:
        cprint("[DL] CDN domain mismatch — trying intercept first...", 213)
        if _intercept(player_iframe_url or cdn_referer, site, out_fmt, cf_session=cf_session):
            cprint(f"\nDONE — {os.path.abspath(config.dir_for(out_fmt))}", 118)
            sys.exit(0)
        if config.ALLOW_YTDLP and ytdlp_ok():
            cprint("[DL] Intercept failed — trying yt-dlp on original URL...", 213)
            if _ytdlp_fallback(site, site, out_fmt):
                cprint(f"\nDONE — {os.path.abspath(config.dir_for(out_fmt))}", 118)
                sys.exit(0)
        cprint("[DL] Falling back to direct download...", 213)

    # Direct download
    os.makedirs(config.dir_for(out_fmt), exist_ok=True)
    target_ext = f".{out_fmt}" if out_fmt else ".mp4"
    out_video = safe_filename(video_url, 1, ext=target_ext)
    cprint_url("DL", "Fetching", video_url, 213)
    cprint(f"[DL] Referer  -> {urlparse(cdn_referer).netloc}", 245)
    cprint(f"[DL] Output   -> {out_video}", 245)
    print(download_file(video_url, out_video, cdn_referer, cf_session=cf_session))
    cprint(f"\nDONE — {os.path.abspath(config.dir_for(out_fmt))}", 118)
