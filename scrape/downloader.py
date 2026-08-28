"""Direct download: filename sanitizing and the ffmpeg-first / raw-HTTP-
fallback download routine with retry and a live progress bar.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from urllib.parse import unquote, urlparse

from . import config
from .config import (MAX_RETRIES, MIN_MB, STREAM_TIMEOUT,
                      FFMPEG_TIMEOUT, cdn_headers, ffmpeg_hdr_block, raw_get)
from .ui import (render_progress_bar, render_time_progress_bar, probe_duration,
                 print_mascot_success, print_mascot_fail)
from .ytdlp import ffmpeg_ok


def safe_filename(url: str, n: int = 1, ext: str = ".mp4") -> str:
    name = unquote(os.path.basename(urlparse(url).path)) or f"video_{n}"
    name = re.sub(r'\.(?:mp4|webm|mkv|m3u8|mpd|ts)$', '', name, flags=re.I) or f"video_{n}"
    return os.path.join(config.OUTPUT_DIR, f"{n:02d}_{name}{ext}")


def download_file(url: str, out_path: str, referer: str, n: int = 1, total: int = 1) -> str:
    tag = f"[{n}/{total}]"
    if os.path.exists(out_path):
        return f"{tag} SKIP (exists): {out_path}"

    if ffmpeg_ok():
        tmp = out_path + ".part.mp4"
        cmd = ["ffmpeg", "-y", "-headers", ffmpeg_hdr_block(referer),
               "-i", url, "-c", "copy", tmp]
        # best-effort size estimate for the bar (HEAD may fail/lie for
        # m3u8/token-CDN sources — falls through to time-based bar instead)
        est_total = 0
        try:
            head = raw_get(url, headers=cdn_headers(referer), stream=True, timeout=10)
            est_total = int(head.headers.get("Content-Length", 0) or 0)
        except Exception:
            pass

        duration = 0.0 if est_total > 0 else probe_duration(url, referer)
        from .ui import _ansi_ready
        use_bytes_bar = _ansi_ready() and est_total > 0
        use_time_bar = _ansi_ready() and not use_bytes_bar and duration > 0

        cmd_bar = cmd
        if use_time_bar:
            # -progress pipe:1 emits key=value lines (out_time_ms=...) we
            # parse each tick; -nostats silences ffmpeg's own status spam
            cmd_bar = ["ffmpeg", "-y", "-headers", ffmpeg_hdr_block(referer),
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

    headers = cdn_headers(referer)
    tmp = out_path + ".part"
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = raw_get(url, headers=headers, stream=True, timeout=STREAM_TIMEOUT)
            resp.raise_for_status()
            total_hdr = int(resp.headers.get("Content-Length", 0) or 0)
            size = 0
            t0 = time.time()
            from .ui import _ansi_ready
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
