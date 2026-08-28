# scrape

A layered media extraction and downloading tool for difficult video pages.

Not just "video downloader with Cloudflare bypass" — it walks a pipeline of
progressively heavier techniques, using the cheapest one that works and
falling through to the next when it doesn't:

```
URL
 ↓
Platform detection      (YouTube / X.com → straight to yt-dlp)
 ↓
Direct HTTP              (curl_cffi with a Chrome TLS fingerprint)
 ↓
Browser / Cloudflare     (real Chrome via DrissionPage, CF challenge solving)
 ↓
HTML + iframe extraction (extractor chain: direct match, then player iframe)
 ↓
Network interception     (watch the browser's own traffic for the CDN URL)
 ↓
yt-dlp                   (generic extractor as a last resort)
 ↓
ffmpeg
 ↓
Download
```

## Install

```
pip install -e .
```

Requires ffmpeg on PATH and Chrome installed for the browser layers.

## Usage

```
scrape URL
scrape URL -f mp3
scrape URL --output downloads
scrape URL --debug
scrape URL --no-browser --no-ytdlp
scrape                       # no args: interactive prompt with animated banner
```

Run `scrape --help` for the full flag list.

Normal-mode logs redact CDN URLs down to just the host, since tokenized
CDN links can carry authentication material in the query string. Pass
`--debug` to see full URLs and structured `key=value` diagnostic lines.

## Layout

```
scrape/
├── main.py          entry point
├── cli.py           argparse CLI
├── config.py        constants, headers, HTTP backend selection
├── media.py         MediaKind / MediaResult — shared result types
├── patterns.py       compiled regexes
├── extractors.py     pure HTML/player extraction + pluggable extractor chain
├── browser.py        Chrome/DrissionPage, Cloudflare bypass, interception
├── ytdlp.py          yt-dlp integration, platform detection, tool checks
├── downloader.py      direct download with ffmpeg-first strategy
├── pipeline.py        orchestrates the layers above
├── ui.py              banner, mascot, prompts, progress bars, colored logs
└── logging_setup.py   normal vs --debug logging modes
```

### Adding support for a new extraction pattern

Add a class to `extractors.py` implementing `extract(html, base_url) ->
MediaResult | None`, and append it to `DEFAULT_CHAIN`. The pipeline walks
the chain and uses the first non-`None` result — no changes needed to
`pipeline.py` itself.

### Tests

```
pytest
```

Tests cover the pure functions only (`extractors.py`, `media.py`,
`safe_filename`) — feed them fake HTML/URLs rather than hitting live sites.
