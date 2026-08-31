"""Lazy Docker + Byparr lifecycle management.

Nothing here runs unless the pipeline actually hits a blocked page and
reaches for the Solverr fallback tier (pipeline.py Step 2b). On a normal
run against a site the existing browser stack already handles, none of
this executes — same "quiet unless needed" pattern as ffmpeg_ok()/
ytdlp_ok() in ytdlp.py.

Behavior:
  - Docker reachable already            -> do nothing, silent.
  - Docker installed, daemon asleep     -> start it (Docker Desktop /
                                            systemctl), wait for it.
  - Docker not found on PATH, but found
    at the default install location     -> use that path directly, no
                                            prompt needed.
  - Docker not found on PATH OR at the
    default location                    -> ask ONCE: give a custom
                                            install folder, or press
                                            Enter to install it fresh.
                                            The answer is cached in
                                            ~/.scrape/docker_config.json
                                            so this never asks twice.
  - Truly not installed anywhere        -> download the official
                                            installer and run it
                                            (Windows: silent flags first,
                                            falls back to the normal
                                            installer UI if that's
                                            rejected — a UAC prompt is
                                            unavoidable, that's Windows,
                                            not this script).

At the end of a scrape session, stop_byparr_if_started() stops the
Byparr container (not Docker Desktop itself) if this run was the one
that started it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

from .ui import cprint

CONTAINER_NAME = "byparr"
IMAGE = "ghcr.io/thephaseless/byparr:latest"
PORT = 8191
DOCKER_START_TIMEOUT = 60
CONTAINER_START_TIMEOUT = 20

_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".scrape")
_CONFIG_PATH = os.path.join(_CONFIG_DIR, "docker_config.json")

_DEFAULT_WIN_DOCKER_DIR = r"C:\Program Files\Docker\Docker\resources\bin"
_DEFAULT_WIN_DESKTOP_EXE = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
_WIN_INSTALLER_URL = "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe"

# Set to True by ensure_solverr_ready() if it actually starts the
# container this session — cleanup only touches what we touched.
_STARTED_THIS_SESSION = False


def _run(cmd: list[str], timeout: float = 10) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 1, "timed out"


def _load_cached_path() -> str | None:
    try:
        with open(_CONFIG_PATH) as f:
            return json.load(f).get("docker_dir")
    except Exception:
        return None


def _save_cached_path(path: str) -> None:
    try:
        os.makedirs(_CONFIG_DIR, exist_ok=True)
        with open(_CONFIG_PATH, "w") as f:
            json.dump({"docker_dir": path}, f)
    except Exception:
        pass  # non-fatal — worst case it asks again next time


def _docker_dir_from_path(path: str) -> str | None:
    """Given a folder the user gives us, find the docker binary/exe."""
    for candidate in ("docker.exe", "docker"):
        full = os.path.join(path, candidate)
        if os.path.isfile(full):
            return path
    return None


def _find_docker() -> str | None:
    """Locate a usable docker executable's directory, or None.
    Order: PATH -> Windows default install dir -> cached custom path."""
    import shutil
    if shutil.which("docker"):
        return None  # already on PATH, nothing extra needed

    if sys.platform == "win32" and os.path.isdir(_DEFAULT_WIN_DOCKER_DIR):
        if _docker_dir_from_path(_DEFAULT_WIN_DOCKER_DIR):
            return _DEFAULT_WIN_DOCKER_DIR

    cached = _load_cached_path()
    if cached and _docker_dir_from_path(cached):
        return cached

    return None


def _prompt_for_docker_path() -> str | None:
    """Only reached when Docker is on neither PATH nor the default
    location — ask once, cache the answer."""
    cprint("[docker] Couldn't find Docker at the default location.", 208)
    answer = input(
        "  If you installed it somewhere custom, paste that folder now.\n"
        "  Otherwise just press Enter and I'll install it fresh: "
    ).strip().strip('"')
    if not answer:
        return None
    found = _docker_dir_from_path(answer)
    if not found:
        cprint(f"[docker] No docker executable found in {answer} — installing fresh instead.", 208)
        return None
    _save_cached_path(found)
    cprint("[docker] Got it — remembered for next time.", 46)
    return found


def _download_and_install_docker() -> bool:
    if sys.platform != "win32":
        cprint("[docker] Automatic install is only wired up for Windows right now. "
               "Grab Docker Desktop from https://www.docker.com/products/docker-desktop/ "
               "and re-run. Continuing without the Solverr fallback for now.", 208)
        return False

    cprint("[docker] Downloading Docker Desktop installer...", 208)
    installer_path = os.path.join(tempfile.gettempdir(), "DockerDesktopInstaller.exe")
    try:
        urllib.request.urlretrieve(_WIN_INSTALLER_URL, installer_path)
    except Exception as e:
        cprint(f"[docker] Download failed: {e}. Install manually from "
               "https://www.docker.com/products/docker-desktop/", 196)
        return False

    cprint("[docker] Installing (default location)... you may see a Windows "
           "permission prompt and/or Docker's own installer window — that part "
           "needs your OK once.", 208)
    # Try the documented silent flags first; fall back to the interactive
    # installer if the silent path is rejected (e.g. Windows blocks it).
    code, _ = _run([installer_path, "install", "--quiet", "--accept-license"], timeout=600)
    if code != 0:
        try:
            subprocess.Popen([installer_path])
        except Exception as e:
            cprint(f"[docker] Couldn't launch installer: {e}", 196)
            return False
        cprint("[docker] Installer window opened — finish it, then re-run scrape.", 208)
        return False  # can't know when a GUI install finishes; don't block this run on it

    cprint("[docker] Installed. It may need a moment (or a restart) before the "
           "engine comes up.", 46)
    return True


def docker_running() -> bool:
    code, _ = _run(["docker", "info"], timeout=5)
    return code == 0


def _launch_docker_desktop(extra_dir: str | None = None) -> bool:
    if sys.platform == "win32":
        exe = _DEFAULT_WIN_DESKTOP_EXE
        if extra_dir:
            candidate = os.path.join(os.path.dirname(extra_dir), "Docker Desktop.exe")
            if os.path.isfile(candidate):
                exe = candidate
        try:
            subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False
    elif sys.platform == "darwin":
        code, _ = _run(["open", "-a", "Docker"])
        return code == 0
    else:
        code, _ = _run(["systemctl", "start", "docker"])
        return code == 0


def ensure_docker_running() -> bool:
    if docker_running():
        return True

    found_dir = _find_docker()
    if found_dir is None:
        import shutil
        if shutil.which("docker") is None:
            # Not on PATH and not at the default location — the one case
            # that warrants asking.
            found_dir = _prompt_for_docker_path()
            if found_dir is None:
                if not _download_and_install_docker():
                    return False
                # Freshly installed — give the daemon a beat before polling.
                time.sleep(5)

    cprint("[docker] Starting Docker Desktop...", 208)
    if not _launch_docker_desktop(found_dir):
        cprint("[docker] Couldn't start it automatically — start Docker yourself "
               "and re-run if you need the Solverr fallback.", 196)
        return False

    deadline = time.time() + DOCKER_START_TIMEOUT
    while time.time() < deadline:
        if docker_running():
            cprint("[docker] Docker is up.", 46)
            return True
        time.sleep(2)

    cprint(f"[docker] Still not ready after {DOCKER_START_TIMEOUT}s — giving up for this run.", 196)
    return False


def _container_status() -> str | None:
    code, out = _run(["docker", "inspect", "-f", "{{.State.Status}}", CONTAINER_NAME])
    return out.strip() if code == 0 else None


def ensure_byparr_running() -> bool:
    global _STARTED_THIS_SESSION
    status = _container_status()

    if status == "running":
        return True

    if status == "exited":
        cprint("[docker] Restarting existing Byparr container...", 208)
        code, out = _run(["docker", "start", CONTAINER_NAME], timeout=30)
        if code != 0:
            cprint(f"[docker] Failed to restart Byparr: {out.strip()[-300:]}", 196)
            return False
        _STARTED_THIS_SESSION = True
    elif status is None:
        cprint(f"[docker] Pulling and starting Byparr ({IMAGE})... first time only.", 208)
        code, out = _run([
            "docker", "run", "-d", "--name", CONTAINER_NAME,
            "-p", f"{PORT}:{PORT}", "--restart", "unless-stopped", IMAGE,
        ], timeout=300)
        if code != 0:
            cprint(f"[docker] Failed to start Byparr: {out.strip()[-300:]}", 196)
            return False
        _STARTED_THIS_SESSION = True

    deadline = time.time() + CONTAINER_START_TIMEOUT
    while time.time() < deadline:
        if _container_status() == "running":
            cprint("[docker] Byparr is up.", 46)
            return True
        time.sleep(1)
    return False


def ensure_solverr_ready() -> bool:
    if not ensure_docker_running():
        return False
    return ensure_byparr_running()


def stop_byparr_if_started() -> None:
    """Called once at the end of a scrape session (see cli.py). Only stops
    the container if this run was the one that (re)started it — never
    touches Docker Desktop itself, and never stops a container the user
    was already running on their own before scrape touched anything."""
    global _STARTED_THIS_SESSION
    if not _STARTED_THIS_SESSION:
        return
    _run(["docker", "stop", CONTAINER_NAME], timeout=15)
    _STARTED_THIS_SESSION = False
