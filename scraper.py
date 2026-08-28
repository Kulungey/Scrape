"""
scraper.py  —  video downloader with Cloudflare bypass

Layers (in order):
  1. curl_cffi direct fetch  (Chrome TLS fingerprint)
  2. Real Chrome via DrissionPage  (CF bypass + network interception)
  3. HTML/iframe scan + base64 decode
  4. yt-dlp generic fallback

YouTube is detected early and routed straight to yt-dlp, skipping all layers.
Token-bound CDN URLs trigger the browser-intercept path automatically.

Install:
  pip install DrissionPage curl_cffi yt-dlp
  ffmpeg must be on PATH (winget install ffmpeg)
  Chrome must be installed
"""

import os, sys, re, time, math, shutil, base64, subprocess, logging, colorsys
from urllib.parse import urlparse, urljoin, unquote

# ── HTTP backend: curl_cffi (preferred) or plain requests ─────────────────────
try:
    from curl_cffi import requests as cffi_requests
    _IMPERSONATE = "chrome124"

    def _make_session(referer: str = "") -> cffi_requests.Session:
        s = cffi_requests.Session(impersonate=_IMPERSONATE)
        if referer:
            s.headers["Referer"] = referer
        return s

    def _raw_get(url: str, headers: dict, stream: bool = False, timeout: int = 30):
        return cffi_requests.get(url, headers=headers, stream=stream,
                                 timeout=timeout, impersonate=_IMPERSONATE,
                                 allow_redirects=True)
    USING_CFFI = True

except ImportError:
    import requests as _req

    def _make_session(referer: str = "") -> _req.Session:
        s = _req.Session()
        if referer:
            s.headers["Referer"] = referer
        return s

    def _raw_get(url: str, headers: dict, stream: bool = False, timeout: int = 30):
        return _req.get(url, headers=headers, stream=stream,
                        timeout=timeout, allow_redirects=True)
    USING_CFFI = False

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

# ── ASCII banner (solid block glyphs, animated rainbow sweep) ─────────────────
LOGO = r"""
███████╗ ██████╗██████╗  █████╗ ██████╗ ███████╗
██╔════╝██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔════╝
███████╗██║     ██████╔╝███████║██████╔╝█████╗
╚════██║██║     ██╔══██╗██╔══██║██╔═══╝ ██╔══╝
███████║╚██████╗██║  ██║██║  ██║██║     ███████╗
╚══════╝ ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝     ╚══════╝
"""

# 256-color ramp the sweep animation cycles through (warm -> cool -> warm)
_WAVE_COLORS = [196, 202, 208, 214, 220, 226, 190, 154, 118, 82,
                46, 47, 48, 49, 50, 51, 45, 39, 33, 27,
                21, 57, 93, 129, 165, 201, 199, 198, 197]

def _ansi_ready() -> bool:
    """True if the terminal can render ANSI escapes; enables them on Windows."""
    if os.name == "nt":
        os.system("")  # no-op that flips on VT100 processing in modern conhost
    return sys.stdout.isatty()

def print_logo(frames: int = 16, delay: float = 0.035) -> None:
    """Print the banner. Animates a color sweep across it in a real terminal,
    falls back to a plain static print anywhere ANSI isn't supported (piped
    output, dumb terminals, etc.)."""
    if not _ansi_ready():
        print(LOGO)
        return
    lines = LOGO.strip("\n").splitlines()
    n = len(_WAVE_COLORS)
    out = sys.stdout
    try:
        out.write("\033[?25l")  # hide cursor
        for frame in range(frames):
            buf = []
            for row, line in enumerate(lines):
                chars = []
                for col, ch in enumerate(line):
                    if ch == " ":
                        chars.append(" ")
                    else:
                        idx = (col // 2 + row - frame) % n
                        chars.append(f"\033[38;5;{_WAVE_COLORS[idx]}m{ch}")
                buf.append("".join(chars) + "\033[0m")
            out.write("\033[H" + "\n".join(buf) + "\n")
            out.flush()
            time.sleep(delay)
    finally:
        out.write("\033[0m\033[?25h")  # reset color, restore cursor
        out.flush()

# ── ASCII mascot (in-place looping frame animation) ────────────────────────────
def _play_frames(frames: list, loops: int = 1, delay: float = 0.12) -> None:
    """Redraw a sequence of multi-line ASCII frames in place, looping `loops`
    times, then leave the final frame on screen. Falls back to a single
    static print of the last frame when ANSI cursor moves aren't supported."""
    frame_lines = [f.splitlines() for f in frames]
    if not _ansi_ready():
        print("\n".join(frame_lines[-1]))
        return
    out = sys.stdout
    try:
        out.write("\033[?25l")
        first = True
        for _ in range(loops):
            for lines in frame_lines:
                if not first:
                    out.write(f"\033[{len(lines)}A")  # cursor up to overwrite
                first = False
                for line in lines:
                    out.write("\033[2K" + line + "\n")  # clear line, redraw
                out.flush()
                time.sleep(delay)
    finally:
        out.write("\033[0m\033[?25h")
        out.flush()

# tears wiggle side to side and drip down, ends in a little splash
_CRY_FRAMES = [
    " (╥﹏╥) \n   ,    \n        ",
    "(╥﹏╥)  \n    '   \n        ",
    "  (╥﹏╥)\n  ,     \n   .    ",
    " (╥﹏╥) \n     `  \n    .   ",
    " (╥﹏╥) \n        \n  ~*~*~ ",
]

# cartoonish squeeze-and-stretch jump for hops
_HAPPY_FRAMES = [
    "  ___   \n (^▽^)  \n ▔▔▔▔▔  ",
    " \\(^▽^)/\n  |   |  \n         ",
    " \\(★▽★)/\n    ✧    \n         ",
    "  ___   \n (^▽^)  \n ▔▔▔▔▔  ",
]

# idle chin-scrub "pondering" loop
_THINKING_FRAMES = [
    " (≖‿≖ )⌐\n        ",
    " (≖‿≖ )~\n        ",
    " ( ≖o≖ )\n    ⌐   ",
]

def print_mascot_fail() -> None:
    _play_frames(_CRY_FRAMES, loops=2, delay=0.15)

def print_mascot_success() -> None:
    _play_frames(_HAPPY_FRAMES, loops=2, delay=0.11)

def print_mascot_thinking() -> None:
    _play_frames(_THINKING_FRAMES, loops=2, delay=0.25)

# ── HSV -> truecolor helper (shared by border / progress bar / press-key) ────
def _rgb(h: float, s: float, v: float) -> str:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, max(0.0, min(1.0, v)))
    return f"\033[38;2;{int(r*255)};{int(g*255)};{int(b*255)}m"

