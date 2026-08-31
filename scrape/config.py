"""Shared constants, config, and the HTTP backend (curl_cffi if available,
plain requests as a fallback)."""

import os
from urllib.parse import urlparse

# ── Config ────────────────────────────────────────────────────────────────────
MAX_RETRIES    = 3
MIN_MB         = 2
YTDLP_TIMEOUT  = 3600
FFMPEG_TIMEOUT = 3600
STREAM_TIMEOUT = 30

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

AUDIO_FMTS = frozenset(("mp3", "aac", "flac", "opus", "m4a", "wav"))

# ── Ad-domain blocklist ─────────────────────────────────────────────────────
# Applied before navigation in both browser engines (DrissionPage/CDP and
# Playwright) so ad/tracker requests never load.
AD_BLOCK_DOMAINS = frozenset((
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "google-analytics.com", "adnxs.com", "adsrvr.org", "taboola.com",
    "outbrain.com", "popads.net", "propellerads.com", "exoclick.com",
    "juicyads.com", "trafficjunky.net", "adtng.com", "revcontent.com",
    "adsafeprotected.com", "moatads.com", "scorecardresearch.com",
    "onclickmax.com", "popcash.net", "adcash.com", "clicksor.com",
))

# CDP-style wildcard patterns, for DrissionPage's driver.set.blocked_urls().
AD_BLOCK_CDP_PATTERNS = tuple(f"*{d}*" for d in AD_BLOCK_DOMAINS)


def is_ad_domain(url: str) -> bool:
    """True if url's host matches (or is a subdomain of) a known ad domain."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(host == d or host.endswith(f".{d}") for d in AD_BLOCK_DOMAINS)

# ── Output directories: video/ and music/ (see dir_for()) ─────────────────────
VIDEO_DIR = "video"
MUSIC_DIR = "music"
OUTPUT_DIR = VIDEO_DIR  # back-compat default; prefer dir_for(out_fmt) at call sites

def dir_for(out_fmt: str) -> str:
    """Pick the video/ or music/ output directory for a given out_fmt.
    Audio formats (mp3, aac, flac, opus, m4a, wav) route to MUSIC_DIR;
    everything else (mp4, mkv, webm, original/"") routes to VIDEO_DIR."""
    return MUSIC_DIR if out_fmt in AUDIO_FMTS else VIDEO_DIR

# ── Runtime toggles (set by the CLI: --no-browser / --no-ytdlp / --output) ────
ALLOW_BROWSER = True
ALLOW_YTDLP = True

# Max video height (px) the user's quality selection caps downloads at.
# 1080 is the ceiling — never raised above that (see cli.QUALITY_CHOICES).
MAX_HEIGHT = 1080

# Audio bitrate (kbps) the user's audio-quality selection targets.
AUDIO_BITRATE = 320

def set_allow_browser(flag: bool) -> None:
    global ALLOW_BROWSER
    ALLOW_BROWSER = flag

def set_allow_ytdlp(flag: bool) -> None:
    global ALLOW_YTDLP
    ALLOW_YTDLP = flag

def set_max_height(height: int) -> None:
    global MAX_HEIGHT
    MAX_HEIGHT = height

def set_audio_bitrate(kbps: int) -> None:
    global AUDIO_BITRATE
    AUDIO_BITRATE = kbps

def set_output_dir(path: str) -> None:
    """--output override: video/ and music/ become subdirectories of path
    instead of top-level directories."""
    global VIDEO_DIR, MUSIC_DIR, OUTPUT_DIR
    VIDEO_DIR = os.path.join(path, "video")
    MUSIC_DIR = os.path.join(path, "music")
    OUTPUT_DIR = VIDEO_DIR

# ── HTTP backend: curl_cffi (preferred) or plain requests ─────────────────────
try:
    from curl_cffi import requests as cffi_requests
    _IMPERSONATE = "chrome124"

    def make_session(referer: str = "") -> cffi_requests.Session:
        s = cffi_requests.Session(impersonate=_IMPERSONATE)
        if referer:
            s.headers["Referer"] = referer
        return s

    def raw_get(url: str, headers: dict, stream: bool = False, timeout: int = 30):
        return cffi_requests.get(url, headers=headers, stream=stream,
                                 timeout=timeout, impersonate=_IMPERSONATE,
                                 allow_redirects=True)
    USING_CFFI = True

except ImportError:
    import requests as _req

    def make_session(referer: str = "") -> _req.Session:
        s = _req.Session()
        if referer:
            s.headers["Referer"] = referer
        return s

    def raw_get(url: str, headers: dict, stream: bool = False, timeout: int = 30):
        return _req.get(url, headers=headers, stream=stream,
                        timeout=timeout, allow_redirects=True)
    USING_CFFI = False


def resolve_redirect(url: str, timeout: int = 6) -> str:
    """Follow HTTP redirects to their final destination.

    Needed *before* yt-dlp URL matching or platform detection — yt-dlp
    matches the literal string you give it against each extractor's regex,
    it never follows a redirect first. Reddit's mobile share links
    (/r/<sub>/s/<code>) 301 to the real /comments/... URL that yt-dlp's
    Reddit regex actually expects; TikTok's vm.tiktok.com/vt.tiktok.com
    share links work the same way. Best-effort: on any failure, returns
    the original URL unchanged rather than raising.
    """
    try:
        resp = raw_get(url, headers={"User-Agent": UA}, stream=True, timeout=timeout)
        try:
            final_url = getattr(resp, "url", None)
        finally:
            resp.close()
        return final_url or url
    except Exception:
        return url


# ── Shared header builders ────────────────────────────────────────────────────
_BASE_HEADERS_STATIC = {
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

def base_headers(referer: str = "") -> dict:
    return {"User-Agent": UA, "Referer": referer, **_BASE_HEADERS_STATIC}

def cdn_headers(referer: str, cf_session: dict | None = None) -> dict:
    parsed = urlparse(referer)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    h = {
        "User-Agent":     (cf_session or {}).get("ua", UA),
        "Referer":        referer,
        "Origin":         origin,
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }
    if cf_session and cf_session.get("cookies"):
        h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cf_session["cookies"].items())
    return h

def ffmpeg_hdr_block(referer: str, cf_session: dict | None = None) -> str:
    h = cdn_headers(referer, cf_session=cf_session)
    block = (
        f"Referer: {h['Referer']}\r\n"
        f"Origin: {h['Origin']}\r\n"
        f"User-Agent: {h['User-Agent']}\r\n"
        f"Sec-Fetch-Dest: video\r\n"
        f"Sec-Fetch-Mode: no-cors\r\n"
        f"Sec-Fetch-Site: cross-site\r\n"
    )
    if "Cookie" in h:
        block += f"Cookie: {h['Cookie']}\r\n"
    return block
