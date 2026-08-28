"""All terminal UI: the animated banner, mascot frames, key polling, the
breathing-border menu, progress bars, and colored log output.

Nothing in here knows about scraping, extraction, or downloading — it only
renders things and reads keystrokes. Keeping it separate means the rest of
the pipeline can be tested without a real terminal.
"""

from __future__ import annotations

import colorsys
import math
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

from .config import ffmpeg_hdr_block

# ── Debug mode (controls how much of a URL gets printed) ──────────────────────
_DEBUG = False

def set_debug(flag: bool) -> None:
    global _DEBUG
    _DEBUG = flag

def is_debug() -> bool:
    return _DEBUG

def redact_url(url: str | None) -> str:
    """Full URL under --debug; otherwise just the scheme+host, since
    tokenized CDN URLs can carry authentication material in the query
    string and shouldn't land in a terminal or log file by default."""
    if not url:
        return "?"
    if _DEBUG:
        return url
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.netloc else url


def _ansi_ready() -> bool:
    """True if the terminal can render ANSI escapes; enables them on Windows."""
    if os.name == "nt":
        os.system("")  # no-op that flips on VT100 processing in modern conhost
    return sys.stdout.isatty()


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

def probe_duration(url: str, referer: str) -> float:
    """Best-effort ffprobe duration lookup, in seconds. Returns 0.0 if it
    can't be determined (some m3u8 sources refuse to report it too, at
    which point we just fall back to indeterminate/no-bar)."""
    if not shutil.which("ffprobe"):
        return 0.0
    try:
        cmd = ["ffprobe", "-v", "error", "-headers", ffmpeg_hdr_block(referer),
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


# ── Colored log helper ────────────────────────────────────────────────────────
_TAG_COLORS = {
    "yt": 226, "yt-dlp": 226, "twitter": 39,
    "1": 118, "2": 82, "3": 154,
    "browser": 51, "listen": 45, "intercept": 208,
    "DL": 213, "!": 196, "ffmpeg": 171,
}

def cprint(msg: str, fallback: int = 255) -> None:
    if not _ansi_ready():
        print(msg)
        return
    m = re.match(r'^\[([^\]]+)\]', msg)
    color = _TAG_COLORS.get(m.group(1), fallback) if m else fallback
    sys.stdout.write(f"\033[38;5;{color}m{msg}\033[0m\n")
    sys.stdout.flush()

def cprint_url(tag: str, label: str, url: str | None, fallback: int = 255) -> None:
    """Like cprint, but for messages carrying a URL: the full URL (which may
    contain a signed/tokenized CDN auth string) is only shown under --debug.
    Normal mode shows just the host."""
    cprint(f"[{tag}] {label}: {redact_url(url)}", fallback)