# ── Single-owner key polling (no threads, no race — see mascot_demo.py notes) ─
class _RawStdin:
    """Puts the terminal in cbreak mode (unix) so keys are readable one at a
    time without waiting for Enter, and without the tty auto-echoing them.
    No-op on Windows; msvcrt already reads raw per-key."""
    def __enter__(self):
        self.enabled = False
        if os.name != "nt" and sys.stdin.isatty():
            import termios, tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            self.enabled = True
        return self

    def __exit__(self, *exc):
        if self.enabled:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

def _poll_key():
    """Non-blocking: return one character if a key is waiting, else None."""
    if os.name == "nt":
        import msvcrt
        if msvcrt.kbhit():
            return msvcrt.getwch()
        return None
    else:
        import select
        if not sys.stdin.isatty():
            return None
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if r:
            return sys.stdin.read(1)
        return None

_BACKSPACE = {"\x08", "\x7f"}
_ENTER = {"\r", "\n"}

def _live_prompt(render_fn, n_lines: int, on_submit, frame_delay: float = 0.05) -> str:
    """The core engine: one loop, one thread, one writer. Every tick it
    either processes a waiting keystroke or redraws the next animation
    frame — never both racing each other (this is the fix for the
    duplicate-border bug from the old background-thread version).
    `render_fn(buf, error)` returns the full box text. `on_submit(buf)`
    returns None to accept, or an error string to reject and re-prompt."""
    out = sys.stdout
    if not _ansi_ready():
        while True:
            buf = input(render_fn("", "") + "\n> ")
            err = on_submit(buf)
            if err is None:
                return buf
            print(f"[!] {err}")

    buf, error = "", ""
    out.write("\033[?25l")
    printed_once = False
    try:
        while True:
            frame = render_fn(buf, error)
            lines = frame.splitlines()
            if printed_once:
                out.write(f"\033[{n_lines}A")
            for line in lines:
                out.write("\033[2K" + line + "\n")
            out.flush()
            printed_once = True

            key = _poll_key()
            if key is None:
                time.sleep(frame_delay)
                continue
            if key in _ENTER:
                err = on_submit(buf)
                if err is None:
                    return buf
                error = err
                buf = ""
            elif key in _BACKSPACE:
                buf = buf[:-1]
                error = ""
            elif key.isprintable():
                buf += key
                error = ""
    finally:
        out.write("\033[0m\033[?25h")
        out.flush()

def input_with_breathing_menu(title: str, options: list, valid: set, default: str = "1") -> str:
    """Bordered menu box with a continuously breathing rainbow border while
    it waits on input. Invalid choices re-prompt in place with an inline
    error instead of silently accepting garbage or crashing."""
    body_width = max(len(title), max(len(o) for o in options), 24) + 2
    t0 = time.time()

    def side(text: str) -> str:
        return f"║ {text.ljust(body_width - 1)}║"

    def border_row(is_top: bool, elapsed: float) -> str:
        hue_shift = (elapsed * 0.12) % 1.0
        breath = 0.55 + 0.45 * math.sin(elapsed * 2.2)
        corners = ("╔", "╗") if is_top else ("╚", "╝")
        chars = [corners[0]]
        for i in range(body_width):
            hue = (i / body_width + hue_shift) % 1.0
            chars.append(_rgb(hue, 0.85, breath) + "═")
        chars.append("\033[0m" + corners[1])
        return "".join(chars)

    def render(buf: str, error: str) -> str:
        elapsed = time.time() - t0
        rows = [title, ""] + options + ["", f"Choice [{default}]: {buf}", "",
                                         (f"\033[91m{error}\033[0m" if error else "")]
        body = [side(r) for r in rows]
        return "\n".join([border_row(True, elapsed)] + body + [border_row(False, elapsed)])

    n_lines = len(render("", "").splitlines())

    def on_submit(buf: str):
        choice = buf.strip() or default
        if choice not in valid:
            return f"use 1–{max(valid, key=int)}, got '{choice}'"
        return None

    return _live_prompt(render, n_lines, on_submit).strip() or default

# ── Rainbow byte-accurate download progress bar ──────────────────────────────
def render_progress_bar(done: int, total: int, width: int = 40, elapsed: float = 0.0) -> str:
    pct = 0.0 if total <= 0 else min(1.0, done / total)
    filled = int(width * pct)
    hue_shift = (elapsed * 0.15) % 1.0
    bar_chars = []
    for i in range(width):
        if i < filled:
            hue = (i / width + hue_shift) % 1.0
            bar_chars.append(_rgb(hue, 0.85, 0.95) + "█")
        else:
            bar_chars.append("\033[38;5;238m░")
    bar = "".join(bar_chars) + "\033[0m"

    def _fmt_mb(n: int) -> str:
        return f"{n / (1024 * 1024):.1f}MB"

    return f"[{bar}] {pct*100:5.1f}%  {_fmt_mb(done)}/{_fmt_mb(total)}"

def render_time_progress_bar(done_sec: float, total_sec: float, width: int = 40, elapsed: float = 0.0) -> str:
    """Same visual style as render_progress_bar, but driven by playback
    time processed rather than bytes — for sources (m3u8/HLS) where the
    real byte total isn't knowable up front but ffmpeg reports duration."""
    pct = 0.0 if total_sec <= 0 else min(1.0, done_sec / total_sec)
    filled = int(width * pct)
    hue_shift = (elapsed * 0.15) % 1.0
    bar_chars = []
    for i in range(width):
        if i < filled:
            hue = (i / width + hue_shift) % 1.0
            bar_chars.append(_rgb(hue, 0.85, 0.95) + "█")
        else:
            bar_chars.append("\033[38;5;238m░")
    bar = "".join(bar_chars) + "\033[0m"

    def _fmt_t(s: float) -> str:
        s = max(0, int(s))
        return f"{s // 60:02d}:{s % 60:02d}"

    return f"[{bar}] {pct*100:5.1f}%  {_fmt_t(done_sec)}/{_fmt_t(total_sec)}"

