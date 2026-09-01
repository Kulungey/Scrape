# scrape

> Paste a URL, pick a format, get the file. A layered downloader built to actually get the media off pages that break most downloaders.

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](https://opensource.org/licenses/MIT)

## Why

I got tired of downloaders that either didn't work or worked once and broke the next week, so I built my own extraction pipeline from scratch: direct HTTP parsing, iframe chain scanning with recursive re-scanning, base64-encoded URL detection, a real Chrome session for Cloudflare and JS-heavy pages, and live network interception to grab the media request as it happens. That's the engine. yt-dlp only gets called in as a fallback, for the sites my own extraction genuinely can't crack on its own, not as the main path.

## Install

```bash
git clone https://github.com/Kulungey/Scrape.git
cd Scrape
pip install -r requirements.txt
pip install -e .
```

Needs Python 3.10+, ffmpeg, and Chrome. Docker is optional, only used as a last-resort Cloudflare fallback and pulled automatically the one time it's needed.

## Quick start

```bash
scrape                              # interactive, better to use it this way
scrape https://example.com/video    # direct
scrape https://example.com/video -f mp3
```

## Tested and working

YouTube (video, Shorts, playlists), X/Twitter, Reddit, Vimeo, Dailymotion, Twitch, TikTok, Instagram Reels, Spotify audio. Beyond that, yt-dlp's library covers a lot more even without dedicated handling.

## What it actually does

* Layered fallback: direct HTTP → browser/Cloudflare → recursive iframe scanning → live network interception → yt-dlp as the last resort
* Reads through obfuscation: base64-encoded URLs, packed JS, tokenized/signed CDN links
* Handles portrait and landscape video, HLS/DASH, separate audio/video streams correctly
* Real Cloudflare handling through an actual browser session, not a header spoof
* MP4, MP3, and other output formats via ffmpeg
* CLI + interactive mode, debug logging

## Known gaps

Login-gated streams, some JS-only "click play to reveal URL" sites, resuming interrupted downloads, live HLS, and non-English bot walls aren't fully handled yet.

## Roadmap

Standalone `.exe` → GUI (same backend, new frontend) → batch downloads → better retry/resume → CI.

## Contributing

Personal project, but issues, ideas, and PRs are welcome. Found a site that breaks it? Open an issue with what happened.

## License

MIT. Built this myself, and it's meant to be accessible, not gatekept. Free to fork, free to build on. Anything legal that follows from what you do with it is on you, not me. Provided as is, no warranty.
