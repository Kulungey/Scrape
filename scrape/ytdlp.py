"""yt-dlp integration: platform detection (YouTube/Twitter), the format-args
builder, cached ffmpeg/yt-dlp availability checks, the update check, and the
rainbow progress-bar subprocess runner.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import time

from . import config
from .config import UA, YTDLP_TIMEOUT, AUDIO_FMTS
from .patterns import YT_RE
from .ui import cprint, render_progress_bar, _rgb

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


# ── Fast "does yt-dlp know this site?" probe ──────────────────────────────────
def ytdlp_probe(url: str, referer: str | None = None, timeout: int = 5) -> bool:
    """--simulate --print url: asks yt-dlp to extract without downloading.
    Returns True (and cheaply) if it recognizes the site — used as the very
    first check in the pipeline so any of yt-dlp's ~1800 native extractors
    (Reddit, Bilibili, Vimeo, TikTok, playlists, etc.) short-circuit our
    HTML/browser layers entirely instead of us reinventing per-site logic.
    Killed on timeout rather than left to hang — some unknown sites make
    yt-dlp do a slow doomed probe of its own before giving up."""
    if not ytdlp_ok():
        return False
    cmd = ["yt-dlp", "--simulate", "--print", "url", "--no-warnings", "--quiet"]
    if referer:
        cmd += ["--referer", referer]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.returncode == 0 and bool(r.stdout.strip())
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


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


# ── yt-dlp format args builder (shared across all call sites) ─────────────────
def yt_fmt_args(out_fmt: str) -> tuple[str, list]:
    """Return (format_selector, extra_args) for yt-dlp."""
    if out_fmt == "original":
        out_fmt = ""
    if out_fmt in AUDIO_FMTS:
        return ("bestaudio/best",
                ["--extract-audio", "--audio-format", out_fmt, "--audio-quality", "0"])
    if not out_fmt:
        return ("bestvideo+bestaudio/best[height<=1080]/best", [])
    sel = "bestvideo+bestaudio/best" if ffmpeg_ok() else "best"
    return (sel, ["--merge-output-format", out_fmt])


# ── Platform detection ────────────────────────────────────────────────────────
def is_youtube(url: str) -> bool:
    return bool(YT_RE.search(url))

def is_twitter(url: str) -> bool:
    return bool(re.search(r'https?://(?:www\.|mobile\.)?(?:x\.com|twitter\.com)/', url, re.I))

def is_vimeo(url: str) -> bool:
    return bool(re.search(r'https?://(?:www\.)?vimeo\.com/', url, re.I))

def vimeo_to_player_url(url: str) -> str:
    """Rewrite vimeo.com/<id> (and /channels/, /groups/ variants) to the
    player.vimeo.com embed URL. Vimeo revoked the OAuth token its macos/web
    clients used for anonymous extraction (yt-dlp issue #17271); the embed
    endpoint answers anonymous requests and was never affected. Already-embed
    URLs pass through unchanged."""
    if "player.vimeo.com" in url:
        return url
    m = re.search(r'vimeo\.com/(?:(?:channels|groups)/[^/]+/(?:videos/)?|video/)?(\d+)', url)
    return f"https://player.vimeo.com/video/{m.group(1)}" if m else url

def is_dailymotion(url: str) -> bool:
    return bool(re.search(
        r'https?://(?:www\.)?dailymotion\.com/video/|https?://dai\.ly/', url, re.I
    ))

def is_reddit(url: str) -> bool:
    return bool(re.search(
        r'https?://(?:(?:\w+\.)?reddit(?:media)?\.com|v\.redd\.it)/', url, re.I
    ))

def is_tiktok(url: str) -> bool:
    return bool(re.search(r'https?://(?:(?:www|vm|vt)\.)?tiktok\.com/', url, re.I))

def is_twitch(url: str) -> bool:
    return bool(re.search(r'https?://(?:(?:www|clips)\.)?twitch\.tv/', url, re.I))


# ── Rainbow yt-dlp progress runner ────────────────────────────────────────────
_PCT_RE  = re.compile(r'\[download\]\s+([\d.]+)%')
_SIZE_RE = re.compile(
    r'\[download\]\s+[\d.]+%\s+of\s+~?\s*([\d.]+)(MiB|GiB|KiB|B)'
)
_MUL = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}

def run_ytdlp_rainbow(cmd: list, timeout: int = YTDLP_TIMEOUT) -> bool:
    """Run yt-dlp and render its progress as a rainbow bar."""
    from .ui import _ansi_ready
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
        cprint(f"[yt-dlp] launch error: {e}", 196)
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
                cprint(line, 245)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        return False
    finally:
        out.write("\033[0m\033[?25h")
        out.flush()
    return proc.returncode == 0


# ── yt-dlp download helpers ────────────────────────────────────────────────────
def ytdlp_youtube(url: str, out_fmt: str = "mp4") -> bool:
    """Download via yt-dlp using best available format, then convert with ffmpeg.
    Avoids all PO Token / format-selector negotiation failures."""
    if not ytdlp_ok():
        cprint("[yt] yt-dlp not found — pip install yt-dlp", 196)
        return False
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    # Step 1: let yt-dlp grab whatever it can — no format filtering, no codec constraints.
    # audio-only request stays audio-only; everything else gets best+audio merged.
    if out_fmt in AUDIO_FMTS:
        dl_fmt_sel = "bestaudio/best"
        dl_extra = []
    else:
        dl_fmt_sel = "bestvideo+bestaudio/best" if ffmpeg_ok() else "best"
        dl_extra = ["--merge-output-format", "mkv"] if ffmpeg_ok() else []

    # Use a temp filename so we can locate the file after download regardless of ext
    tmp_template = os.path.join(config.OUTPUT_DIR, "%(title)s.ytdl.%(ext)s")
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
        cprint(f"[yt] {' '.join(cmd)}", 245)
        return run_ytdlp_rainbow(cmd)

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
        cprint("[yt] all attempts failed — log into YouTube in Edge/Chrome and retry.", 196)
        return False

    # Step 2: find the downloaded file (pattern: *.ytdl.*)
    matches = sorted(glob.glob(os.path.join(config.OUTPUT_DIR, "*.ytdl.*")))
    if not matches:
        cprint("[yt] download succeeded but no output file found", 196)
        return False
    src = matches[-1]  # most recent if somehow multiple

    # Audio or original — no conversion needed, just strip the .ytdl. marker
    if out_fmt in AUDIO_FMTS or not out_fmt:
        final = src.replace(".ytdl.", ".")
        os.rename(src, final)
        cprint(f"[yt] saved: {final}", 46)
        return True

    # Step 3: ffmpeg convert to requested container
    base = src.replace(".ytdl.", ".")          # e.g. Title.mkv
    final = os.path.splitext(base)[0] + "." + out_fmt
    if ffmpeg_ok():
        cprint(f"[yt] converting → {out_fmt} ...", 220)
        conv = ["ffmpeg", "-y", "-i", src, "-c", "copy", final]
        ret = subprocess.run(conv, capture_output=True)
        if ret.returncode != 0:
            # copy failed (container mismatch) — re-encode
            cprint("[yt] stream copy failed, re-encoding...", 220)
            conv = ["ffmpeg", "-y", "-i", src,
                    "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart",
                    final]
            ret = subprocess.run(conv, capture_output=True)
        try:
            os.remove(src)
        except OSError:
            pass
        if ret.returncode == 0:
            cprint(f"[yt] saved: {final}", 46)
            return True
        cprint("[yt] ffmpeg conversion failed", 196)
        return False
    else:
        # No ffmpeg — just rename to desired ext and hope for the best
        os.rename(src, final)
        cprint(f"[yt] saved (no ffmpeg — raw): {final}", 220)
        return True

def ytdlp_vimeo(url: str, out_fmt: str = "mp4") -> bool:
    """Vimeo native extraction.

    Public videos work without credentials.  Private / login-required videos
    need browser cookies — we try the same cookie-escalation ladder as YouTube
    rather than failing hard on the first 403.
    """
    if not ytdlp_ok():
        cprint("[vimeo] yt-dlp not found — pip install yt-dlp", 196)
        return False
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    fmt_sel, extra = yt_fmt_args(out_fmt)
    player_url = vimeo_to_player_url(url)

    def _try(extra_args: list) -> bool:
        cmd = ["yt-dlp", "--no-warnings", "--newline", "--impersonate", "chrome"] + extra_args + [
            "-f", fmt_sel,
            "-o", os.path.join(config.OUTPUT_DIR, "%(title)s.%(ext)s"),
        ] + extra + [player_url]
        cprint(f"[vimeo] {' '.join(cmd)}", 245)
        return run_ytdlp_rainbow(cmd)

    # 1. Plain (public videos)
    if _try([]):
        return True
    # 2. Edge cookies (login-required videos)
    cprint("[vimeo] Retrying with --cookies-from-browser edge...", 51)
    if _try(["--cookies-from-browser", "edge"]):
        return True
    # 3. Chrome
    cprint("[vimeo] Retrying with --cookies-from-browser chrome...", 51)
    if _try(["--cookies-from-browser", "chrome"]):
        return True
    # 4. Firefox
    cprint("[vimeo] Retrying with --cookies-from-browser firefox...", 51)
    if _try(["--cookies-from-browser", "firefox"]):
        return True
    cprint("[vimeo] All attempts failed — log into Vimeo in a browser and retry.", 196)
    return False


def ytdlp_twitter(url: str, out_fmt: str = "mp4") -> bool:
    """Twitter/X native extraction. Tries without cookies first (works for
    most public tweets), then retries with Firefox cookies if that fails."""
    if not ytdlp_ok():
        cprint("[twitter] yt-dlp not found — pip install yt-dlp", 196)
        return False
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    fmt_sel, extra = yt_fmt_args(out_fmt)
    base = (["yt-dlp", "--no-warnings", "--newline",
              "-f", fmt_sel,
              "-o", os.path.join(config.OUTPUT_DIR, "%(title)s.%(ext)s")]
            + extra + [url])
    cprint("[twitter] Trying yt-dlp (no cookies)...", 39)
    cprint(f"[twitter] {' '.join(base)}", 245)
    if run_ytdlp_rainbow(base):
        return True
    cprint("[twitter] Retrying with --cookies-from-browser firefox...", 39)
    cookie_cmd = (["yt-dlp", "--cookies-from-browser", "firefox",
                   "--no-warnings", "--newline",
                   "-f", fmt_sel,
                   "-o", os.path.join(config.OUTPUT_DIR, "%(title)s.%(ext)s")]
                  + extra + [url])
    return run_ytdlp_rainbow(cookie_cmd)

def ytdlp_download(url: str, referer: str, out_fmt: str = "mp4") -> bool:
    if not ytdlp_ok():
        cprint("[yt-dlp] not found — pip install yt-dlp", 196)
        return False
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    fmt_sel, extra = yt_fmt_args(out_fmt)
    cmd = (
        ["yt-dlp",
         "--referer", referer,
         "--add-header", f"User-Agent:{UA}",
         "--extractor-args", "generic:impersonate",
         "--impersonate", "chrome",
         "--no-warnings", "--newline",
         "-f", fmt_sel,
         "-o", os.path.join(config.OUTPUT_DIR, "%(title)s.%(ext)s")]
        + extra + [url]
    )
    cprint(f"[yt-dlp] {' '.join(cmd)}", 245)
    return run_ytdlp_rainbow(cmd)