def _probe_duration(url: str, referer: str) -> float:
    """Best-effort ffprobe duration lookup, in seconds. Returns 0.0 if it
    can't be determined (some m3u8 sources refuse to report it too, at
    which point we just fall back to indeterminate/no-bar)."""
    if not shutil.which("ffprobe"):
        return 0.0
    try:
        cmd = ["ffprobe", "-v", "error", "-headers", _ffmpeg_hdr_block(referer),
               "-show_entries", "format=duration", "-of", "csv=p=0", url]
        r = subprocess.run(cmd, capture_output=True, timeout=15, text=True)
        return float(r.stdout.strip())
    except Exception:
        return 0.0

# ── Animated "press any key to close" ─────────────────────────────────────────
def press_any_key_to_close(message: str = "Press any key to close...", frame_delay: float = 0.05) -> None:
    if not _ansi_ready():
        input(message + " ")
        return
    out = sys.stdout
    t0 = time.time()
    out.write("\033[?25l")
    try:
        with _RawStdin():
            printed_once = False
            while True:
                elapsed = time.time() - t0
                breath = 0.5 + 0.5 * math.sin(elapsed * 3.0)
                hue = (elapsed * 0.2) % 1.0
                colored = _rgb(hue, 0.8, 0.5 + 0.5 * breath) + message + "\033[0m"
                if printed_once:
                    out.write("\033[2K\r")
                out.write(colored)
                out.flush()
                printed_once = True
                key = _poll_key()
                if key is not None:
                    out.write("\n")
                    return
                time.sleep(frame_delay)
    finally:
        out.write("\033[0m\033[?25h")
        out.flush()

