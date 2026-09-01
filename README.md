# scrape

> Paste a URL, pick a format, get the file. A layered downloader for video/audio pages that regular downloaders choke on.

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](https://opensource.org/licenses/MIT)

## Why

Most downloaders are slow, break on certain sites, or just give up. `scrape` tries the cheapest extraction method first (direct HTTP) and escalates through heavier ones (browser + Cloudflare handling, iframe scanning, network interception, yt-dlp) only when it needs to.

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
scrape                              # interactive(better to use this way)
scrape https://example.com/video    # direct
scrape https://example.com/video -f mp3
```

## Tested and working

YouTube (video, Shorts, playlists), X/Twitter, Reddit, Vimeo, Dailymotion, Twitch, TikTok, Instagram Reels, Spotify (audio). Anything else yt-dlp supports works through the fallback layer even without dedicated handling.

## Features

* Layered fallback: direct HTTP → browser/Cloudflare → iframe scan → network interception → yt-dlp
* Portrait and landscape video, HLS/DASH, separate audio/video streams
* Tokenized/signed CDN URL handling
* MP4, MP3, and other output formats via ffmpeg
* CLI + interactive mode, debug logging

## Known gaps

Login-gated streams, some JS-only "click play to reveal URL" sites, resuming interrupted downloads, live HLS, and non-English bot walls aren't fully handled yet.

## Roadmap

Standalone `.exe` → GUI (same backend, new frontend) → batch downloads → better retry/resume → CI.

## Contributing

Personal project, but issues, ideas, and PRs are welcome. Found a site that breaks it? Open an issue with what happened.

## License

MIT. Made this to be accessible, not to gatekeep it — free to fork, free to build on. Anything legal that follows from what you do with it is on you, not me. Provided as is, no warranty.
