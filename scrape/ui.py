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
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlparse

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.style import Style
from rich.text import Text

from .config import ffmpeg_hdr_block

# ── Shared console — one instance so cprint() and an active Live co-operate ──
# Rich's documented approach: route all output through the same Console and
# Live.console so lines printed "above" a Live display don't corrupt redraws.
_console = Console(highlight=False)

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

def _logo_frame(lines: list, frame: int, n: int) -> Text:
    """Build one frame of the rainbow-sweep banner as a real Rich Text
    object (per-character color spans), not an ANSI string."""
    text = Text()
    for row, line in enumerate(lines):
        for col, ch in enumerate(line):
            if ch == " ":
                text.append(" ")
            else:
                idx = (col // 2 + row - frame) % n
                text.append(ch, style=f"color({_WAVE_COLORS[idx]})")
        text.append("\n")
    return text


def print_logo(frames: int = 16, delay: float = 0.035) -> None:
    """Print the banner. Animates a color sweep across it in a real terminal,
    falls back to a plain static print anywhere ANSI isn't supported (piped
    output, dumb terminals, etc.). Rendering/redraw is entirely owned by a
    single rich.live.Live loop — no manual cursor-home/hide escapes."""
    if not _ansi_ready():
        print(LOGO)
        return
    lines = LOGO.strip("\n").splitlines()
    n = len(_WAVE_COLORS)
    fps = max(4, min(60, round(1 / delay))) if delay > 0 else 30
    with Live(console=_console, refresh_per_second=fps, transient=False) as live:
        for frame in range(frames):
            live.update(_logo_frame(lines, frame, n))
            time.sleep(delay)


# ── ASCII mascot (in-place looping frame animation) ────────────────────────────
def _animate_frames(frames: list, loops: int = 1, delay: float = 0.12,
                     style: str | None = None) -> None:
    """Step through a sequence of multi-line ASCII frames via a single Rich
    Live instance, looping `loops` times, then leave the final frame on
    screen. Falls back to a single static print of the last frame when
    ANSI/Live rendering isn't supported (piped output, dumb terminals)."""
    if not _ansi_ready():
        print(frames[-1])
        return
    with Live(console=_console, refresh_per_second=max(4, round(1 / delay)),
              transient=False) as live:
        for _ in range(loops):
            for frame in frames:
                live.update(Text(frame, style=style) if style else Text(frame))
                time.sleep(delay)

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
    _animate_frames(_CRY_FRAMES, loops=2, delay=0.15, style="bold red")

def print_mascot_success() -> None:
    _animate_frames(_HAPPY_FRAMES, loops=2, delay=0.11, style="bold green")

def print_mascot_thinking() -> None:
    _animate_frames(_THINKING_FRAMES, loops=2, delay=0.25, style="bold cyan")


# ── HSV -> truecolor helpers ───────────────────────────────────────────────
# _rgb: raw ANSI truecolor escape. Kept only for the "pure" bar renderers
# below (render_progress_bar / render_pct_bar / render_indeterminate_bar).
# Those functions don't touch the screen or own any timing loop — they just
# return a colored string that a caller's Rich Live feeds through
# Text.from_ansi(), which is Rich's own documented mechanism for consuming
# ANSI-colored text as a renderable. That's Rich parsing color, not us
# driving a redraw loop, so it isn't part of the "raw ANSI as animation
# engine" problem this rebuild removes.
def _rgb(h: float, s: float, v: float) -> str:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, max(0.0, min(1.0, v)))
    return f"\033[38;2;{int(r*255)};{int(g*255)};{int(b*255)}m"

# _hue_color: HSV -> "#rrggbb" for building genuine Rich Style/Text objects
# directly (no ANSI at all). Used by every renderer below that now owns its
# own Rich Live loop.
def _hue_color(h: float, s: float, v: float) -> str:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, max(0.0, min(1.0, v)))
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"


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