# ── Idle logo replay (single-loop — waits on a key, replays sweep every ~5s) ──
def wait_for_site_input_with_idle_logo(replay_every: float = 5.0) -> str:
    """Sits at 'Site URL: ' prompt. If the user hasn't typed anything for
    `replay_every` seconds, the logo does one rainbow sweep in place above
    the prompt line, then returns to waiting — all in the same single
    loop/single writer that reads keystrokes, so no race with typing."""
    if not _ansi_ready():
        return input("Site URL: ").strip()

    lines = LOGO.strip("\n").splitlines()
    n_logo = len(lines)
    n_wave = len(_WAVE_COLORS)
    out = sys.stdout
    buf = ""
    t0 = time.time()
    last_key_t = t0
    sweep_frame = 0
    printed_once = False
    out.write("\033[?25l")
    try:
        while True:
            now = time.time()
            idle = now - last_key_t
            sweeping = idle >= replay_every

            logo_rows = []
            for row, line in enumerate(lines):
                chars = []
                for col, ch in enumerate(line):
                    if ch == " ":
                        chars.append(" ")
                    else:
                        idx = (col // 2 + row - (sweep_frame if sweeping else 0)) % n_wave
                        chars.append(f"\033[38;5;{_WAVE_COLORS[idx]}m{ch}")
                logo_rows.append("".join(chars) + "\033[0m")
            prompt_row = f"Site URL: {buf}"
            frame = "\n".join(logo_rows + [prompt_row])
            n_lines = n_logo + 1

            if printed_once:
                out.write(f"\033[{n_lines}A")
            for line in frame.splitlines():
                out.write("\033[2K" + line + "\n")
            out.flush()
            printed_once = True

            if sweeping:
                sweep_frame += 1
                if sweep_frame >= 16:  # one full sweep, then rest until idle timer resets
                    sweep_frame = 0
                    last_key_t = now  # restart the 5s idle countdown after a replay

            key = _poll_key()
            if key is None:
                time.sleep(0.035 if sweeping else 0.05)
                continue
            last_key_t = time.time()
            sweep_frame = 0
            if key in _ENTER:
                out.write("\n")
                return buf.strip()
            elif key in _BACKSPACE:
                buf = buf[:-1]
            elif key.isprintable():
                buf += key
    finally:
        out.write("\033[0m\033[?25h")
        out.flush()

# ── Compiled regexes (module-level — compiled once) ───────────────────────────
MEDIA_RE = re.compile(
    r'https?://[^\s"\'<>{}\[\]]+\.(?:mp4|m3u8|mpd)(?:[?#][^\s"\'<>]*)?',
    re.I
)
SKIP_DOMAINS_RE = re.compile(
    r'jwpltx\.com|google-analytics|doubleclick|googlesyndication'
    r'|facebook\.com|twitter\.com|scorecardresearch|omtrdc\.net',
    re.I
)
TOKEN_BOUND_RE  = re.compile(r'\|\d{9,10}\|[0-9a-f]{16,}', re.I)
IFRAME_RE       = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
IFRAME_SKIP_RE  = re.compile(
    r'google\.com/recaptcha|accounts\.google|facebook\.com/plugins'
    r'|twitter\.com/i/|disqus\.com|google|facebook|disqus',
    re.I
)
_DIRECT_RE = re.compile(
    r'(?:(?:file|src|source|href|data-src)["\s]*[:=]["\s]*|["\'])'
    r'["\']?(https?://[^\s"\'<>{}\[\]]+\.(?:mp4|m3u8|mpd)(?:[?#][^\s"\'<>]*)?)',
    re.I,
)
YT_RE = re.compile(
    r'(?:https?://)?(?:www\.|m\.)?'
    r'(?:youtube\.com/(?:watch|shorts|live|embed)|youtu\.be/)',
    re.I
)

# ── Cached tool checks ────────────────────────────────────────────────────────
_FFMPEG: bool | None = None
_YTDLP:  bool | None = None

def ffmpeg_ok() -> bool:
    global _FFMPEG
    if _FFMPEG is None:
        _FFMPEG = shutil.which("ffmpeg") is not None
    return _FFMPEG

def ytdlp_ok() -> bool:
    global _YTDLP
    if _YTDLP is None:
        _YTDLP = shutil.which("yt-dlp") is not None
    return _YTDLP

# ── Dependency update check ───────────────────────────────────────────────────
def quick_update_check() -> None:
    """Fast update check: ask yt-dlp if it needs updating (single process,
    no PyPI round-trips). Skips silently if yt-dlp not installed or offline."""
    if not ytdlp_ok():
        return
    try:
        # --update-to stable checks GitHub releases — fast, single request
        r = subprocess.run(
            ["yt-dlp", "--update-to", "stable"],
            capture_output=True, text=True, timeout=10
        )
        out = (r.stdout + r.stderr).strip()
        # yt-dlp prints "yt-dlp is up to date" or "Updated to <ver>"
        if "up to date" not in out.lower() and "updated" in out.lower():
            print(f"[update] {out.splitlines()[0]}")
    except Exception:
        pass  # offline or timeout — silent skip

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

def _base_headers(referer: str = "") -> dict:
    return {"User-Agent": UA, "Referer": referer, **_BASE_HEADERS_STATIC}

def _cdn_headers(referer: str) -> dict:
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

def _ffmpeg_hdr_block(referer: str) -> str:
    h = _cdn_headers(referer)
    return (
        f"Referer: {h['Referer']}\r\n"
        f"Origin: {h['Origin']}\r\n"
        f"User-Agent: {UA}\r\n"
        f"Sec-Fetch-Dest: video\r\n"
        f"Sec-Fetch-Mode: no-cors\r\n"
        f"Sec-Fetch-Site: cross-site\r\n"
    )

# ── yt-dlp format args builder (shared across all call sites) ─────────────────
def _yt_fmt_args(out_fmt: str) -> tuple[str, list]:
    """Return (format_selector, extra_args) for yt-dlp."""
    if out_fmt in AUDIO_FMTS:
        return ("bestaudio/best",
                ["--extract-audio", "--audio-format", out_fmt, "--audio-quality", "0"])
    if not out_fmt:
        return ("bestvideo+bestaudio/best[height<=1080]/best", [])
    sel = "bestvideo+bestaudio/best" if ffmpeg_ok() else "best"
    return (sel, ["--merge-output-format", out_fmt])


# ── Cloudflare bypass ─────────────────────────────────────────────────────────
_cf_log = logging.getLogger("CFBypass")

def _cf_bypass(driver, max_attempts: int = 10) -> bool:
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


# ── Chrome options ────────────────────────────────────────────────────────────
def _chrome_opts():
    from DrissionPage import ChromiumOptions
    opts = ChromiumOptions()
    opts.set_argument("--no-sandbox")
    opts.set_argument("--disable-blink-features=AutomationControlled")
    opts.set_argument(f"--user-agent={UA}")
    opts.headless(True)
    return opts


# ── Network listener helpers ──────────────────────────────────────────────────
def _start_listener(driver):
    try:
        driver.listen.start()
        return driver.listen
    except Exception as e:
        _cprint(f"[listen] Unavailable: {e}", 196)
        return None

def _extract_media_url(raw_url: str) -> str | None:
    if SKIP_DOMAINS_RE.search(raw_url):
        mu = re.search(r'[?&]mu=([^&]+)', raw_url)
        if mu:
            media = unquote(mu.group(1))
            if MEDIA_RE.search(media):
                return media
        return None
    return raw_url if MEDIA_RE.search(raw_url) else None

def _poll_listener(listener, captured: dict, timeout: int = 15) -> None:
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
                url = _extract_media_url(getattr(p, "url", "") or "")
                if url:
                    captured["url"] = url
                    _cprint(f"[listen] Captured: {url}", 45)
                    return
        except Exception:
            time.sleep(0.3)


# ── Browser fetch (layer 2) ───────────────────────────────────────────────────
def _drission_fetch(site: str) -> tuple:
    try:
        from DrissionPage import ChromiumPage
    except ImportError:
        _cprint("[browser] DrissionPage not installed — pip install DrissionPage", 196)
        return None, None

    print("[browser] Launching Chrome...")
    captured = {"url": None}
    driver = ChromiumPage(addr_or_opts=_chrome_opts())
    listener = _start_listener(driver)

    try:
        driver.get(site)
        time.sleep(3)
        _cf_bypass(driver)
        time.sleep(2)
        _poll_listener(listener, captured, timeout=15)

        html = driver.html
        if not captured["url"]:
            m = MEDIA_RE.search(html)
            if m:
                captured["url"] = m.group(0)
                print(f"[browser] Found in HTML: {captured['url']}")

        if not captured["url"]:
            for m in IFRAME_RE.finditer(html):
                src = m.group(1).strip()
                if not src.startswith("http") or IFRAME_SKIP_RE.search(src):
                    continue
                print(f"[browser] Checking iframe: {src}")
                driver.get(src)
                time.sleep(3)
                _poll_listener(listener, captured, timeout=15)
                if captured["url"]:
                    break
                fm = MEDIA_RE.search(driver.html)
                if fm:
                    captured["url"] = fm.group(0)
                    print(f"[browser] Found in iframe HTML: {captured['url']}")
                    break

        return html, captured["url"]

    except Exception as e:
        print(f"[browser] Error: {e}")
        return None, None
    finally:
        try: driver.quit()
        except Exception: pass


# ── Browser-intercept CDN download (token-bound) ──────────────────────────────
def _browser_intercept_and_download(player_url: str, site_referer: str,
                                    out_fmt: str = "mp4") -> bool:
    try:
        from DrissionPage import ChromiumPage
    except ImportError:
        print("[intercept] DrissionPage not installed.")
        return False

    print(f"[intercept] Opening in Chrome: {player_url}")
    captured = {"url": None}
    driver = ChromiumPage(addr_or_opts=_chrome_opts())
    listener = _start_listener(driver)

    try:
        driver.get(player_url)
        time.sleep(3)
        _cf_bypass(driver)

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
            _poll_listener(listener, captured, timeout=1)
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
                print(f"[intercept] Found in HTML: {captured['url']}")

        cdn_url = captured["url"]
        if not cdn_url:
            # X.com often won't expose the CDN URL through network interception;
            # yt-dlp has a native Twitter extractor — try it on the original page URL.
            if is_twitter and ytdlp_ok():
                print("[intercept] No CDN URL — handing off to yt-dlp (Twitter extractor)...")
                fmt_sel, extra = _yt_fmt_args(out_fmt)
                cmd = (
                    ["yt-dlp",
                     "--add-header", f"User-Agent:{UA}",
                     "--no-warnings", "--progress",
                     "-f", fmt_sel,
                     "-o", os.path.join(OUTPUT_DIR, "%(title)s.%(ext)s")]
                    + extra + [site_referer]          # site_referer is the original x.com URL
                )
                try:
                    result = subprocess.run(cmd, timeout=YTDLP_TIMEOUT)
                    return result.returncode == 0
                except subprocess.TimeoutExpired:
                    print("[intercept] yt-dlp timed out on Twitter URL.")
            print("[intercept] No CDN URL found.")
            return False

        print(f"[intercept] CDN URL: {cdn_url}")
        os.makedirs(OUTPUT_DIR, exist_ok=True)

        # Try yt-dlp first (no cookie loop — DPAPI is broken on Windows)
        if ytdlp_ok():
            fmt_sel, extra = _yt_fmt_args(out_fmt)
            cmd = (
                ["yt-dlp",
                 "--referer", player_url,
                 "--add-header", f"User-Agent:{UA}",
                 "--extractor-args", "generic:impersonate",
                 "--no-warnings", "--progress",
                 "-f", fmt_sel,
                 "-o", os.path.join(OUTPUT_DIR, "%(title)s.%(ext)s")]
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
            cmd_ff = ["ffmpeg", "-y", "-headers", _ffmpeg_hdr_block(player_url),
                      "-i", cdn_url, "-c", "copy", tmp]
            try:
                r = subprocess.run(cmd_ff, capture_output=True, timeout=FFMPEG_TIMEOUT)
            except subprocess.TimeoutExpired:
                if os.path.exists(tmp): os.remove(tmp)
                print(f"[intercept] ffmpeg timed out")
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


# ── Simple HTTP fetch (layer 1) ───────────────────────────────────────────────
def _simple_fetch(site: str) -> tuple:
    sess = _make_session(site)
    sess.headers.update(_base_headers(site))
    root = f"{urlparse(site).scheme}://{urlparse(site).netloc}"
    if root.rstrip("/") != site.rstrip("/"):
        try:
            sess.get(root, timeout=15, allow_redirects=True)
        except Exception:
            pass
    resp = sess.get(site, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    return resp.text, None


# ── Extraction helpers ────────────────────────────────────────────────────────
def find_direct_url(html: str) -> str | None:
    m = _DIRECT_RE.search(html)
    return m.group(1) if m else None

def b64_try(s: str) -> str | None:
    try:
        decoded = base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-8")
        if decoded.startswith(("http", "/")):
            return decoded
    except Exception:
        pass
    return None

def extract_player_url(html: str, base_url: str) -> str | None:
    for m in IFRAME_RE.finditer(html):
        src = m.group(1).strip()
        if not src or IFRAME_SKIP_RE.search(src):
            continue
        resolved = src if src.startswith("http") else urljoin(base_url, src)
        if resolved.startswith("http"):
            return resolved
    return None

def extract_media_from_player(html: str, player_base: str) -> dict:
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
    params = dict(p.split("=", 1) for p in data_id.split("?", 1)[-1].split("&") if "=" in p)
    for key, target in (("vid", "video"), ("s", "subtitle"), ("i", "thumb")):
        if key in params:
            decoded = b64_try(params[key])
            if decoded:
                result[target] = decoded
    return result


# ── Colored log helper ────────────────────────────────────────────────────────
_TAG_COLORS = {
    "yt": 226, "yt-dlp": 226, "twitter": 39,
    "1": 118, "2": 82, "3": 154,
    "browser": 51, "listen": 45, "intercept": 208,
    "DL": 213, "!": 196, "ffmpeg": 171,
}

def _cprint(msg: str, fallback: int = 255) -> None:
    if not _ansi_ready():
        print(msg)
        return
    m = re.match(r'^\[([^\]]+)\]', msg)
    color = _TAG_COLORS.get(m.group(1), fallback) if m else fallback
    sys.stdout.write(f"\033[38;5;{color}m{msg}\033[0m\n")
    sys.stdout.flush()


# ── Rainbow yt-dlp progress runner ────────────────────────────────────────────
_PCT_RE  = re.compile(r'\[download\]\s+([\d.]+)%')
_SIZE_RE = re.compile(
    r'\[download\]\s+[\d.]+%\s+of\s+~?\s*([\d.]+)(MiB|GiB|KiB|B)'
)
_MUL = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}

def _run_ytdlp_rainbow(cmd: list, timeout: int = YTDLP_TIMEOUT) -> bool:
    """Run yt-dlp and render its progress as a rainbow bar."""
    if not _ansi_ready():
        try:
            return subprocess.run(cmd, timeout=timeout).returncode == 0
        except subprocess.TimeoutExpired:
            return False
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
    except Exception as e:
        _cprint(f"[yt-dlp] launch error: {e}", 196)
        return False

    out = sys.stdout
    out.write("\033[?25l")
    printed_bar = False
    t0 = time.time()
    done_b = total_b = 0
    try:
        for raw in proc.stdout:
            line = raw.rstrip()
            pm = _PCT_RE.match(line)
            if pm:
                pct = float(pm.group(1)) / 100.0
                sm = _SIZE_RE.match(line)
                if sm:
                    total_b = int(float(sm.group(1)) * _MUL.get(sm.group(2), 1))
                    done_b  = int(pct * total_b)
                elif total_b:
                    done_b = int(pct * total_b)
                if total_b > 0:
                    bar = render_progress_bar(done_b, total_b, elapsed=time.time() - t0)
                else:
                    w = 40
                    filled = int(w * pct)
                    hs = (time.time() * 0.15) % 1.0
                    chars = [(_rgb((i/w+hs)%1.0, 0.85, 0.95) + "█") if i < filled
                             else "\033[38;5;238m░" for i in range(w)]
                    bar = "[" + "".join(chars) + f"\033[0m] {pct*100:5.1f}%"
                if printed_bar:
                    out.write("\033[1A")
                out.write(f"\033[2K{bar}\n")
                out.flush()
                printed_bar = True
            elif line:
                if printed_bar:
                    out.write("\n")
                    printed_bar = False
                _cprint(line, 245)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        return False
    finally:
        out.write("\033[0m\033[?25h")
        out.flush()
    return proc.returncode == 0


# ── yt-dlp helpers ────────────────────────────────────────────────────────────
def is_youtube(url: str) -> bool:
    return bool(YT_RE.search(url))

def is_twitter(url: str) -> bool:
    return bool(re.search(r'https?://(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/', url, re.I))

def ytdlp_youtube(url: str, out_fmt: str = "mp4") -> bool:
    """Download via yt-dlp using best available format, then convert with ffmpeg.
    Avoids all PO Token / format-selector negotiation failures."""
    if not ytdlp_ok():
        _cprint("[yt] yt-dlp not found — pip install yt-dlp", 196)
        return False
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Step 1: let yt-dlp grab whatever it can — no format filtering, no codec constraints.
    # audio-only request stays audio-only; everything else gets best+audio merged.
    if out_fmt in AUDIO_FMTS:
        dl_fmt_sel = "bestaudio/best"
        dl_extra = []
    else:
        dl_fmt_sel = "bestvideo+bestaudio/best" if ffmpeg_ok() else "best"
        dl_extra = ["--merge-output-format", "mkv"] if ffmpeg_ok() else []

    # Use a temp filename so we can locate the file after download regardless of ext
    tmp_template = os.path.join(OUTPUT_DIR, "%(title)s.ytdl.%(ext)s")
    # Just let yt-dlp do its thing with no extractor args — it handles
    # client selection and PO tokens internally when up to date.
    def _try(extra_args: list) -> bool:
        cmd = (
            ["yt-dlp", "--no-warnings", "--newline"]
            + extra_args
            + ["-f", dl_fmt_sel]
            + dl_extra
            + ["-o", tmp_template, url]
        )
        _cprint(f"[yt] {' '.join(cmd)}", 245)
        return _run_ytdlp_rainbow(cmd)

    # 1. Plain run — works for most videos
    if _try([]):
        pass
    # 2. 403 on CDN? Try with Edge cookies (most likely browser on Windows)
    elif _try(["--cookies-from-browser", "edge"]):
        pass
    # 3. Chrome
    elif _try(["--cookies-from-browser", "chrome"]):
        pass
    # 4. Firefox
    elif _try(["--cookies-from-browser", "firefox"]):
        pass
    else:
        _cprint("[yt] all attempts failed — log into YouTube in Edge/Chrome and retry.", 196)
        return False

    # Step 2: find the downloaded file (pattern: *.ytdl.*)
    import glob
    matches = sorted(glob.glob(os.path.join(OUTPUT_DIR, "*.ytdl.*")))
    if not matches:
        _cprint("[yt] download succeeded but no output file found", 196)
        return False
    src = matches[-1]  # most recent if somehow multiple

    # Audio or original — no conversion needed, just strip the .ytdl. marker
    if out_fmt in AUDIO_FMTS or not out_fmt:
        final = src.replace(".ytdl.", ".")
        os.rename(src, final)
        _cprint(f"[yt] saved: {final}", 46)
        return True

    # Step 3: ffmpeg convert to requested container
    base = src.replace(".ytdl.", ".")          # e.g. Title.mkv
    final = os.path.splitext(base)[0] + "." + out_fmt
    if ffmpeg_ok():
        _cprint(f"[yt] converting → {out_fmt} ...", 220)
        conv = ["ffmpeg", "-y", "-i", src, "-c", "copy", final]
        ret = subprocess.run(conv, capture_output=True)
        if ret.returncode != 0:
            # copy failed (container mismatch) — re-encode
            _cprint("[yt] stream copy failed, re-encoding...", 220)
            conv = ["ffmpeg", "-y", "-i", src,
                    "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart",
                    final]
            ret = subprocess.run(conv, capture_output=True)
        try:
            os.remove(src)
        except OSError:
            pass
        if ret.returncode == 0:
            _cprint(f"[yt] saved: {final}", 46)
            return True
        _cprint(f"[yt] ffmpeg conversion failed", 196)
        return False
    else:
        # No ffmpeg — just rename to desired ext and hope for the best
        os.rename(src, final)
        _cprint(f"[yt] saved (no ffmpeg — raw): {final}", 220)
        return True

def ytdlp_twitter(url: str, out_fmt: str = "mp4") -> bool:
    """Twitter/X native extraction. Tries without cookies first (works for
    most public tweets), then retries with Firefox cookies if that fails."""
    if not ytdlp_ok():
        _cprint("[twitter] yt-dlp not found — pip install yt-dlp", 196)
        return False
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    fmt_sel, extra = _yt_fmt_args(out_fmt)
    base = (["yt-dlp", "--no-warnings", "--newline",
              "-f", fmt_sel,
              "-o", os.path.join(OUTPUT_DIR, "%(title)s.%(ext)s")]
            + extra + [url])
    _cprint("[twitter] Trying yt-dlp (no cookies)...", 39)
    _cprint(f"[twitter] {' '.join(base)}", 245)
    if _run_ytdlp_rainbow(base):
        return True
    _cprint("[twitter] Retrying with --cookies-from-browser firefox...", 39)
    cookie_cmd = (["yt-dlp", "--cookies-from-browser", "firefox",
                   "--no-warnings", "--newline",
                   "-f", fmt_sel,
                   "-o", os.path.join(OUTPUT_DIR, "%(title)s.%(ext)s")]
                  + extra + [url])
    return _run_ytdlp_rainbow(cookie_cmd)

def ytdlp_download(url: str, referer: str, out_fmt: str = "mp4") -> bool:
    if not ytdlp_ok():
        _cprint("[yt-dlp] not found — pip install yt-dlp", 196)
        return False
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    fmt_sel, extra = _yt_fmt_args(out_fmt)
    cmd = (
        ["yt-dlp",
         "--referer", referer,
         "--add-header", f"User-Agent:{UA}",
         "--extractor-args", "generic:impersonate",
         "--no-warnings", "--newline",
         "-f", fmt_sel,
         "-o", os.path.join(OUTPUT_DIR, "%(title)s.%(ext)s")]
        + extra + [url]
    )
    _cprint(f"[yt-dlp] {' '.join(cmd)}", 245)
    return _run_ytdlp_rainbow(cmd)


# ── Direct download ───────────────────────────────────────────────────────────
def safe_filename(url: str, n: int = 1, ext: str = ".mp4") -> str:
    name = unquote(os.path.basename(urlparse(url).path)) or f"video_{n}"
    name = re.sub(r'\.(?:mp4|webm|mkv|m3u8|mpd|ts)$', '', name, flags=re.I) or f"video_{n}"
    return os.path.join(OUTPUT_DIR, f"{n:02d}_{name}{ext}")

def download_file(url: str, out_path: str, referer: str, n: int = 1, total: int = 1) -> str:
    tag = f"[{n}/{total}]"
    if os.path.exists(out_path):
        return f"{tag} SKIP (exists): {out_path}"

    if ffmpeg_ok():
        tmp = out_path + ".part.mp4"
        cmd = ["ffmpeg", "-y", "-headers", _ffmpeg_hdr_block(referer),
               "-i", url, "-c", "copy", tmp]
        # best-effort size estimate for the bar (HEAD may fail/lie for
        # m3u8/token-CDN sources — falls through to time-based bar instead)
        est_total = 0
        try:
            head = _raw_get(url, headers=_cdn_headers(referer), stream=True, timeout=10)
            est_total = int(head.headers.get("Content-Length", 0) or 0)
        except Exception:
            pass

        duration = 0.0 if est_total > 0 else _probe_duration(url, referer)
        use_bytes_bar = _ansi_ready() and est_total > 0
        use_time_bar = _ansi_ready() and not use_bytes_bar and duration > 0

        cmd_bar = cmd
        if use_time_bar:
            # -progress pipe:1 emits key=value lines (out_time_ms=...) we
            # parse each tick; -nostats silences ffmpeg's own status spam
            cmd_bar = ["ffmpeg", "-y", "-headers", _ffmpeg_hdr_block(referer),
                       "-i", url, "-c", "copy", "-progress", "pipe:1", "-nostats", tmp]

        try:
            stdout_pipe = subprocess.PIPE if use_time_bar else subprocess.DEVNULL
            proc = subprocess.Popen(cmd_bar, stdout=stdout_pipe, stderr=subprocess.PIPE, text=use_time_bar)
            t0 = time.time()
            printed_once = False
            out_time_sec = 0.0
            stderr_chunks = []

            def _tick_line():
                if use_bytes_bar and os.path.exists(tmp):
                    return render_progress_bar(os.path.getsize(tmp), est_total, elapsed=time.time() - t0)
                if use_time_bar:
                    return render_time_progress_bar(out_time_sec, duration, elapsed=time.time() - t0)
                return None

            while proc.poll() is None:
                if time.time() - t0 > FFMPEG_TIMEOUT:
                    proc.kill()
                    proc.wait()
                    if os.path.exists(tmp): os.remove(tmp)
                    print_mascot_fail()
                    return f"{tag} TIMEOUT (ffmpeg > {FFMPEG_TIMEOUT}s)"

                if use_time_bar:
                    line = proc.stdout.readline() if proc.stdout else ""
                    if line.startswith("out_time_ms="):
                        try:
                            out_time_sec = int(line.split("=", 1)[1]) / 1_000_000
                        except ValueError:
                            pass
                        continue  # loop again immediately, no sleep needed here

                bar_line = _tick_line()
                if bar_line is not None:
                    if printed_once:
                        sys.stdout.write("\033[1A")
                    sys.stdout.write("\033[2K" + bar_line + "\n")
                    sys.stdout.flush()
                    printed_once = True
                if not use_time_bar:
                    time.sleep(0.1 if use_bytes_bar else 0.5)

            stderr_chunks.append(proc.stderr.read() if proc.stderr else "")
            r_returncode = proc.returncode
            r_stderr = "".join(stderr_chunks) if use_time_bar else b"".join(
                c if isinstance(c, bytes) else c.encode() for c in stderr_chunks)
            if r_returncode == 0 and os.path.exists(tmp):
                size = os.path.getsize(tmp)
                if printed_once:
                    final = (render_progress_bar(size, max(size, est_total), elapsed=time.time() - t0)
                             if use_bytes_bar else
                             render_time_progress_bar(duration, duration, elapsed=time.time() - t0))
                    sys.stdout.write("\033[1A\033[2K" + final + "\n")
                    sys.stdout.flush()
                if size >= MIN_MB * 1024 * 1024:
                    os.replace(tmp, out_path)
                    print_mascot_success()
                    return f"{tag} SAVED via ffmpeg: {out_path} ({size/1024/1024:.1f} MB)"
                os.remove(tmp)
                print_mascot_fail()
                return f"{tag} TOO SMALL ({size/1024/1024:.2f} MB)"
            err_text = r_stderr.decode(errors="replace") if isinstance(r_stderr, bytes) else r_stderr
            err = err_text.strip().splitlines()[-3:]
            print(f"{tag} ffmpeg failed, falling back: {' | '.join(err)}")
        except Exception as e:
            if os.path.exists(tmp): os.remove(tmp)
            print(f"{tag} ffmpeg errored, falling back: {e}")

    headers = _cdn_headers(referer)
    tmp = out_path + ".part"
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = _raw_get(url, headers=headers, stream=True, timeout=STREAM_TIMEOUT)
            resp.raise_for_status()
            total_hdr = int(resp.headers.get("Content-Length", 0) or 0)
            size = 0
            t0 = time.time()
            bar_live = _ansi_ready() and total_hdr > 0
            printed_once = False
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(1024 * 1024):
                    f.write(chunk); size += len(chunk)
                    if bar_live:
                        line = render_progress_bar(size, total_hdr, elapsed=time.time() - t0)
                        if printed_once:
                            sys.stdout.write("\033[1A")
                        sys.stdout.write("\033[2K" + line + "\n")
                        sys.stdout.flush()
                        printed_once = True
            os.replace(tmp, out_path)
            if size < MIN_MB * 1024 * 1024:
                os.remove(out_path)
                print_mascot_fail()
                return f"{tag} TOO SMALL ({size/1024/1024:.2f} MB)"
            print_mascot_success()
            return f"{tag} SAVED: {out_path} ({size/1024/1024:.1f} MB)"
        except Exception as e:
            last_err = e
            if os.path.exists(tmp): os.remove(tmp)
            if attempt < MAX_RETRIES:
                wait = 2 ** attempt
                print(f"{tag} retry {attempt} ({e}), waiting {wait}s...")
                time.sleep(wait)
    print_mascot_fail()
    return f"{tag} FAILED: {last_err}"


# ── Main ──────────────────────────────────────────────────────────────────────
def _pick_format() -> str:
    options_raw = ["mp4", "mp3", "mkv", "webm", "original"]
    print_mascot_thinking()
    labeled = [f"{i}. {opt}" for i, opt in enumerate(options_raw, 1)]
    valid = {str(i) for i in range(1, len(options_raw) + 1)}
    raw = input_with_breathing_menu("Output format:", labeled, valid, default="1")
    idx = int(raw) - 1
    return "" if options_raw[idx] == "original" else options_raw[idx]


def scrape(site: str, out_fmt: str = "mp4") -> None:
    # YouTube shortcut
    if is_youtube(site):
        _cprint("[yt] YouTube detected — routing to yt-dlp", 226)
        sys.exit(0 if ytdlp_youtube(site, out_fmt) else 1)

    # Twitter/X shortcut — HTML scraping can't get the CDN URL without login;
    # yt-dlp's native Twitter extractor handles the GraphQL auth internally
    if is_twitter(site):
        _cprint("[twitter] X.com detected — routing to yt-dlp", 39)
        sys.exit(0 if ytdlp_twitter(site, out_fmt) else 1)

    # Layer 1: direct fetch
    html, video_url = None, None
    _cprint(f"[1] Direct fetch: {site}", 118)
    try:
        html, video_url = _simple_fetch(site)
        _cprint("[1] OK", 118)
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "?")
        _cprint(f"[1] Failed (HTTP {status}) — browser mode", 196)

    # Layer 2: real Chrome + CF bypass
    if html is None:
        _cprint("[2] Browser mode...", 82)
        html, video_url = _drission_fetch(site)
        if html is None:
            raise SystemExit("[!] All fetch modes failed.")

    # Layer 3: HTML scan
    if not video_url:
        _cprint("[3] Scanning HTML...", 154)
        video_url = find_direct_url(html)

    cdn_referer = site
    player_iframe_url = None

    if not video_url:
        _cprint("[3] Scanning for player iframe...", 154)
        player_iframe_url = extract_player_url(html, site)
        if player_iframe_url:
            _cprint(f"[3] Player: {player_iframe_url}", 154)
            cdn_referer = player_iframe_url
            player_base = (f"{urlparse(player_iframe_url).scheme}://"
                           f"{urlparse(player_iframe_url).netloc}")
            try:
                sess = _make_session(site)
                sess.headers.update(_base_headers(site))
                presp = sess.get(player_iframe_url, timeout=20, allow_redirects=True)
                presp.raise_for_status()
                media = extract_media_from_player(presp.text, player_base)
                video_url = media.get("video")
                if not video_url and media.get("player_url"):
                    presp2 = sess.get(media["player_url"], timeout=20, allow_redirects=True)
                    video_url = extract_media_from_player(presp2.text, player_base).get("video")
            except Exception as e:
                _cprint(f"[3] Player fetch error: {e}", 196)

    # Token-bound URL detected
    if video_url and TOKEN_BOUND_RE.search(video_url):
        _cprint("[!] Token-bound — browser intercept", 208)
        sys.exit(0 if _browser_intercept_and_download(
            player_iframe_url or site, site, out_fmt) else 1)

    # No URL found — intercept then yt-dlp
    if not video_url:
        _cprint("[!] No media URL — trying intercept...", 208)
        if _browser_intercept_and_download(player_iframe_url or site, site, out_fmt):
            sys.exit(0)
        _cprint("[!] Trying yt-dlp...", 208)
        sys.exit(0 if ytdlp_download(site, site, out_fmt) else 1)

    # CDN domain mismatch — try intercept first
    if urlparse(video_url).netloc != urlparse(cdn_referer).netloc:
        _cprint("[DL] CDN domain mismatch — trying intercept first...", 213)
        if _browser_intercept_and_download(player_iframe_url or cdn_referer, site, out_fmt):
            _cprint(f"\nDONE — {os.path.abspath(OUTPUT_DIR)}", 118)
            sys.exit(0)
        if ytdlp_ok():
            _cprint("[DL] Intercept failed — trying yt-dlp on original URL...", 213)
            if ytdlp_download(site, site, out_fmt):
                _cprint(f"\nDONE — {os.path.abspath(OUTPUT_DIR)}", 118)
                sys.exit(0)
        _cprint("[DL] Falling back to direct download...", 213)

    # Direct download
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    target_ext = f".{out_fmt}" if out_fmt else ".mp4"
    out_video = safe_filename(video_url, 1, ext=target_ext)
    _cprint(f"\n[DL] {video_url}", 213)
    _cprint(f"[DL] Referer  -> {cdn_referer}", 245)
    _cprint(f"[DL] Output   -> {out_video}", 245)
    print(download_file(video_url, out_video, cdn_referer))
    _cprint(f"\nDONE — {os.path.abspath(OUTPUT_DIR)}", 118)


if __name__ == "__main__":
    quick_update_check()
    if len(sys.argv) > 1:
        print_logo()
        site = sys.argv[1]
    else:
        site = wait_for_site_input_with_idle_logo()
    if not site:
        raise SystemExit("No URL.")
    exit_code = 0
    try:
        scrape(site, _pick_format())
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        press_any_key_to_close()
    sys.exit(exit_code)
