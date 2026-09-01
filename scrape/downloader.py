"""Direct download: filename sanitizing and the ffmpeg-first / raw-HTTP-
fallback download routine with retry and a live progress bar.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import unquote, urlparse

from . import config
from .config import (MAX_RETRIES, MIN_MB, STREAM_TIMEOUT,
                      FFMPEG_TIMEOUT, cdn_headers, ffmpeg_hdr_block, raw_get)
from .ui import (render_progress_bar, render_time_progress_bar, render_indeterminate_bar,
                 render_mascot_frame, probe_duration, print_mascot_success, print_mascot_fail,
                 _console, _ansi_ready)
from .ytdlp import ffmpeg_ok
from .logging_setup import debug_event


def safe_filename(url: str, n: int = 1, ext: str = ".mp4") -> str:
    name = unquote(os.path.basename(urlparse(url).path)) or f"video_{n}"
    name = re.sub(r'\.(?:mp4|webm|mkv|m3u8|mpd|ts)$', '', name, flags=re.I) or f"video_{n}"
    out_dir = config.dir_for(ext.lstrip("."))
    return os.path.join(out_dir, f"{n:02d}_{name}{ext}")


# ── Resume bookkeeping (raw-HTTP fallback path only) ────────────────────────
# A single-stream download's own byte offset IS the state — no fragment
# index to track like yt-dlp's multi-fragment case. The only thing worth
# persisting alongside the .part file is *what it's a partial download of*
# (url + validator), so a leftover .part from a killed process doesn't get
# blindly appended to if the source changed or it's actually a stale file
# from something else entirely.

def _resume_state_path(tmp: str) -> str:
    return tmp + ".resume.json"


def _load_resume_state(tmp: str, url: str) -> int:
    """Returns the byte offset to resume from, or 0 if there's nothing
    valid to resume (no partial file, no/mismatched state, corrupt state)."""
    state_path = _resume_state_path(tmp)
    if not (os.path.exists(tmp) and os.path.exists(state_path)):
        return 0
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return 0
    if state.get("url") != url:
        return 0
    return os.path.getsize(tmp)


def _save_resume_state(tmp: str, url: str, etag: str, last_modified: str) -> None:
    try:
        with open(_resume_state_path(tmp), "w", encoding="utf-8") as f:
            json.dump({"url": url, "etag": etag, "last_modified": last_modified}, f)
    except Exception:
        pass  # best-effort — losing the sidecar just means no resume next time


def _clear_resume_state(tmp: str) -> None:
    for p in (tmp, _resume_state_path(tmp)):
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


def download_file(url: str, out_path: str, referer: str,
                   cf_session: dict | None = None) -> str:
    """cf_session — pre-cleared CF cookies (from get_cf_session), so a direct
    ffmpeg/HTTP download rides the same cleared session instead of hitting
    the challenge cold."""
    if os.path.exists(out_path):
        return f"SKIP (exists): {out_path}"

    if ffmpeg_ok():
        tmp = out_path + ".part.mp4"
        cmd = ["ffmpeg", "-y", "-headers", ffmpeg_hdr_block(referer, cf_session=cf_session),
               "-i", url, "-c", "copy", tmp]
        # best-effort size estimate for the bar (HEAD may fail/lie for
        # m3u8/token-CDN sources — falls through to time-based bar instead)
        est_total = 0
        try:
            head = raw_get(url, headers=cdn_headers(referer, cf_session=cf_session), stream=True, timeout=10)
            est_total = int(head.headers.get("Content-Length", 0) or 0)
        except Exception as e:
            debug_event(stage="content_length_probe", error=str(e))

        duration = 0.0 if est_total > 0 else probe_duration(url, referer)
        ansi = _ansi_ready()
        use_bytes_bar = ansi and est_total > 0
        use_time_bar = ansi and not use_bytes_bar and duration > 0
        # Neither a byte total nor a probeable duration (common for
        # tokenized CDN / m3u8 sources) — still show a bar, just an
        # indeterminate one, so the screen is never silently blank
        # while a download is actually happening.
        use_indeterminate_bar = ansi and not use_bytes_bar and not use_time_bar

        cmd_bar = cmd
        if use_time_bar or use_indeterminate_bar:
            # -progress pipe:1 emits key=value lines (out_time_ms=...) we
            # parse each tick; -nostats silences ffmpeg's own status spam
            cmd_bar = ["ffmpeg", "-y", "-headers", ffmpeg_hdr_block(referer, cf_session=cf_session),
                       "-i", url, "-c", "copy", "-progress", "pipe:1", "-nostats", tmp]

        try:
            from rich.live import Live
            from rich.text import Text
            import queue as _q

            stream_progress = use_time_bar or use_indeterminate_bar
            stdout_pipe = subprocess.PIPE if stream_progress else subprocess.DEVNULL
            proc = subprocess.Popen(cmd_bar, stdout=stdout_pipe, stderr=subprocess.PIPE,
                                    text=stream_progress)
            t0 = time.time()
            out_time_sec = 0.0
            timed_out = False
            stderr_chunks = []

            def _tick_line() -> str:
                if use_bytes_bar and os.path.exists(tmp):
                    return render_progress_bar(os.path.getsize(tmp), est_total, elapsed=time.time() - t0)
                if use_time_bar:
                    return render_time_progress_bar(out_time_sec, duration, elapsed=time.time() - t0)
                return render_indeterminate_bar(elapsed=time.time() - t0, label="downloading...")

            def _frame() -> Text:
                elapsed = time.time() - t0
                return Text.from_ansi("\n".join(render_mascot_frame(elapsed) + [_tick_line()]))

            if stream_progress:
                from .ui import PipeReader
                reader = PipeReader(proc.stdout)
                with Live(_frame(), console=_console, refresh_per_second=12, transient=True) as live:
                    while True:
                        try:
                            line = reader.queue.get(timeout=0.08)
                            if line.startswith("out_time_ms="):
                                try:
                                    out_time_sec = int(line.split("=", 1)[1]) / 1_000_000
                                except ValueError:
                                    pass
                        except _q.Empty:
                            pass
                        if time.time() - t0 > FFMPEG_TIMEOUT:
                            timed_out = True
                            break
                        live.update(_frame())
                        if reader.is_done():
                            break
                if timed_out:
                    proc.kill()
                    proc.wait()
                    if os.path.exists(tmp): os.remove(tmp)
                    print_mascot_fail()
                    return f"TIMEOUT (ffmpeg > {FFMPEG_TIMEOUT}s)"
                proc.wait()
            else:
                # Byte-count bar: polls growing tmp file, no stdout pipe needed
                with Live(_frame(), console=_console, refresh_per_second=12, transient=True) as live:
                    while proc.poll() is None:
                        if time.time() - t0 > FFMPEG_TIMEOUT:
                            timed_out = True
                            break
                        live.update(_frame())
                        time.sleep(0.08)
                if timed_out:
                    proc.kill()
                    proc.wait()
                    if os.path.exists(tmp): os.remove(tmp)
                    print_mascot_fail()
                    return f"TIMEOUT (ffmpeg > {FFMPEG_TIMEOUT}s)"

            stderr_chunks.append(proc.stderr.read() if proc.stderr else "")
            r_returncode = proc.returncode
            r_stderr = "".join(stderr_chunks) if stream_progress else b"".join(
                c if isinstance(c, bytes) else c.encode() for c in stderr_chunks)
            if r_returncode == 0 and os.path.exists(tmp):
                size = os.path.getsize(tmp)
                if size >= MIN_MB * 1024 * 1024:
                    os.replace(tmp, out_path)
                    _clear_resume_state(out_path + ".part")  # stale raw-HTTP partial, if any
                    print_mascot_success()
                    return f"SAVED via ffmpeg: {out_path} ({size/1024/1024:.1f} MB)"
                os.remove(tmp)
                print_mascot_fail()
                return f"TOO SMALL ({size/1024/1024:.2f} MB)"
            err_text = r_stderr.decode(errors="replace") if isinstance(r_stderr, bytes) else r_stderr
            err = err_text.strip().splitlines()[-3:]
            print(f"ffmpeg failed, falling back: {' | '.join(err)}")
        except Exception as e:
            if os.path.exists(tmp): os.remove(tmp)
            print(f"ffmpeg errored, falling back: {e}")

    base_headers = cdn_headers(referer, cf_session=cf_session)
    tmp = out_path + ".part"
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            import threading
            from rich.live import Live
            from rich.text import Text

            # Resume offset comes from the .part file's own size — that IS
            # the state for a single-stream download. The sidecar json just
            # confirms this leftover .part actually belongs to this url
            # before we trust it (survives across separate runs, not just
            # retries within this one).
            resume_from = _load_resume_state(tmp, url)
            req_headers = dict(base_headers)
            if resume_from > 0:
                req_headers["Range"] = f"bytes={resume_from}-"

            resp = raw_get(url, headers=req_headers, stream=True, timeout=STREAM_TIMEOUT)

            if resp.status_code == 416:
                # Our saved offset doesn't line up with what the server has
                # any more (source changed size, etc). Drop the stale
                # partial + state and restart this attempt from scratch.
                resp.close()
                _clear_resume_state(tmp)
                resume_from = 0
                resp = raw_get(url, headers=base_headers, stream=True, timeout=STREAM_TIMEOUT)

            resp.raise_for_status()
            resumed = resume_from > 0 and resp.status_code == 206
            if resume_from > 0 and not resumed:
                # We asked for a Range; server ignored it and sent a fresh
                # 200 + full body instead. Can't append on top of bytes we
                # already have without duplicating data, so start over.
                resume_from = 0

            content_len = int(resp.headers.get("Content-Length", 0) or 0)
            # On a 206, Content-Length is the *remaining* bytes, not the
            # total — add back what's already on disk for an accurate bar.
            total_hdr = (resume_from + content_len) if resumed else content_len
            _save_resume_state(tmp, url, resp.headers.get("ETag", ""),
                               resp.headers.get("Last-Modified", ""))
            t0 = time.time()
            bar_live = _ansi_ready() and total_hdr > 0

            # Writing happens on its own thread so the render loop below never
            # waits on network I/O — it only reads `state`, on its own clock,
            # the same way every other Live loop in this file already does.
            # (This is the fix for the mascot/bar freezing between chunks:
            # redraws used to happen only when iter_content() yielded data.)
            state = {"size": resume_from, "done": False, "error": None}

            def _writer() -> None:
                try:
                    with open(tmp, "ab" if resumed else "wb") as f:
                        for chunk in resp.iter_content(1024 * 1024):
                            f.write(chunk)
                            state["size"] += len(chunk)
                except Exception as e:
                    state["error"] = e
                finally:
                    state["done"] = True

            def _frame_http() -> Text:
                elapsed = time.time() - t0
                bar = (render_progress_bar(state["size"], total_hdr, elapsed=elapsed)
                       if bar_live else
                       render_indeterminate_bar(elapsed=elapsed, label="downloading..."))
                return Text.from_ansi("\n".join(render_mascot_frame(elapsed) + [bar]))

            writer = threading.Thread(target=_writer, daemon=True)
            with Live(_frame_http(), console=_console, refresh_per_second=12, transient=True) as live:
                writer.start()
                while not state["done"]:
                    live.update(_frame_http())
                    time.sleep(0.08)
                writer.join(timeout=2)
                live.update(_frame_http())

            if state["error"] is not None:
                raise state["error"]
            size = state["size"]
            os.replace(tmp, out_path)
            _clear_resume_state(tmp)
            if size < MIN_MB * 1024 * 1024:
                os.remove(out_path)
                print_mascot_fail()
                return f"TOO SMALL ({size/1024/1024:.2f} MB)"
            print_mascot_success()
            verb = "RESUMED" if resumed else "SAVED"
            return f"{verb}: {out_path} ({size/1024/1024:.1f} MB)"
        except Exception as e:
            last_err = e
            # Deliberately NOT deleting tmp here (unlike before): keeping the
            # .part + its .resume.json sidecar is what lets the *next*
            # attempt — or a later run of the whole script, if this process
            # gets killed entirely — pick up from this offset instead of
            # starting over at byte zero.
            if attempt < MAX_RETRIES:
                wait = 2 ** attempt
                print(f"retry {attempt} ({e}), waiting {wait}s...")
                time.sleep(wait)
    print_mascot_fail()
    return f"FAILED: {last_err}"