def input_with_breathing_menu(title: str, options: list, valid: set, default: str = "1") -> str:
    """Bordered menu box with a continuously breathing rainbow border while
    it waits on input. Invalid choices re-prompt in place with an inline
    error instead of silently accepting garbage or crashing.

    One rich.live.Live instance owns the box and its animation timing.
    Each tick either reacts to a waiting keystroke or redraws the next
    breathing frame — never both racing each other. Rich's Panel draws the
    actual border (no manually built box-drawing characters or cursor-up
    escapes)."""
    if not _ansi_ready():
        while True:
            print(title)
            for o in options:
                print(o)
            buf = input(f"Choice [{default}]: ")
            choice = buf.strip() or default
            if choice in valid:
                return choice
            print(f"[!] use 1–{max(valid, key=int)}, got '{choice}'")

    body_width = max(len(title), max(len(o) for o in options), 24) + 4
    t0 = time.time()
    buf, error = "", ""

    def render() -> Panel:
        elapsed = time.time() - t0
        hue = (elapsed * 0.12) % 1.0
        breath = 0.55 + 0.45 * math.sin(elapsed * 2.2)
        border_color = _hue_color(hue, 0.85, breath)

        body = Text()
        body.append(title + "\n\n", style="bold")
        for o in options:
            body.append(o + "\n")
        body.append(f"\nChoice [{default}]: {buf}", style="bold cyan")
        if error:
            body.append(f"\n{error}", style="bold red")
        return Panel(body, border_style=Style(color=border_color),
                     width=body_width, padding=(0, 1))

    with Live(render(), console=_console, refresh_per_second=20,
              transient=False) as live:
        with _RawStdin():
            while True:
                key = _poll_key()
                if key is None:
                    live.update(render())
                    time.sleep(0.05)
                    continue
                if key in _ENTER:
                    choice = buf.strip() or default
                    if choice in valid:
                        return choice
                    error = f"use 1–{max(valid, key=int)}, got '{choice}'"
                    buf = ""
                elif key in _BACKSPACE:
                    buf = buf[:-1]
                    error = ""
                elif key.isprintable():
                    buf += key
                    error = ""
                live.update(render())


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

# ── Idle "breathing" mascot loop — plays continuously during any live
#    operation (download, spotdl, yt-dlp), not just as a before/after clip ──
_BREATH_FRAMES = [
    "  ___   \n (◕‿◕)  \n ▔▔▔▔▔  ",
    "  ___   \n (◕‿◕)  \n ▔▔▔▔▔  ",
    "  ____  \n (◕‿◕)  \n ▔▔▔▔▔▔ ",
    "  ___   \n (◕‿◕)  \n ▔▔▔▔▔  ",
]

def render_mascot_frame(elapsed: float, delay: float = 0.35) -> list[str]:
    """Pure function of elapsed time, same idea as the progress-bar
    renderers: given how long we've been running, return the mascot's
    current breathing-loop frame as a list of lines. It cycles forever,
    so any caller that redraws it every render tick gets a mascot that's
    always animating for the full duration of the operation — never a
    one-off clip that plays once and then just sits static."""
    idx = int(elapsed / delay) % len(_BREATH_FRAMES)
    return _BREATH_FRAMES[idx].splitlines()


# ── Rich-backed live block context manager ────────────────────────────────────
# Replaces the old redraw_block / clear_block / run_live / QueueSource pattern.
#
# Usage in ytdlp.py / downloader.py:
#
#   with live_block() as live:
#       while not done:
#           lines = render_mascot_frame(elapsed) + [render_pct_bar(pct, elapsed=elapsed)]
#           live.update(Text.from_ansi("\n".join(lines)))
#           time.sleep(0.1)
#
# transient=True  → block vanishes when the `with` exits (downloads in progress)
# transient=False → last frame stays (logo, celebrations, prompts)

@contextmanager
def live_block(transient: bool = True):
    """Context manager wrapping rich.live.Live with our shared console.
    Yields the Live object so callers can call live.update(renderable).
    transient=True  erases the block on exit (in-progress download bars).
    transient=False leaves the last frame on screen (mascot celebrations,
    the logo sweep, interactive prompts)."""
    with Live(
        console=_console,
        refresh_per_second=20,
        transient=transient,
    ) as live:
        yield live


# ── Compat shims — keep ytdlp.py / downloader.py compiling unchanged ─────────
# These are thin wrappers around the new Rich machinery so callers that still
# use the old redraw_block/clear_block/run_live/QueueSource signatures keep
# working without modification.  Callers can be migrated to live_block() at
# leisure; nothing breaks in the meantime.

def redraw_block(out, prev_n_lines: int, lines: list) -> int:
    """Compat: write lines directly to stdout the old way.
    Still works; callers inside a live_block() should use live.update()
    instead, but existing call-sites outside a Live context are fine here."""
    if prev_n_lines:
        out.write(f"\033[{prev_n_lines}A")
    for line in lines:
        out.write("\033[2K" + line + "\n")
    out.flush()
    return len(lines)


def clear_block(out, prev_n_lines: int) -> int:
    """Compat: erase a previously-drawn live block and leave nothing behind.
    Always returns 0 so callers can assign straight to their line-count tracker."""
    if prev_n_lines:
        out.write(f"\033[{prev_n_lines}A")
        for _ in range(prev_n_lines):
            out.write("\033[2K\n")
        out.write(f"\033[{prev_n_lines}A")
    out.flush()
    return 0


