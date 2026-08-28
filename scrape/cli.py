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
from .logging_setup import configure as configure_logging
from .pipeline import scrape
from .ui import (print_logo, wait_for_site_input_with_idle_logo, press_any_key_to_close,
                 print_mascot_thinking, input_with_breathing_menu)
from .ytdlp import quick_update_check

FORMAT_CHOICES = ["mp4", "mp3", "mkv", "webm", "aac", "flac", "opus", "m4a", "wav", "original"]


def pick_format_interactively() -> str:
    """The breathing-menu format picker, used only when running with no
    arguments at all (double-click / bare `scrape` interactive mode)."""
    options_raw = ["mp4", "mp3", "mkv", "webm", "original"]
    print_mascot_thinking()
    labeled = [f"{i}. {opt}" for i, opt in enumerate(options_raw, 1)]
    valid = {str(i) for i in range(1, len(options_raw) + 1)}
    raw = input_with_breathing_menu("Output format:", labeled, valid, default="1")
    idx = int(raw) - 1
    return "" if options_raw[idx] == "original" else options_raw[idx]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scrape",
        description="Layered media extraction and downloading tool for difficult video pages.",
    )
    p.add_argument("url", nargs="?", help="Page URL to scrape. Omit for an interactive prompt.")
    p.add_argument("-f", "--format", dest="out_fmt", default=None, choices=FORMAT_CHOICES,
                   help="Output format (default: mp4, or an interactive picker "
                        "if run with no URL at all).")
    p.add_argument("--output", dest="output_dir", default=None,
                   help=f"Output directory (default: {config.OUTPUT_DIR})")
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

    quick_update_check()

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

    exit_code = 0
    try:
        scrape(site, out_fmt)
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        if interactive:  # matches the original script's double-click UX
            press_any_key_to_close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
