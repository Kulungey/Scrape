"""Command-line entry point.

    scrape URL
    scrape URL -f mp3
    scrape URL --output downloads
    scrape URL --debug
    scrape URL --no-browser --no-ytdlp
    scrape                      # interactive prompt, animated banner

Kept deliberately thin — all it does is parse args, wire them into config/ui,
and hand off to pipeline.scrape().
"""

from __future__ import annotations

import argparse
import sys

from . import config
from .config import AUDIO_FMTS
from .ytdlp import _LOSSY_AUDIO_FMTS
from .dns_check import ensure_dns
from .logging_setup import configure as configure_logging
from .pipeline import scrape
from .docker_manager import stop_byparr_if_started
from .ui import (print_logo, wait_for_site_input_with_idle_logo, press_any_key_to_close,
                 print_mascot_thinking, input_with_breathing_menu, cprint)
from .ytdlp import quick_update_check

FORMAT_CHOICES = ["mp4", "mp3", "mkv", "webm", "aac", "flac", "opus", "m4a", "wav", "original"]
QUALITY_CHOICES = ["1080p", "720p", "480p", "360p"]
AUDIO_QUALITY_CHOICES = ["320kbps", "256kbps", "192kbps", "128kbps"]

# label -> short description shown next to it in the extension picker
_EXT_DESCRIPTIONS = {
    "mp4":      "most compatible, plays everywhere",
    "mkv":      "keeps multiple audio/subtitle tracks",
    "webm":     "smaller file size",
    "original": "no conversion, whatever the source gives",
}

# label -> max height cap the quality picker maps to
_QUALITY_HEIGHTS = {"1080p": 1080, "720p": 720, "480p": 480, "360p": 360}

# label -> target bitrate (kbps) the audio-quality picker maps to
_AUDIO_KBPS = {"320kbps": 320, "256kbps": 256, "192kbps": 192, "128kbps": 128}


def pick_format_interactively() -> str:
    """The breathing-menu extension picker, used only when running with no
    arguments at all (double-click / bare `scrape` interactive mode)."""
    options_raw = ["mp4", "mkv", "webm", "mp3", "original"]
    print_mascot_thinking()
    labeled = [
        f"{i}. {opt.upper():<8} {_EXT_DESCRIPTIONS[opt]}" if opt in _EXT_DESCRIPTIONS
        else f"{i}. {opt.upper():<8} audio only"
        for i, opt in enumerate(options_raw, 1)
    ]
    valid = {str(i) for i in range(1, len(options_raw) + 1)}
    raw = input_with_breathing_menu("Output format:", labeled, valid, default="1")
    idx = int(raw) - 1
    return "" if options_raw[idx] == "original" else options_raw[idx]


def pick_quality_interactively() -> int:
    """Second breathing-menu box (same style as the extension picker), shown
    for video formats. Returns the max height in pixels. 1080p is the
    ceiling — the cap is a maximum, not an exact requirement (falls back to
    the highest quality available at or below it)."""
    labeled = [f"{i}. {opt}" for i, opt in enumerate(QUALITY_CHOICES, 1)]
    valid = {str(i) for i in range(1, len(QUALITY_CHOICES) + 1)}
    raw = input_with_breathing_menu("Video quality:", labeled, valid, default="1")
    idx = int(raw) - 1
    return _QUALITY_HEIGHTS[QUALITY_CHOICES[idx]]


