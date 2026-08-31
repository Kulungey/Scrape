# scrape

A layered media extraction and downloading tool for difficult video pages.

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](https://opensource.org/licenses/MIT)

Paste a URL, choose a format, and let `scrape` handle the rest.

`scrape` started as a personal project after getting tired of downloaders that were either painfully slow, unreliable, or simply unable to download certain websites. Instead of constantly looking for another downloader that might work, I decided to build one myself.

The idea is simple: give it a URL, try the easiest and most reliable method first, and keep going when that method fails.

The project is still evolving, but the goal is to turn it into a dependable tool that I can use myself and eventually share with anyone who wants to run, modify, or build on it.

## How it works

`scrape` uses a layered approach rather than relying on a single extraction method.

```text
URL
↓
Platform detection
↓
Direct HTTP
↓
Browser / Cloudflare
↓
HTML + iframe extraction
↓
Network interception
↓
yt-dlp fallback
↓
ffmpeg
↓
Download
```

YouTube and X are routed directly through `yt-dlp`. Other sites are given progressively heavier extraction methods, with a real Chrome session available for pages that require JavaScript or Cloudflare handling.

The goal is not to force every website through the same method. It's to find a method that works.

## What it currently handles

The extraction layer currently looks for:

* `mp4`, `m3u8`, `mpd`, `webm`, `ts`, and `mp3` URLs in static HTML
* iframe player chains, including recursive re-scanning
* base64 encoded media URLs inside player pages
* `data-id` player parameters using `vid=`, `s=`, and `i=` keys
* token-bound CDN URLs, which are routed to browser interception
* media URLs exposed through browser network requests, matched by URL pattern or, for tokenized/extensionless CDN URLs, by the response's actual Content-Type
* HLS streams with separate audio and video tracks
* portrait/vertical video (reels, shorts, and similar formats), with quality selection mapped to the correct dimension instead of assuming landscape
* yt-dlp as a fallback for its large extractor library
* DNS failures with preflight checks and fallback handling
* browser TLS impersonation through yt-dlp where required

Known platforms with dedicated handling include:

* YouTube
* Twitter / X
* Vimeo
* Dailymotion
* Reddit
* TikTok
* Twitch
* Spotify (audio, via spotdl)

For sites supported by yt-dlp, `scrape` can fall back to its extractors through the `ytdlp_probe` and `ytdlp_download` layers. That includes sites like Instagram, which don't get a dedicated shortcut but still work through this path.

## Install

```bash
git clone https://github.com/Kulungey/Scrape.git
cd Scrape
pip install -r requirements.txt
pip install -e .
```
Docker / Cloudflare solver

scrape can use an external browser solver for Cloudflare-protected pages. The solver runs separately from the Python application, so Docker is not required for normal downloads.

On Windows, install Docker Desktop first.

Official Docker Desktop installation instructions:

https://docs.docker.com/desktop/setup/install/windows-install/

After Docker Desktop is installed and running, start the Byparr solver:

docker run -d `
  --name byparr `
  -p 8191:8191 `
  --restart unless-stopped `
  ghcr.io/thephaseless/byparr:latest

The solver will then be available locally at:

http://localhost:8191

Verify that the container is running:

docker ps

If the container is running, scrape can use the local solver service for its Cloudflare fallback.

To stop it:

docker stop byparr

To start it again later:

docker start byparr

To remove the container completely:

docker rm -f byparr

Docker is not required for YouTube, X, Spotify, normal direct extraction, or the regular browser/yt-dlp fallback chain. It is an additional dependency for the external Cloudflare-solving path.

Cloudflare support is best-effort. A solver being available does not guarantee that every Cloudflare challenge will be bypassed, and websites can change their protection at any time.

Requires Python 3.10+, ffmpeg, and Chrome for browser based extraction.

## Usage

Run without arguments for the interactive experience:

```bash
scrape
```

Or give it a URL directly:

```bash
scrape https://example.com/video
```

You can also use the original Python entry point:

```bash
python scraper.py https://example.com/video
```

Choose a format:

```bash
scrape https://example.com/video -f mp4
scrape https://example.com/video -f mp3
```

Choose an output directory:

```bash
scrape https://example.com/video --output downloads
```

For troubleshooting:

```bash
scrape https://example.com/video --debug
```

Run `scrape --help` for all available options.

## Features

* Layered media extraction
* YouTube, X, Vimeo, Dailymotion, Reddit, TikTok, Twitch, and Spotify support
* Direct HTTP extraction
* HTML and iframe extraction
* Recursive iframe player scanning
* Base64 encoded URL detection
* Player parameter extraction
* HLS and DASH manifest detection
* Separate audio and video handling
* Portrait/vertical video quality selection (reels, shorts, and similar)
* Cloudflare handling through a real Chrome session
* Browser network interception, with an idle cutoff so it stops early once the page goes quiet instead of always waiting out the full timeout
* Content-Type based media matching for tokenized CDN URLs that don't carry a file extension
* yt-dlp fallback
* yt-dlp TLS impersonation
* DNS preflight and fallback handling
* ffmpeg based conversion
* MP4, MP3, and other output formats
* Interactive terminal interface
* Command line interface for scripting
* Debug logging
* Tokenized CDN URLs redacted from normal logs
* Pluggable extraction chain
* Automated tests for core logic

## Known gaps

`scrape` now covers the main extraction paths and the currently tested platforms, but there are still several engineering gaps.

* HLS behind a login wall does not currently pass cookies into the extractor layer
* DASH manifests requiring authentication headers have the same limitation
* Some sites expose the media URL only after an actual play button is clicked
* The main flow does not currently simulate play clicks before waiting for network interception
* Interrupted large file downloads resume from zero rather than continuing from the partial file
* Non-English bot walls are not currently detected by the browser block check
* Token-bound CDN detection currently covers specific token formats rather than every possible signed CDN URL
* The `original` format flag still needs proper handling for formats that do not map directly to an ffmpeg container
* MPD / DASH manifests can be detected and classified but are not yet handled as cleanly as HLS
* Authentication and cookie handling still need to be expanded
* Live HLS has not yet been fully validated
* Master playlist handling needs more real-world testing

These are known engineering gaps, not promises of permanent site support.

## Project layout

```text
scrape/
├── main.py
├── cli.py
├── config.py
├── media.py
├── patterns.py
├── extractors.py
├── browser.py
├── ytdlp.py
├── downloader.py
├── pipeline.py
├── ui.py
└── logging_setup.py
```

The project is deliberately split into separate layers so the extraction logic, browser handling, downloading, command line interface, and user interface can evolve independently.

**Finished 8-29-2026**

The core scraper architecture, extraction pipeline, platform compatibility layer, browser fallback system, yt-dlp fallback, DNS handling, TLS impersonation, and initial automated test coverage are now in place.

**Updated 8-31-2026**

A round of fixes aimed at extraction speed and portrait video support:

* Removed a leftover debug HTML dump that ran on every single search
* The browser layer now checks the page's static HTML for the media URL before waiting on network capture, so searches where the answer is already on the page finish much faster
* The network listener now cuts off early once traffic goes quiet instead of always waiting the full timeout
* Added mp3 to the patterns the extractor and browser layer look for
* Added a Content-Type based fallback for CDN URLs that don't have a file extension in the URL itself
* Fixed quality selection for portrait/vertical video (reels, shorts, and similar). Quality tiers were only ever checked against height, which works for landscape video but not portrait, where height is the long edge. This was causing "requested format not available" errors on sites like Instagram. Quality selection now falls back to matching on width when height doesn't turn anything up, so the same 1080p/720p/480p/360p tiers apply correctly regardless of orientation

The project has also been manually tested against the main supported platforms:

* YouTube
* YouTube Shorts
* YouTube playlists
* X / Twitter
* Reddit
* Vimeo
* Dailymotion
* TikTok
* Twitch
* Instagram Reels (via yt-dlp fallback, portrait quality selection confirmed working)

## Roadmap

This is an ambitious one man project, so development is intentionally gradual. The priority is to make each layer reliable before piling more features on top of it.

### Foundation

* [x] Split the original scraper into a proper package
* [x] Add shared media types
* [x] Add layered extraction pipeline
* [x] Add extractor chain
* [x] Add command line interface
* [x] Add debug logging
* [x] Redact tokenized URLs from normal logs
* [x] Add tests
* [x] Add backward compatible `scraper.py` entry point
* [x] Verify package installation and imports

### Platform Compatibility

* [x] YouTube video
* [x] YouTube Shorts
* [x] YouTube playlists
* [x] X / Twitter
* [x] Reddit
* [x] Vimeo
* [x] Dailymotion
* [x] TikTok
* [x] Twitch
* [x] Spotify (audio)
* [x] Instagram (via yt-dlp fallback)

### Extraction & Reliability

* [x] Test separate audio and video streams
* [x] Test HLS / m3u8 extraction
* [x] Add iframe extraction
* [x] Add recursive iframe scanning
* [x] Add base64 URL extraction
* [x] Add player parameter extraction
* [x] Add browser network interception
* [x] Add token-bound CDN detection
* [x] Add Direct HTTP → browser fallback
* [x] Add browser → extractor fallback
* [x] Add extractor → yt-dlp fallback
* [x] Add DNS preflight checks
* [x] Add DNS fallback handling
* [x] Add yt-dlp TLS impersonation
* [x] Add regression tests for core routing and probing
* [x] Reduce unnecessary wait time in the browser extraction layer
* [x] Add Content-Type based media matching for extensionless CDN URLs
* [x] Fix quality selection for portrait/vertical video
* [ ] Test live HLS
* [ ] Test master playlists
* [ ] Improve fallback behavior
* [ ] Improve download and retry handling
* [ ] Improve error messages
* [ ] Improve browser cleanup and failure handling
* [ ] Improve authentication and cookie handling
* [ ] Add range-based resume for interrupted downloads
* [ ] Improve DASH / MPD handling
* [ ] Improve bot-wall detection
* [ ] Expand signed CDN detection
* [ ] Improve `original` format handling

### Open Source Polish

* [ ] Add continuous integration
* [ ] Improve documentation
* [ ] Add contribution guidelines
* [ ] Improve examples
* [ ] Establish a release workflow
* [ ] Add automated compatibility testing where practical

### Standalone Builds

* [ ] Build a Windows `.exe`
* [ ] Test packaged builds on clean systems
* [ ] Make runtime dependencies clear
* [ ] Automate release builds

### GUI

* [ ] Build a graphical frontend around the existing pipeline
* [ ] URL input
* [ ] Format and quality selection
* [ ] Output directory selection
* [ ] Download progress
* [ ] Cancellation
* [ ] Clear error and status reporting
* [ ] Package the GUI for easy use

The GUI is intended to be a frontend for the existing backend rather than a separate downloader. The same extraction and download pipeline should power both interfaces.

### Batch & Quality

* [ ] Batch downloads
* [ ] Download queue
* [ ] Per-download status and error handling
* [ ] Format and quality probing
* [ ] Resolution selection
* [ ] Audio/video quality selection
* [ ] Better automatic format selection

### Further Ideas

* [ ] More extraction patterns
* [ ] More site support
* [ ] Concurrent downloads where useful
* [ ] Persistent configuration
* [ ] Additional quality of life features
* [ ] Plugin-based extractors
* [ ] Better live-stream support

Features will be added when they solve an actual problem rather than just to make the project bigger.

## Philosophy

`scrape` is built around a fairly simple idea:

**If one method can't get the media, try another.**

A website might expose a direct video URL. Another might hide it behind an iframe. Another might only reveal it after JavaScript runs. Another might require a browser session entirely.

Instead of treating those cases as completely different applications, `scrape` tries to give them a common pipeline.

I built this because I wanted a downloader I could actually rely on, without paying for something locked down or fighting with ad-riddled sites that barely worked half the time. It's free, it's open, and it's meant to be accessible to anyone who wants to use it, fork it, or build something different on top of it.

If it becomes useful to other people too, even better.

## Real World Testing

Manual testing against real pages. Results can change as websites update their players or protection.

`[x]` passed · `[!]` tested but currently fails · `[ ]` not tested

### Platforms

* [x] YouTube video
* [x] YouTube Shorts
* [x] X / Twitter video
* [x] YouTube playlist
* [x] Reddit
* [x] Vimeo
* [x] Dailymotion
* [x] Twitch
* [x] TikTok
* [x] Instagram Reels (yt-dlp fallback)

### Media Sources

* [x] Direct MP4
* [x] M3U8 / HLS
* [x] Portrait / vertical video (reels, shorts)
* [ ] MPD / DASH
* [x] Separate audio / video
* [x] Media URL in HTML / JSON
* [x] iframe player
* [x] Base64 encoded player URL
* [x] Extensionless CDN URL matched by Content-Type
* [ ] JavaScript generated URL requiring a play click
* [ ] Relative media paths in inline JavaScript
* [ ] `blob:` URLs

### Browser & Fallbacks

* [x] Cloudflare protected page*-> requires docker quite a hassle but have implemented feature to open and close after each sessions so just install docker thats all
* [x] Network interception
* [x] Tokenized CDN URL detection
* [x] Direct HTTP → browser fallback
* [x] Browser → extractor fallback
* [x] Extractor → yt-dlp fallback
* [x] DNS preflight / fallback
* [x] yt-dlp TLS impersonation
* [ ] Non-English bot wall detection

### Downloads

* [x] MP4
* [x] MP3
* [x] MKV / WebM
* [x] Original format
* [x] M3U8 → ffmpeg
* [ ] MPD → ffmpeg / yt-dlp
* [x] Large file / interrupted download
* [ ] Live HLS

> These are manual compatibility tests, not guarantees of permanent site support.

## What's Next

The core extraction system is now in place and the last round of fixes cleaned up speed and portrait video handling. The next stage is about turning `scrape` from a capable developer tool into something that can be distributed and used comfortably by other people.

The immediate priorities are:

1. Standalone Windows `.exe`
2. GUI frontend
3. Batch downloading
4. Format and quality probing
5. Better download reliability
6. DASH and authentication improvements
7. Automated releases and CI

The GUI will use the existing backend rather than replacing it. The goal is to keep the extraction pipeline independent from whatever interface sits on top of it.

For now, let me cook. One problem at a time, one improvement at a time.

## Contributing

The project is currently developed as a personal project, but contributions, ideas, bug reports, and improvements are welcome.

If you find a website that `scrape` cannot handle, an issue describing what happened and how the page behaves is especially useful.

## License

Released under the MIT License. You are free to use, modify, and distribute this project for your own purposes.

I made this to be accessible, not to gatekeep it. It's free to fork, free to build on, and free to use however you want. Any legal responsibility for what you do with it is yours, not mine.

Use it responsibly. The software is provided as is, and I am not responsible for any damage, data loss, legal issues, or other consequences resulting from its use or modification.