class QueueSource:
    """Compat: same .queue / .is_done() shape as PipeReader, for a
    caller-owned background thread instead of a subprocess pipe.
    The caller pushes items onto .queue and calls mark_done() in a
    finally when that worker finishes; run_live drives it exactly
    like a PipeReader."""

    def __init__(self):
        self.queue: "queue.Queue" = queue.Queue()
        self._done = threading.Event()

    def mark_done(self) -> None:
        self._done.set()

    def is_done(self) -> bool:
        return self._done.is_set() and self.queue.empty()


class PipeReader:
    """Reads a subprocess text stream on a background thread and pushes
    each chunk onto a queue, splitting on \\r as well as \\n (tqdm/rich/
    ffmpeg -progress style tools redraw in place with \\r and may not send
    a real newline until the whole step is done). This thread never
    touches the screen — it only reads and queues — so the main thread
    stays the sole writer and there's no race on terminal output."""

    def __init__(self, stream):
        self.queue: "queue.Queue[str]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, args=(stream,), daemon=True)
        self._thread.start()

    def _run(self, stream) -> None:
        buf = []
        try:
            while True:
                ch = stream.read(1)
                if ch == "":
                    break
                if ch in ("\r", "\n"):
                    if buf:
                        self.queue.put("".join(buf))
                        buf = []
                else:
                    buf.append(ch)
            if buf:
                self.queue.put("".join(buf))
        except Exception:
            pass

    def is_done(self) -> bool:
        """True once the reader thread has died AND its queue is fully
        drained — checked in that order, which is what avoids reporting
        done one tick early on a line that arrived right as the pipe
        closed, or spinning forever on an already-closed pipe."""
        return not self._thread.is_alive() and self.queue.empty()


def run_live(source, on_line, on_tick, tick_interval: float = 0.1) -> None:
    """Drive a live stream of progress items on our own clock, decoupled
    from how often the producer actually emits anything. `source` is
    either a subprocess.Popen (its stdout is wrapped in a PipeReader
    automatically) or an already-live QueueSource/PipeReader-shaped
    object (`.queue` + `.is_done()`) fed by a caller's own background
    thread — e.g. a library progress callback instead of a text pipe.
    Either way, this loop — on the main thread, the only one allowed to
    touch the screen — drains whatever's arrived every `tick_interval`
    seconds and calls `on_line(item)` for it, or calls `on_tick()` when
    nothing arrived in time so callers can still redraw (mascot/bar) on
    schedule through a silent stretch instead of the animation just
    stopping until the next item shows up.

    `on_tick` may return a truthy value to stop early (e.g. a caller-side
    timeout) — run_live itself doesn't kill the process/thread, that's
    left to the caller, since what "stop" means (kill vs. detach and move
    on) differs per call site.

    Returns once the source reports done (thread/process finished AND its
    queue is fully drained)."""
    reader = PipeReader(source.stdout) if hasattr(source, "stdout") else source
    while True:
        try:
            item = reader.queue.get(timeout=tick_interval)
            on_line(item)
        except queue.Empty:
            if on_tick():
                return
            if reader.is_done():
                return


def render_pct_bar(pct: float, width: int = 40, elapsed: float = 0.0) -> str:
    """Same rainbow-fill look as render_progress_bar, but for callers that
    only have a percentage (0.0-1.0) and no byte counts — e.g. spotdl's
    own tqdm/rich progress output."""
    pct = max(0.0, min(1.0, pct))
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
    return f"[{bar}] {pct*100:5.1f}%"


