"""Shared constants, config, and the HTTP backend (curl_cffi if available,
plain requests as a fallback)."""

from urllib.parse import urlparse

# ── Config ────────────────────────────────────────────────────────────────────
OUTPUT_DIR     = "videos"
MAX_RETRIES    = 3
MIN_MB         = 2
YTDLP_TIMEOUT  = 3600
FFMPEG_TIMEOUT = 3600
STREAM_TIMEOUT = 30

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

AUDIO_FMTS = frozenset(("mp3", "aac", "flac", "opus", "m4a", "wav"))

# ── Runtime toggles (set by the CLI: --no-browser / --no-ytdlp / --output) ────
ALLOW_BROWSER = True
ALLOW_YTDLP = True

def set_allow_browser(flag: bool) -> None:
    global ALLOW_BROWSER
    ALLOW_BROWSER = flag

def set_allow_ytdlp(flag: bool) -> None:
    global ALLOW_YTDLP
    ALLOW_YTDLP = flag

def set_output_dir(path: str) -> None:
    global OUTPUT_DIR
    OUTPUT_DIR = path

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

def cdn_headers(referer: str) -> dict:
    parsed = urlparse(referer)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return {
        "User-Agent":     UA,
        "Referer":        referer,
        "Origin":         origin,
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }

def ffmpeg_hdr_block(referer: str) -> str:
    h = cdn_headers(referer)
    return (
        f"Referer: {h['Referer']}\r\n"
        f"Origin: {h['Origin']}\r\n"
        f"User-Agent: {UA}\r\n"
        f"Sec-Fetch-Dest: video\r\n"
        f"Sec-Fetch-Mode: no-cors\r\n"
        f"Sec-Fetch-Site: cross-site\r\n"
    )