def pick_audio_quality_interactively() -> int:
    """Second breathing-menu box, shown instead of pick_quality_interactively
    when the selected format is audio (mp3). Returns the target bitrate in
    kbps."""
    labeled = [f"{i}. {opt}" for i, opt in enumerate(AUDIO_QUALITY_CHOICES, 1)]
    valid = {str(i) for i in range(1, len(AUDIO_QUALITY_CHOICES) + 1)}
    raw = input_with_breathing_menu("Audio quality:", labeled, valid, default="1")
    idx = int(raw) - 1
    return _AUDIO_KBPS[AUDIO_QUALITY_CHOICES[idx]]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scrape",
        description="Layered media extraction and downloading tool for difficult video pages.",
    )
    p.add_argument("url", nargs="?", help="Page URL to scrape. Omit for an interactive prompt.")
    p.add_argument("-f", "--format", dest="out_fmt", default=None, choices=FORMAT_CHOICES,
                   help="Output format (default: mp4, or an interactive picker "
                        "if run with no URL at all).")
    p.add_argument("-q", "--quality", dest="quality", default=None, choices=QUALITY_CHOICES,
                   help="Max video quality cap (default: 1080p, or an interactive "
                        "picker if run with no URL at all). Downloads the highest "
                        "quality available at or below this cap; applies to video "
                        "formats only (mp4/mkv/webm/original).")
    p.add_argument("-b", "--bitrate", dest="bitrate", default=None,
                   choices=AUDIO_QUALITY_CHOICES,
                   help="Target audio bitrate (default: 320kbps, or an interactive "
                        "picker if run with no URL at all). Applies to mp3/aac/"
                        "opus/m4a only; ignored for lossless audio (flac/wav) "
                        "and video formats.")
    p.add_argument("--output", dest="output_dir", default=None,
                   help=f"Output directory (default: {config.VIDEO_DIR}/ and "
                        f"{config.MUSIC_DIR}/ for video and audio respectively)")
    p.add_argument("--debug", action="store_true",
                   help="Verbose structured logging and full (unredacted) URLs.")
    p.add_argument("--no-browser", action="store_true",
                   help="Never launch Chrome — direct HTTP and yt-dlp only.")
    p.add_argument("--no-ytdlp", action="store_true",
                   help="Never shell out to yt-dlp.")
    p.add_argument("--no-banner", action="store_true",
                   help="Skip the animated ASCII banner.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    configure_logging(args.debug)
    if args.output_dir:
        config.set_output_dir(args.output_dir)
    config.set_allow_browser(not args.no_browser)
    config.set_allow_ytdlp(not args.no_ytdlp)

    ensure_dns()
    updated = quick_update_check()
    if updated:
        cprint("\n[update] Dependencies updated — restart scrape for changes to take effect.", 46)
        input("Press Enter to continue anyway, or Ctrl+C to restart... ")

    interactive = args.url is None
    if args.url:
        if not args.no_banner:
            print_logo()
        site = args.url
    else:
        site = wait_for_site_input_with_idle_logo()

    if not site:
        print("No URL.", file=sys.stderr)
        return 1

    if args.out_fmt is not None:
        out_fmt = args.out_fmt
    elif interactive:
        out_fmt = pick_format_interactively()
    else:
        out_fmt = "mp4"
    out_fmt = "" if out_fmt == "original" else out_fmt

    # Quality selection happens right after format selection, and shows a
    # different menu depending on whether the format is video or audio.
    # ORIGINAL (out_fmt == "") keeps the source as-is, so no quality menu.
    if out_fmt and out_fmt in AUDIO_FMTS:
        # Bitrate cap only means anything for lossy codecs — lossless
        # (flac/wav) always downloads best, matching existing behavior.
        if out_fmt in _LOSSY_AUDIO_FMTS:
            if args.bitrate is not None:
                config.set_audio_bitrate(_AUDIO_KBPS[args.bitrate])
            elif interactive:
                config.set_audio_bitrate(pick_audio_quality_interactively())
            else:
                config.set_audio_bitrate(_AUDIO_KBPS["320kbps"])
    elif out_fmt:
        if args.quality is not None:
            config.set_max_height(_QUALITY_HEIGHTS[args.quality])
        elif interactive:
            config.set_max_height(pick_quality_interactively())
        else:
            config.set_max_height(_QUALITY_HEIGHTS["1080p"])

    exit_code = 0
    try:
        scrape(site, out_fmt)
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        stop_byparr_if_started()
        press_any_key_to_close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