def render_indeterminate_bar(width: int = 40, elapsed: float = 0.0, label: str = "working...") -> str:
    """Rainbow bar for when we truly don't know a total (no Content-Length,
    no probeable duration, no yt-dlp percent line yet). A block of color
    bounces back and forth so there's always *something* animating instead
    of the bar just not being drawn — used as the fallback everywhere a
    real percentage isn't available yet, so every download shows a bar
    of some kind from the moment it starts."""
    hue_shift = (elapsed * 0.15) % 1.0
    period = width * 2 - 2 if width > 1 else 1
    pos = elapsed * 18.0 % period
    pos = pos if pos <= width - 1 else period - pos  # bounce
    span = 6  # width of the moving highlight block
    lo, hi = pos - span / 2, pos + span / 2
    bar_chars = []
    for i in range(width):
        if lo <= i <= hi:
            hue = (i / width + hue_shift) % 1.0
            bar_chars.append(_rgb(hue, 0.85, 0.95) + "█")
        else:
            bar_chars.append("\033[38;5;238m░")
    bar = "".join(bar_chars) + "\033[0m"
    return f"[{bar}] {label}"


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
    # After subprocesses (especially spotdl/yt-dlp) stdin can be left in a
    # dirty state on Windows. Re-open it so msvcrt.kbhit() actually fires.
    if os.name == "nt":
        try:
            import msvcrt
            # Drain any buffered bytes left in stdin so the first real keypress
            # is the one we respond to, not leftover bytes from a subprocess.
            while msvcrt.kbhit():
                msvcrt.getwch()
        except Exception:
            pass

    if not _ansi_ready():
        input(message + " ")
        return

    t0 = time.time()

    def render() -> Text:
        elapsed = time.time() - t0
        breath = 0.5 + 0.5 * math.sin(elapsed * 3.0)
        hue = (elapsed * 0.2) % 1.0
        return Text(message, style=Style(color=_hue_color(hue, 0.8, 0.5 + 0.5 * breath)))

    with Live(render(), console=_console, refresh_per_second=20, transient=False) as live:
        with _RawStdin() as raw:
            # Drain anything already buffered in stdin (leftover Enter from
            # a previous prompt, a stray keystroke typed while a download
            # was still running, etc.) so the very first _poll_key() call
            # below can't immediately "see" old input and instantly close
            # the screen before the person ever gets to read it.
            if raw.enabled:
                import termios
                termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
            while True:
                key = _poll_key()
                if key is not None:
                    return
                live.update(render())
                time.sleep(frame_delay)


# ── Idle logo replay (single-loop — waits on a key, replays sweep every ~5s) ──
def wait_for_site_input_with_idle_logo(replay_every: float = 5.0) -> str:
    """Sits at 'Site URL: ' prompt. If the user hasn't typed anything for
    `replay_every` seconds, the logo does one rainbow sweep in place above
    the prompt line, then returns to waiting — all in the same single Live
    loop/single writer that reads keystrokes, so no race with typing and no
    manual cursor-up bookkeeping."""
    if not _ansi_ready():
        return input("Site URL: ").strip()

    lines = LOGO.strip("\n").splitlines()
    n_wave = len(_WAVE_COLORS)
    buf = ""
    t0 = time.time()
    last_key_t = t0
    sweep_frame = 0

    def render(sweeping: bool) -> Text:
        text = _logo_frame(lines, sweep_frame if sweeping else 0, n_wave)
        text.append(f"Site URL: {buf}", style="bold cyan")
        return text

    with Live(console=_console, refresh_per_second=20, transient=False) as live:
        with _RawStdin():
            while True:
                now = time.time()
                sweeping = (now - last_key_t) >= replay_every
                live.update(render(sweeping))

                if sweeping:
                    sweep_frame += 1
                    if sweep_frame >= 16:  # one full sweep, then rest until idle timer resets
                        sweep_frame = 0
                        last_key_t = now  # restart the idle countdown after a replay

                key = _poll_key()
                if key is None:
                    time.sleep(0.035 if sweeping else 0.05)
                    continue
                last_key_t = time.time()
                sweep_frame = 0
                if key in _ENTER:
                    return buf.strip()
                elif key in _BACKSPACE:
                    buf = buf[:-1]
                elif key.isprintable():
                    buf += key


# ── Colored log helper ────────────────────────────────────────────────────────
_TAG_COLORS = {
    "yt": 226, "yt-dlp": 226, "twitter": 39,
    "1": 118, "2": 82, "3": 154,
    "browser": 51, "listen": 45, "intercept": 208,
    "DL": 213, "!": 196, "ffmpeg": 171,
}

def cprint(msg: str, fallback: int = 255) -> None:
    """Colored log line. Routes through _console so output interleaves
    correctly above any active Rich Live display."""
    if not _ansi_ready():
        print(msg)
        return
    m = re.match(r'^\[([^\]]+)\]', msg)
    color = _TAG_COLORS.get(m.group(1), fallback) if m else fallback
    # Build the ANSI string the same way as before, but hand it to Rich's
    # console so it doesn't corrupt an active Live display.
    ansi_line = f"\033[38;5;{color}m{msg}\033[0m"
    _console.print(Text.from_ansi(ansi_line))

def cprint_url(tag: str, label: str, url: str | None, fallback: int = 255) -> None:
    """Like cprint, but for messages carrying a URL: the full URL (which may
    contain a signed/tokenized CDN auth string) is only shown under --debug.
    Normal mode shows just the host."""
    cprint(f"[{tag}] {label}: {redact_url(url)}", fallback)
