# scraper.py — video downloader with Cloudflare bypass

A single-file CLI tool that pulls video files down from a page URL, falling
through a chain of strategies until one works — direct fetch, real-browser
Cloudflare bypass, HTML/iframe scraping, or a yt-dlp fallback. YouTube links
skip straight to yt-dlp.

## How it works

Requests are tried in order, stopping at the first success:

1. **Direct fetch** — `curl_cffi` (or `requests` if that's unavailable) with a
   Chrome TLS fingerprint, no browser needed.
2. **Real Chrome via DrissionPage** — launches an actual Chrome instance to
   clear Cloudflare's JS challenge and sniff the media URL off the network
   tab.
3. **HTML / iframe scan** — regex + base64 decoding over the raw page (and
   any player iframe it finds) to dig out a direct media URL.
4. **yt-dlp fallback** — handed off whenever nothing above finds a URL, the
   URL is YouTube, or the CDN URL turns out to be token-bound (short-lived
   signed URL) or in a domain-mismatch situation.

Direct downloads stream with retries; `.m3u8` sources are pulled through
`ffmpeg`. Progress renders as a live-redrawing rainbow bar in the terminal,
including through yt-dlp's silent merge/remux step and through Chrome's fully
blocking page-load/Cloudflare-bypass calls — both used to just go quiet and
look frozen; now a marquee bar keeps animating through them.

## Requirements

- **Python 3.10+** (the code uses `X | None` union-type hints, which need
  3.10 or newer)
- **ffmpeg** on `PATH` — required for `.m3u8` downloads and yt-dlp's
  audio/video mux step
  - Windows: `winget install ffmpeg`
  - macOS: `brew install ffmpeg`
  - Linux: `apt install ffmpeg`
- **Google Chrome** installed — used by DrissionPage for the browser-bypass
  layer

### Python packages

```bash
pip install -r requirements.txt
```

| Package     | Why it's needed |
|-------------|------------------|
| `curl_cffi` | Preferred HTTP backend — impersonates a real Chrome TLS fingerprint so Layer 1 isn't trivially blocked. Falls back to `requests` if not installed. |
| `DrissionPage` | Drives real Chrome for the Cloudflare-bypass / network-intercept layer (Layer 2). |
| `yt-dlp`    | Handles YouTube and the generic fallback layer (Layer 4); also used internally for `.m3u8`/DASH merges. |
| `requests`  | Fallback HTTP backend if `curl_cffi` fails to install (e.g. no prebuilt wheel for your platform). |

## Usage

```bash
python scraper.py <url>
```

Or run it with no arguments to get an interactive prompt (with an idle-logo
animation) for the URL and output format:

```bash
python scraper.py
```

Downloaded files land in `./videos/`.

## Terminal experience

The whole CLI is built around one rule: nothing animated should ever freeze
mid-way and look dead.

- **Breathing menu box** — the output-format picker has a continuously
  animating rainbow border, with the "thinking" mascot looping above it the
  whole time you're choosing.
- **Rainbow progress bars** — byte-accurate where possible, falling back to
  a time-based or marquee bar when the source doesn't report a real size.
- **Mascots** — a single happy/sad face animates through to the very end of
  the run (through the final "press any key to close" wait), instead of
  playing a couple of loops and freezing partway.
- **Browser-intercept marquee** — Chrome's page-load and Cloudflare-bypass
  steps are fully blocking with no progress hooks of their own; those now
  run on a background thread while the main thread keeps a marquee bar
  animating, so the CLI never goes silently unresponsive during a Cloudflare
  clear.

All animation is single-writer: any background thread only touches data
(subprocess pipes, the browser driver) and never the terminal directly, to
avoid the redraw races that come from two things trying to draw at once.

## Configuration

A few constants near the top of `scraper.py` control behavior:

| Constant         | Default | Purpose |
|------------------|---------|----------|
| `OUTPUT_DIR`     | `videos`| Where downloaded files are saved |
| `MAX_RETRIES`    | `3`     | Retry attempts for a failed direct download |
| `MIN_MB`         | `2`     | Minimum acceptable file size (guards against corrupt/partial downloads) |
| `YTDLP_TIMEOUT`  | `3600`  | Max seconds to let a yt-dlp subprocess run |
| `FFMPEG_TIMEOUT` | `3600`  | Max seconds to let an ffmpeg subprocess run |
| `STREAM_TIMEOUT` | `30`    | Socket timeout for streamed direct downloads |

## Known limitations / possible next steps

- **Windows-only cookie handling** — the intercept path notes that DPAPI
  cookie decryption is unreliable on Windows, so it skips cookies rather than
  looping; worth revisiting if you need authenticated sessions.
- **Chrome-only bypass** — DrissionPage is hard-wired to Chrome; no
  Firefox/WebKit fallback if Chrome isn't installed.
- **No proxy support** — neither the direct-fetch nor browser layers accept
  a proxy URL; add one if you're scraping from a blocked network.
- **No concurrent downloads** — `scrape()` handles one URL per run; batching
  a list of URLs would need a thin wrapper around it.
- **Single output filename scheme** — `safe_filename()` numbers files
  sequentially per run; a resumable/skip-if-exists mode isn't implemented.

## Code health

Checked with `pyflakes` — no unused imports, no dead functions, no unused
variables. Every top-level function is reachable from `scrape()` or the
`if __name__ == "__main__"` entry point.
