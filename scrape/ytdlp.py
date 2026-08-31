"""yt-dlp integration: platform detection (YouTube/Twitter), the format-args
builder, cached ffmpeg/yt-dlp availability checks, the update check, and the
rainbow progress-bar subprocess runner.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

from . import config
from .config import UA, YTDLP_TIMEOUT, AUDIO_FMTS, base_headers, make_session
from .patterns import YT_RE
from .ui import (cprint, render_progress_bar, render_pct_bar, render_indeterminate_bar,
                 render_mascot_frame)

# Lossy audio codecs where a bitrate cap is meaningful (see AUDIO_BITRATE).
_LOSSY_AUDIO_FMTS = frozenset(("mp3", "aac", "opus", "m4a"))

# ── Cached tool checks ────────────────────────────────────────────────────────
_FFMPEG:  bool | None = None
_YTDLP:   bool | None = None
_SPOTDL:  bool | None = None

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

def spotdl_ok() -> bool:
    """spotdl is used as a Python library (see run_spotdl_library), not
    shelled out to as a CLI, so this checks for the importable package
    rather than a binary on PATH."""
    global _SPOTDL
    if _SPOTDL is None:
        import importlib.util
        _SPOTDL = importlib.util.find_spec("spotdl") is not None
    return _SPOTDL


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
def _pip_install(package: str, upgrade: bool = False) -> bool:
    """Run pip install [--upgrade] package. Returns True on success."""
    cmd = [sys.executable, "-m", "pip", "install", "--break-system-packages"]
    if upgrade:
        cmd.append("--upgrade")
    cmd.append(package)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r.returncode == 0
    except Exception:
        return False


def _pip_check_update(package: str) -> None:
    """Check PyPI for package. Auto-installs if missing, auto-upgrades if outdated.

    Uses pip show (installed version) + pip index versions (latest on PyPI).
    Skips silently on network failure.
    """
    try:
        # Check if installed
        r_inst = subprocess.run(
            [sys.executable, "-m", "pip", "show", package],
            capture_output=True, text=True, timeout=8,
        )
        if r_inst.returncode != 0:
            # Not installed — install it now
            cprint(f"[update] {package} not found — installing...", 208)
            if _pip_install(package):
                cprint(f"[update] {package} installed", 46)
            else:
                cprint(f"[update] {package} install failed — pip install {package}", 196)
            return

        installed = ""
        for line in r_inst.stdout.splitlines():
            if line.lower().startswith("version:"):
                installed = line.split(":", 1)[1].strip()
                break
        if not installed:
            return

        # Get latest available version from PyPI
        r_idx = subprocess.run(
            [sys.executable, "-m", "pip", "index", "versions", package],
            capture_output=True, text=True, timeout=8,
        )
        m = re.search(r'\(([^)]+)\)', r_idx.stdout)
        if not m:
            return
        latest = m.group(1).strip().split(",")[0].strip()

        if latest and latest != installed:
            cprint(f"[update] {package} {installed} → {latest} — updating...", 220)
            if _pip_install(package, upgrade=True):
                cprint(f"[update] {package} updated to {latest}", 46)
            else:
                cprint(f"[update] {package} update failed — pip install -U {package}", 196)
    except Exception:
        pass


def quick_update_check() -> bool:
    """Check and auto-update all dependencies in parallel.

    Results are cached to disk for UPDATE_CACHE_TTL_HOURS so the check runs
    at most once per day — not on every single launch.

    Returns True if anything was installed or upgraded (caller should
    prompt for restart so the new versions are actually loaded).
    Skips silently on network failure.
    """
    import json, tempfile, pathlib
    from concurrent.futures import ThreadPoolExecutor, as_completed

    UPDATE_CACHE_TTL_HOURS = 24
    _cache_path = pathlib.Path(tempfile.gettempdir()) / "scrape_update_cache.json"

    # --- cache gate: skip if checked recently ---
    try:
        if _cache_path.exists():
            data = json.loads(_cache_path.read_text())
            last = data.get("ts", 0)
            if time.time() - last < UPDATE_CACHE_TTL_HOURS * 3600:
                return False   # nothing to do — checked recently
    except Exception:
        pass

    changed = []

    def _check_ytdlp():
        if not ytdlp_ok():
            cprint("[update] yt-dlp not found — installing...", 208)
            if _pip_install("yt-dlp"):
                cprint("[update] yt-dlp installed", 46)
                return True
            cprint("[update] yt-dlp install failed — pip install yt-dlp", 196)
            return False
        try:
            r = subprocess.run(
                ["yt-dlp", "--update-to", "stable"],
                capture_output=True, text=True, timeout=15,
            )
            out = (r.stdout + r.stderr).strip()
            if out:
                first = out.splitlines()[0]
                if "up to date" in out.lower():
                    cprint(f"[update] yt-dlp: {first}", 245)
                    return False
                else:
                    cprint(f"[update] yt-dlp: {first}", 46)
                    return True
        except Exception:
            pass
        return False

    def _check_pip(pkg):
        """pip show + pip index versions — returns True if something changed."""
        try:
            r_inst = subprocess.run(
                [sys.executable, "-m", "pip", "show", pkg],
                capture_output=True, text=True, timeout=6,
            )
            if r_inst.returncode != 0:
                cprint(f"[update] {pkg} not found — installing...", 208)
                if _pip_install(pkg):
                    cprint(f"[update] {pkg} installed", 46)
                    return True
                cprint(f"[update] {pkg} install failed", 196)
                return False

            installed = ""
            for line in r_inst.stdout.splitlines():
                if line.lower().startswith("version:"):
                    installed = line.split(":", 1)[1].strip()
                    break
            if not installed:
                return False

            r_idx = subprocess.run(
                [sys.executable, "-m", "pip", "index", "versions", pkg],
                capture_output=True, text=True, timeout=6,
            )
            m = re.search(r'\(([^)]+)\)', r_idx.stdout)
            if not m:
                return False
            latest = m.group(1).strip().split(",")[0].strip()

            if latest and latest != installed:
                cprint(f"[update] {pkg} {installed} → {latest} — updating...", 220)
                if _pip_install(pkg, upgrade=True):
                    cprint(f"[update] {pkg} updated to {latest}", 46)
                    return True
                cprint(f"[update] {pkg} update failed", 196)
        except Exception:
            pass
        return False

    def _check_docker():
        """Docker is optional — only used by the Solverr/Byparr CF fallback
        tier (see docker_manager.py). We don't force-install it here the
        way we do pip packages: that install flow is lazy and only ever
        triggers the one time a site actually needs the fallback. This
        just reports current status in the same place as everything else.

        Docker Desktop has its own built-in auto-updater once installed
        (Settings -> Software Updates, checked automatically when the app
        is running), so there's no separate upgrade command to shell out
        to here — we just surface the installed version and point at the
        updater/download page if it looks stale or missing.
        """
        docker_bin = shutil.which("docker")
        if not docker_bin:
            from .docker_manager import _find_docker
            found_dir = _find_docker()
            if found_dir:
                docker_bin = os.path.join(found_dir, "docker.exe")
        if not docker_bin:
            cprint("[update] Docker: not installed (optional — only needed for the "
                   "Solverr CF/DDoS-Guard fallback). Install: "
                   "https://www.docker.com/products/docker-desktop/", 245)
            return False
        try:
            r = subprocess.run([docker_bin, "--version"], capture_output=True,
                               text=True, timeout=6)
            if r.returncode == 0:
                cprint(f"[update] Docker: {r.stdout.strip()} — "
                       "up-to-date checks happen inside Docker Desktop itself "
                       "(Settings > Software Updates)", 245)
            else:
                cprint("[update] Docker: found but not responding to --version — "
                       "reinstall from https://www.docker.com/products/docker-desktop/", 208)
        except Exception:
            pass
        return False  # never counts as "changed" — nothing here for us to upgrade

    # spotapi and SpotipyFree are dead/unused — removed from check list
    pip_pkgs = ("DrissionPage", "playwright", "curl_cffi", "spotdl", "rich")

    with ThreadPoolExecutor(max_workers=len(pip_pkgs) + 2) as pool:
        futures = {pool.submit(_check_ytdlp): "yt-dlp", pool.submit(_check_docker): "docker"}
        futures.update({pool.submit(_check_pip, pkg): pkg for pkg in pip_pkgs})
        for fut in as_completed(futures):
            try:
                if fut.result():
                    changed.append(futures[fut])
            except Exception:
                pass

    # Write cache timestamp regardless of whether anything changed
    try:
        _cache_path.write_text(json.dumps({"ts": time.time()}))
    except Exception:
        pass

    return bool(changed)


# ── yt-dlp format args builder (shared across all call sites) ─────────────────
def _video_quality_selector(cap: int, with_audio: bool = True) -> str:
    """Format selector capped at `cap`, orientation-agnostic.

    The quality tiers (1080/720/480/360...) are one number, but which axis
    that number caps depends on orientation: height for landscape (16:9),
    width for portrait (9:16 — reels/shorts/TikTok-style clips), since a
    portrait clip's height is the LONG edge, not the quality-defining one.

    Rather than pre-detecting orientation (which would need an extra probe
    before we even know what formats exist), we let yt-dlp's own "/"
    fallback chain do it implicitly: try the height-based filter first —
    correct for landscape, and for portrait content it naturally matches
    nothing — then only fall through to the width-based filter once that
    happens. Same cap value, whichever axis actually has it.
    """
    if with_audio:
        return (f"bestvideo[height<={cap}]+bestaudio/"
                f"bestvideo[width<={cap}]+bestaudio/"
                f"best[height<={cap}]/"
                f"best[width<={cap}]")
    return f"best[height<={cap}]/best[width<={cap}]"


def yt_fmt_args(out_fmt: str) -> tuple[str, list]:
    """Return (format_selector, extra_args) for yt-dlp.

    Video selectors are always capped at config.MAX_HEIGHT (set from the
    quality picker) — highest available quality at or below the cap, never
    an uncapped fallback that could exceed it. Works for portrait video
    (reels/shorts) too — see _video_quality_selector().
    """
    if out_fmt == "original":
        out_fmt = ""
    if out_fmt in AUDIO_FMTS:
        # Bitrate cap only makes sense for lossy codecs (mp3/aac/opus/m4a);
        # lossless (flac/wav) ignore it and always get "best".
        quality = f"{config.AUDIO_BITRATE}K" if out_fmt in _LOSSY_AUDIO_FMTS else "0"
        return ("bestaudio/best",
                ["--extract-audio", "--audio-format", out_fmt, "--audio-quality", quality])
    cap = config.MAX_HEIGHT
    sel = _video_quality_selector(cap, with_audio=ffmpeg_ok())
    if not out_fmt:
        return (sel, [])
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

def is_spotify(url: str) -> bool:
    return bool(re.search(r'https?://open\.spotify\.com/', url, re.I))


def _spotify_embed_metadata(track_id: str) -> str | None:
    """Fast path: open.spotify.com/embed/track/<id> ships a URI-encoded JSON
    blob (track name + artists) directly in the HTML — one plain HTTP GET,
    no browser, no TOTP-gated web-player token. This is what "made it fast"
    before; SpotipyFree (headless browser, ~25-30s) is now only the
    fallback if this quick path ever fails.
    """
    embed_url = f"https://open.spotify.com/embed/track/{track_id}"
    try:
        t0 = time.time()
        sess = make_session(embed_url)
        sess.headers.update(base_headers(embed_url))
        resp = sess.get(embed_url, timeout=8)
        resp.raise_for_status()
        html = resp.text

        marker = '"resource":"'
        start = html.find(marker)
        if start == -1:
            return None
        start += len(marker)
        end = html.find('"}', start)
        if end == -1:
            return None

        decoded = urllib.parse.unquote(html[start:end] + '"}')
        data = json.loads(decoded)
        title = (data.get("name") or "").strip()
        artists = data.get("artists") or []
        artist = artists[0].get("name", "").strip() if artists else ""
        result = f"{artist} {title}".strip()
        elapsed = time.time() - t0
        cprint(f"[spotify] metadata via embed page in {elapsed:.2f}s: {result!r}", 245)
        if result and result.lower() not in _SPOTIFY_GARBAGE:
            return result
    except Exception as e:
        cprint(f"[spotify] embed metadata failed: {type(e).__name__}: {e} — falling back to SpotipyFree...", 208)
    return None


def _spotify_track_name(track_id: str) -> str | None:
    """Get track title + artist from Spotify without the TOTP-gated web player endpoint.

    Strategy: embed page (fast, plain HTTP) → SpotipyFree (slow, headless
    browser) → spotapi. Only falls to the slower tiers if the faster one
    fails outright.

    Returns "Artist Title" string, or None if lookup fails.
    """
    fast = _spotify_embed_metadata(track_id)
    if fast:
        return fast

    try:
        t0 = time.time()
        from SpotipyFree import Spotify as FreeSpotify  # already a dep of spotdl
        fs = FreeSpotify(
            client_id=_SPOTDL_CLIENT_ID,
            client_secret=_SPOTDL_CLIENT_SECRET,
            headless=True,
            no_cache=True,
        )
        data = fs.track(track_id)
        title  = (data.get("name") or "").strip()
        # artists field is a list of dicts with "name" key (same shape as spotipy)
        artists = data.get("artists") or []
        artist = artists[0].get("name", "").strip() if artists else ""
        result = f"{artist} {title}".strip()
        elapsed = time.time() - t0
        cprint(f"[spotify] metadata via SpotipyFree in {elapsed:.2f}s: {result!r}", 245)
        if result and result.lower() not in ("spotify", ""):
            return result
    except Exception as e:
        cprint(f"[spotify] SpotipyFree metadata failed: {type(e).__name__}: {e}", 208)

    return None


# Garbage values Spotify returns when the API/page is blocked or JS-only
_SPOTIFY_GARBAGE = {"spotify", "", "spotify - web player"}


# ── spotdl progress runner (library-backed, real 0-100% callback) ─────────────
# spotdl's CLI never gets us a real number here: its numeric progress
# (0-100) lives entirely inside a Rich Progress object, which Rich itself
# refuses to render once stdout isn't a real terminal (which it isn't —
# we pipe it). --simple-tui, its only other CLI mode, only ever logs
# stage labels ("Searching for song", "Downloading", ...), never a digit.
# The fix is to skip the CLI and call spotdl as a Python library instead,
# hooking its internal SongTracker callback directly — that callback
# fires with a real 0-100 int on every step regardless of what stdout is.
_SPOTDL_CLIENT_ID = "5f573c9620494bae87890c0f08a60293"
_SPOTDL_CLIENT_SECRET = "212476d9b0f3472eaa762d90b19b0ba8"


def _spotdl_client(audio_fmt: str, out_tmpl: str, update_callback=None):
    """Build a configured Spotdl instance.

    - lyrics_providers=[]  → no genius/azlyrics/musixmatch network requests at all
    - audio_providers=["youtube"] → regular YouTube, avoids YTM rate-limit errors
    - bitrate="disable"    → remux only, skip ffmpeg re-encode (2-3x faster)
    - simple_tui=True      → spotdl won't try to draw its own Rich progress UI
    - ProgressHandler(web_ui=True) → yt_dlp_progress_hook and ffmpeg_progress_hook
      give real 0-100 values instead of the hardcoded 70 they emit under
      simple_tui=True + web_ui=False (confirmed in spotdl 4.5.2 source)

    SpotifyClient is a singleton that raises on double-init. Spotdl.__init__
    always calls SpotifyClient.init() — so we guard here: if it's already
    initialized (second download in a session) we re-use the existing instance
    by bypassing Spotdl() and building the Downloader directly.
    """
    from spotdl import Spotdl
    from spotdl.download.downloader import Downloader
    from spotdl.download.progress_handler import ProgressHandler
    from spotdl.utils.spotify import SpotifyClient

    downloader_settings = {
        "format": audio_fmt,
        "output": out_tmpl,
        "audio_providers": ["youtube"],
        "lyrics_providers": [],   # no lyrics lookups — we don't use them
        "bitrate": "disable",
        "threads": 1,
        "simple_tui": True,
        "print_errors": False,
    }

    if SpotifyClient._instance is None:
        client = Spotdl(
            client_id=_SPOTDL_CLIENT_ID,
            client_secret=_SPOTDL_CLIENT_SECRET,
            headless=True,
            downloader_settings=downloader_settings,
        )
    else:
        # Singleton already initialized — calling Spotdl() again would raise
        # SpotifyError("A spotify client has already been initialized").
        # Spotdl.__init__ only does two things: SpotifyClient.init() + Downloader().
        # Since SpotifyClient is already up, build just the Downloader.
        # Wrap it in a minimal shim so callers see the same .search() and
        # .download_songs() interface as a real Spotdl instance.
        from spotdl.utils.query import parse_query

        class _SpotdlShim:
            def __init__(self, dl: Downloader) -> None:
                self.downloader = dl

            def search(self, query):
                return parse_query(
                    query=query,
                    threads=self.downloader.settings["threads"],
                    use_ytm_data=self.downloader.settings["ytm_data"],
                    playlist_numbering=self.downloader.settings["playlist_numbering"],
                    album_type=self.downloader.settings["album_type"],
                    playlist_retain_track_cover=self.downloader.settings[
                        "playlist_retain_track_cover"
                    ],
                )

            def download_songs(self, songs):
                return self.downloader.download_multiple_songs(songs)

        client = _SpotdlShim(Downloader(settings=downloader_settings))

    # Replace the progress handler with one that reports real numbers.
    # web_ui=True is the key: without it, yt_dlp_progress_hook and
    # ffmpeg_progress_hook both hardcode self.progress = 70 regardless
    # of actual download state (spotdl 4.5.2 source confirmed).
    client.downloader.progress_handler = ProgressHandler(
        simple_tui=True,
        web_ui=True,
        update_callback=update_callback,
    )
    return client


def run_spotdl_library(url: str, audio_fmt: str, out_tmpl: str, timeout: int = 600) -> bool:
    """Download via spotdl's Python API, rendering real per-song progress as a
    rainbow bar. Architecture:

        worker thread  →  mutate state dict only
        render loop    →  read state, call live.update() — sole UI owner

    The worker never touches Live directly; this eliminates the two-threads-
    racing-live.update() bug that caused frozen/stuttering animation.

    Progress is real 0-100 from spotdl's SongTracker callback, not fabricated.
    Requires web_ui=True in ProgressHandler — see _spotdl_client docstring.
    """
    import logging
    from .ui import _ansi_ready, _console
    from rich.live import Live
    from rich.text import Text

    logging.getLogger("spotdl").setLevel(logging.ERROR)

    def _run_sync() -> tuple[bool, str | None]:
        try:
            client = _spotdl_client(audio_fmt, out_tmpl)
            songs = client.search([url])
            if not songs:
                return False, "no song metadata found"
            results = client.download_songs(songs)
            return any(path is not None for _, path in results), None
        except Exception as e:
            return False, str(e)

    if not _ansi_ready():
        ok, err = _run_sync()
        if err:
            cprint(f"[spotdl] {err}", 196)
        return ok

    t0 = time.time()
    result: dict = {"ok": False, "error": None}
    # pct=None until the first real callback fires. During metadata lookup and
    # YouTube search there is no download progress — show indeterminate rather
    # than a fake 0%.  phase carries the current stage label for display.
    state = {"pct": None, "phase": "resolving...", "song_name": ""}
    timed_out = False

    def _frame() -> Text:
        elapsed = time.time() - t0
        phase_label = state["phase"]
        if state["song_name"]:
            phase_label = f"{state['song_name']}: {phase_label}"
        if state["pct"] is not None:
            bar = render_pct_bar(state["pct"], elapsed=elapsed)
        else:
            bar = render_indeterminate_bar(elapsed=elapsed, label=phase_label)
        # Show phase above bar when we have real progress too
        mascot = render_mascot_frame(elapsed)
        if state["pct"] is not None:
            lines = mascot + [f"\033[38;5;245m{phase_label}\033[0m", bar]
        else:
            lines = mascot + [bar]
        return Text.from_ansi("\n".join(lines))

    def _worker() -> None:
        """Worker: mutates state, never calls live.update(). The render loop
        owns all UI. Status messages go through _console.print() which is
        thread-safe and routes above the Live display correctly."""
        try:
            def update_callback(tracker, message):
                # Real progress: 0-100 int from SongTracker.progress.
                # web_ui=True in ProgressHandler means yt_dlp_progress_hook
                # and ffmpeg_progress_hook give actual values, not hardcoded 70.
                new_pct = max(0.0, min(1.0, tracker.progress / 100.0))
                old_phase = state["phase"]
                state["pct"] = new_pct
                state["phase"] = message
                state["song_name"] = tracker.song_name
                # Only print phase transitions, not every tick, to avoid flooding
                if message != old_phase:
                    _console.print(Text.from_ansi(
                        f"\033[38;5;245m[spotdl] {tracker.song_name}: {message}\033[0m"
                    ))
                # No live.update() here — the render loop handles that

            client = _spotdl_client(audio_fmt, out_tmpl, update_callback)
            state["phase"] = "searching YouTube..."
            songs = client.search([url])
            if not songs:
                result["error"] = "no song metadata found"
                return
            state["phase"] = "downloading..."
            results = client.download_songs(songs)
            result["ok"] = any(path is not None for _, path in results)
        except Exception as e:
            result["error"] = str(e)

    with Live(_frame(), console=_console, refresh_per_second=12, transient=True) as live:
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        # Render loop: sole owner of live.update(). Runs at ~12fps independently
        # of how often the worker fires callbacks — mascot always animates.
        while t.is_alive():
            if time.time() - t0 > timeout:
                timed_out = True
                break
            live.update(_frame())
            time.sleep(0.08)
        t.join(timeout=2)
        # Final frame with completion state
        live.update(_frame())

    if timed_out:
        cprint("[spotdl] timed out", 196)
        return False
    if result["error"]:
        cprint(f"[spotdl] {result['error']}", 196)
    return result["ok"]


def spotdl_download(url: str, out_fmt: str = "mp3") -> bool:
    """Download a Spotify track/album/playlist.

    Fast path (single tracks — yt-dlp direct search):
      1. Fetch title+artist from Spotify's anonymous Web API (curl_cffi, no login).
      2. Validate the result isn't garbage ("Spotify", empty, etc.).
      3. Search regular YouTube (ytsearch1:) — fast, no rate limits.
      4. If that fails, try YouTube Music (ytmsearch1:) as fallback.

    Slow path (if metadata fetch failed, OR album/playlist URL):
      spotdl called as a Python library (not shelled out to) with
      audio_providers=["youtube"] (regular YouTube, not YTM — avoids the
      "no usable results after 3 attempts" YTM rate-limit error) and
      bitrate="disable" (skip ffmpeg re-encode, remux only — 2-3x faster).
      Library, not CLI, because that's the only way to get a real 0-100
      progress number instead of stage labels — see run_spotdl_library.

    Supported formats: mp3, flac, ogg, opus, m4a, wav. Anything else → mp3.
    """
    clean_url = url.split("?")[0]
    if clean_url != url:
        cprint(f"[spotify] stripped tracking params → {clean_url}", 245)

    SPOTDL_FMTS = {"mp3", "flac", "ogg", "opus", "m4a", "wav"}
    audio_fmt = out_fmt if out_fmt in SPOTDL_FMTS else "mp3"
    if out_fmt not in SPOTDL_FMTS:
        cprint(f"[spotify] {out_fmt!r} not supported — using mp3", 208)

    os.makedirs(config.dir_for(audio_fmt), exist_ok=True)
    out_tmpl = os.path.join(config.dir_for(audio_fmt), "%(title)s - %(uploader)s.%(ext)s")

    def _ytdlp_search(prefix: str, query: str, label: str) -> bool:
        cmd = [
            "yt-dlp", f"{prefix}:{query}",
            "--extract-audio", "--audio-format", audio_fmt, "--audio-quality", "0",
            "--no-warnings", "--newline",
            "-o", out_tmpl,
        ]
        cprint(f"[spotify] {label}: {query!r}", 51)
        cprint(f"[spotify] {' '.join(cmd)}", 245)
        try:
            return run_ytdlp_rainbow(cmd, timeout=180)
        except Exception:
            return False

    # ── Fast path: single tracks only ────────────────────────────────────────
    if "/track/" in clean_url and ytdlp_ok():
        track_id = clean_url.rstrip("/").split("/")[-1]
        cprint(f"[spotify] fetching track metadata ({track_id})...", 245)
        raw_query = _spotify_track_name(track_id)

        # Reject garbage — if Spotify's CDN blocked us we get "Spotify" or ""
        if raw_query and raw_query.lower().strip() not in _SPOTIFY_GARBAGE and len(raw_query) > 4:
            cprint(f"[spotify] got: {raw_query!r}", 82)
            if _ytdlp_search("ytsearch1", raw_query, "searching YouTube"):
                return True
            cprint("[spotify] YouTube search failed — trying YouTube Music...", 208)
            if _ytdlp_search("ytmsearch1", raw_query, "searching YouTube Music"):
                return True
            cprint("[spotify] both search paths failed — falling back to spotdl", 208)
        else:
            if raw_query:
                cprint(f"[spotify] metadata looks like garbage ({raw_query!r}) — skipping search", 208)
            else:
                cprint("[spotify] metadata fetch failed — going straight to spotdl", 208)

    # ── Slow path: spotdl as a library ───────────────────────────────────────
    # audio_providers=["youtube"] → regular YouTube search (no YTM rate limiting)
    # bitrate="disable"           → remux only, skip re-encode (2-3x faster)
    # Called as a library (not the CLI) specifically so we get a real
    # 0-100 progress callback instead of stage labels only — see
    # run_spotdl_library's docstring.
    if not spotdl_ok():
        cprint("[spotify] spotdl not installed — pip install spotdl", 196)
        return False

    cprint("[spotify] handing off to spotdl (youtube provider, no re-encode)...", 208)
    spotdl_out_tmpl = os.path.join(config.dir_for(audio_fmt), "{title} - {artists}.{output-ext}")
    try:
        return run_spotdl_library(clean_url, audio_fmt, spotdl_out_tmpl, timeout=600)
    except Exception as e:
        cprint(f"[spotify] spotdl error: {e}", 196)
        return False


# ── Rainbow yt-dlp progress runner ────────────────────────────────────────────
_PCT_RE  = re.compile(r'\[download\]\s+([\d.]+)%')
_SIZE_RE = re.compile(
    r'\[download\]\s+[\d.]+%\s+of\s+~?\s*([\d.]+)(MiB|GiB|KiB|B)'
)
_MUL = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}

def run_ytdlp_rainbow(cmd: list, timeout: int = YTDLP_TIMEOUT) -> bool:
    """Run yt-dlp and render its progress as a rainbow bar."""
    from .ui import _ansi_ready, _console, PipeReader
    from rich.live import Live
    from rich.text import Text

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

    t0 = time.time()
    state = {"pct": None, "done_b": 0, "total_b": 0}

    def _current_bar(elapsed: float) -> str:
        if state["total_b"] > 0:
            return render_progress_bar(state["done_b"], state["total_b"], elapsed=elapsed)
        if state["pct"] is not None:
            return render_pct_bar(state["pct"], elapsed=elapsed)
        return render_indeterminate_bar(elapsed=elapsed, label="working...")

    def _frame() -> Text:
        elapsed = time.time() - t0
        lines = render_mascot_frame(elapsed) + [_current_bar(elapsed)]
        return Text.from_ansi("\n".join(lines))

    reader = PipeReader(proc.stdout)
    timed_out = False

    with Live(_frame(), console=_console, refresh_per_second=12, transient=True) as live:
        while True:
            import queue as _q
            try:
                raw = reader.queue.get(timeout=0.08)
                line = raw.rstrip()
                if line:
                    elapsed = time.time() - t0
                    pm = _PCT_RE.match(line)
                    if pm:
                        pct = float(pm.group(1)) / 100.0
                        state["pct"] = pct
                        sm = _SIZE_RE.match(line)
                        if sm:
                            state["total_b"] = int(float(sm.group(1)) * _MUL.get(sm.group(2), 1))
                            state["done_b"] = int(pct * state["total_b"])
                        elif state["total_b"]:
                            state["done_b"] = int(pct * state["total_b"])
                    else:
                        # yt-dlp log line (merge, postprocess, etc) — print above live
                        _console.print(Text.from_ansi(f"\033[38;5;245m{line}\033[0m"))
            except _q.Empty:
                pass
            # This check used to be entirely missing: `timeout` was accepted
            # as a parameter but never compared against elapsed time in this
            # (the interactive/Rich) branch, so a hung yt-dlp process here
            # had no enforced ceiling at all — only the non-ANSI fallback
            # branch above ever actually honored it.
            if time.time() - t0 > timeout:
                timed_out = True
                break
            live.update(_frame())
            if reader.is_done():
                break

    if timed_out:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        cprint(f"[yt-dlp] timed out after {timeout}s", 196)
        return False

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        return False
    return proc.returncode == 0


# ── Shared cookie-escalation ladder ────────────────────────────────────────────
def _cookie_escalation(attempt, browsers: tuple[str, ...], label: str) -> bool:
    """Run `attempt(browser)` with browser=None first (no cookies), then
    each browser in `browsers` in order, stopping at the first success.
    `attempt` takes the browser name (or None) and returns whether that
    run succeeded. Shared by the YouTube/Vimeo/Twitter cookie-retry paths
    so the ladder logic lives in one place instead of three."""
    if attempt(None):
        return True
    for browser in browsers:
        cprint(f"[{label}] Retrying with --cookies-from-browser {browser}...", 51)
        if attempt(browser):
            return True
    return False


# ── Shared yt-dlp command builder (generic-engine path) ───────────────────────
def build_ytdlp_generic_cmd(url: str, out_fmt: str, referer: str | None = None,
                             progress_flag: str = "--newline",
                             ua: str | None = None,
                             cookiefile: str | None = None) -> list:
    """Build a yt-dlp command for the generic-extractor / impersonation path —
    shared by ytdlp_download() and the CDN fallback in
    browser_intercept_and_download() so the flag set lives in one place.

    ua        — override User-Agent (must match the one used during CF clearance)
    cookiefile — path to a Netscape cookie file (from _write_cookiejar)
    """
    fmt_sel, extra = yt_fmt_args(out_fmt)
    effective_ua = ua or UA
    cmd = ["yt-dlp"]
    if referer:
        cmd += ["--referer", referer]
    if cookiefile:
        cmd += ["--cookies", cookiefile]
    cmd += ["--add-header", f"User-Agent:{effective_ua}",
            "--extractor-args", "generic:impersonate",
            "--impersonate", "chrome",
            "--socket-timeout", "15",
            "--retries", "2",
            "--no-warnings", progress_flag,
            "-f", fmt_sel,
            "-o", os.path.join(config.dir_for(out_fmt), "%(title)s.%(ext)s")]
    return cmd + extra + [url]


# ── yt-dlp download helpers ────────────────────────────────────────────────────
def ytdlp_youtube(url: str, out_fmt: str = "mp4") -> bool:
    """Download via yt-dlp using best available format, then convert with ffmpeg.
    Avoids all PO Token / format-selector negotiation failures."""
    if not ytdlp_ok():
        cprint("[yt] yt-dlp not found — pip install yt-dlp", 196)
        return False
    os.makedirs(config.dir_for(out_fmt), exist_ok=True)

    # Step 1: let yt-dlp grab whatever it can — no format filtering, no codec constraints.
    # audio-only request stays audio-only; everything else gets best+audio merged.
    if out_fmt in AUDIO_FMTS:
        dl_fmt_sel = "bestaudio/best"
        dl_extra = []
    else:
        cap = config.MAX_HEIGHT
        dl_fmt_sel = _video_quality_selector(cap, with_audio=ffmpeg_ok())
        dl_extra = ["--merge-output-format", "mkv"] if ffmpeg_ok() else []

    # Use a temp filename so we can locate the file after download regardless of ext
    tmp_template = os.path.join(config.dir_for(out_fmt), "%(title)s.ytdl.%(ext)s")
    # Just let yt-dlp do its thing with no extractor args — it handles
    # client selection and PO tokens internally when up to date.
    def _try(browser: str | None) -> bool:
        extra_args = ["--cookies-from-browser", browser] if browser else []
        cmd = (
            ["yt-dlp", "--no-warnings", "--newline"]
            + extra_args
            + ["-f", dl_fmt_sel]
            + dl_extra
            + ["-o", tmp_template, url]
        )
        cprint(f"[yt] {' '.join(cmd)}", 245)
        return run_ytdlp_rainbow(cmd)

    # Plain run first (works for most videos), then Edge/Chrome/Firefox cookies
    # in turn for the 403-on-CDN case.
    if not _cookie_escalation(_try, ("edge", "chrome", "firefox"), "yt"):
        cprint("[yt] all attempts failed — log into YouTube in Edge/Chrome and retry.", 196)
        return False

    # Step 2: find the downloaded file (pattern: *.ytdl.*)
    matches = sorted(glob.glob(os.path.join(config.dir_for(out_fmt), "*.ytdl.*")))
    if not matches:
        cprint("[yt] download succeeded but no output file found", 196)
        return False
    src = matches[-1]  # most recent if somehow multiple

    # Audio or original — strip the .ytdl. marker then convert if needed
    if out_fmt in AUDIO_FMTS or not out_fmt:
        if not out_fmt:
            # original — just rename
            final = src.replace(".ytdl.", ".")
            os.rename(src, final)
            cprint(f"[yt] saved: {final}", 46)
            return True

        # Actual audio format requested (mp3, aac, flac, etc.)
        # Check if it's already the right format (e.g. asked mp3, got mp3)
        src_ext = os.path.splitext(src)[1].lstrip(".")
        base_no_marker = src.replace(".ytdl.", ".")
        final = os.path.splitext(base_no_marker)[0] + f".{out_fmt}"

        if src_ext == out_fmt:
            os.rename(src, final)
            cprint(f"[yt] saved: {final}", 46)
            return True

        # Need ffmpeg to convert (e.g. webm/opus → mp3)
        if ffmpeg_ok():
            cprint(f"[yt] converting {src_ext} → {out_fmt} ...", 220)
            quality_args = (["-b:a", f"{config.AUDIO_BITRATE}k"]
                             if out_fmt in _LOSSY_AUDIO_FMTS else ["-q:a", "0"])
            cmd_ff = ["ffmpeg", "-y", "-i", src, "-vn",
                      "-acodec", "libmp3lame" if out_fmt == "mp3" else out_fmt,
                      *quality_args, final]
            r = subprocess.run(cmd_ff, capture_output=True)
            if r.returncode == 0 and os.path.exists(final):
                os.remove(src)
                cprint(f"[yt] saved: {final}", 46)
                return True
            cprint(f"[yt] ffmpeg audio conversion failed — keeping original", 208)
        # ffmpeg not available or failed — rename as-is and warn
        final_fallback = src.replace(".ytdl.", ".")
        os.rename(src, final_fallback)
        cprint(f"[yt] saved as {src_ext} (ffmpeg needed for {out_fmt}): {final_fallback}", 208)
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
    os.makedirs(config.dir_for(out_fmt), exist_ok=True)
    fmt_sel, extra = yt_fmt_args(out_fmt)
    player_url = vimeo_to_player_url(url)

    def _try(browser: str | None) -> bool:
        extra_args = ["--cookies-from-browser", browser] if browser else []
        cmd = ["yt-dlp", "--no-warnings", "--newline", "--impersonate", "chrome"] + extra_args + [
            "-f", fmt_sel,
            "-o", os.path.join(config.dir_for(out_fmt), "%(title)s.%(ext)s"),
        ] + extra + [player_url]
        cprint(f"[vimeo] {' '.join(cmd)}", 245)
        return run_ytdlp_rainbow(cmd)

    # Plain (public videos) first, then Edge/Chrome/Firefox cookies for
    # login-required ones.
    if _cookie_escalation(_try, ("edge", "chrome", "firefox"), "vimeo"):
        return True
    cprint("[vimeo] All attempts failed — log into Vimeo in a browser and retry.", 196)
    return False


def ytdlp_twitter(url: str, out_fmt: str = "mp4") -> bool:
    """Twitter/X native extraction. Tries without cookies first (works for
    most public tweets), then retries with Firefox cookies if that fails."""
    if not ytdlp_ok():
        cprint("[twitter] yt-dlp not found — pip install yt-dlp", 196)
        return False
    os.makedirs(config.dir_for(out_fmt), exist_ok=True)
    fmt_sel, extra = yt_fmt_args(out_fmt)

    def _try(browser: str | None) -> bool:
        extra_args = ["--cookies-from-browser", browser] if browser else []
        cmd = (["yt-dlp"] + extra_args + ["--no-warnings", "--newline",
                 "-f", fmt_sel,
                 "-o", os.path.join(config.dir_for(out_fmt), "%(title)s.%(ext)s")]
               + extra + [url])
        cprint(f"[twitter] {' '.join(cmd)}", 245)
        return run_ytdlp_rainbow(cmd)

    cprint("[twitter] Trying yt-dlp (no cookies)...", 39)
    return _cookie_escalation(_try, ("firefox",), "twitter")

def _write_cookiejar(cookies: dict, ua: str, domain: str = "") -> str:
    """Write a Netscape-format cookie file from a {name: value} dict.

    domain must match the actual request host or yt-dlp silently drops the
    cookie.  We write both the bare domain and dot-prefixed form so
    subdomains are covered too.
    """
    from urllib.parse import urlparse
    if domain.startswith("http"):
        host = urlparse(domain).netloc
    else:
        host = domain
    host = host.split(":")[0]
    dot_host = f".{host}" if not host.startswith(".") else host

    fd, path = tempfile.mkstemp(suffix=".txt", prefix="scrape_cf_")
    with os.fdopen(fd, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for name, value in cookies.items():
            f.write(f"{host}\tFALSE\t/\tFALSE\t9999999999\t{name}\t{value}\n")
            f.write(f"{dot_host}\tTRUE\t/\tFALSE\t9999999999\t{name}\t{value}\n")
    return path


def ytdlp_download(url: str, referer: str, out_fmt: str = "mp4",
                   cf_session: dict | None = None) -> bool:
    """Generic yt-dlp download.

    cf_session — dict with 'cookies' ({name: value}) and 'ua' (string)
    obtained from get_cf_session() after Chrome clears a Cloudflare challenge.
    When present, cookies are written to a temp Netscape file and passed via
    --cookies so yt-dlp rides the same CF-cleared session.  The file is
    deleted automatically on return.
    """
    if not ytdlp_ok():
        cprint("[yt-dlp] not found — pip install yt-dlp", 196)
        return False
    os.makedirs(config.dir_for(out_fmt), exist_ok=True)

    cookiefile = None
    try:
        ua = UA
        if cf_session:
            ua = cf_session.get('ua', UA)
            cookiefile = _write_cookiejar(cf_session['cookies'], ua, domain=url)
            cprint(f"[yt-dlp] Injecting CF session ({len(cf_session['cookies'])} cookies)", 51)

        cmd = build_ytdlp_generic_cmd(url, out_fmt, referer=referer,
                                      progress_flag="--newline", ua=ua,
                                      cookiefile=cookiefile)
        cprint(f"[yt-dlp] {' '.join(cmd)}", 245)
        return run_ytdlp_rainbow(cmd)
    finally:
        if cookiefile and os.path.exists(cookiefile):
            try:
                os.remove(cookiefile)
            except OSError:
                pass
