# scrape

Video downloader with Cloudflare bypass. Paste a link, pick a format, done.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **YouTube & Twitter/X** — routed straight to yt-dlp, no scraping needed
- **Cloudflare-protected sites** — real Chrome via DrissionPage handles the JS challenge
- **Token-bound CDN URLs** — browser network interception captures the live stream URL
- **Auto-update** — checks yt-dlp on startup, updates silently if stale
- **Rainbow progress bar** — because why not
- **ffmpeg post-processing** — download in whatever format yt-dlp gives, convert to what you asked for

---

## Requirements

### Python packages

```
pip install -r requirements.txt
```

| Package | Purpose |
|---|---|
| `yt-dlp` | YouTube, Twitter/X, and generic video extraction |
| `curl_cffi` | Chrome TLS fingerprint for Cloudflare bypass |
| `DrissionPage` | Real Chrome automation for JS-heavy sites |

### System dependencies

| Tool | Install |
|---|---|
| **Python 3.10+** | [python.org](https://python.org) |
| **ffmpeg** | `winget install ffmpeg` (Windows) · `brew install ffmpeg` (Mac) · `apt install ffmpeg` (Linux) |
| **Chrome** | Must be installed — DrissionPage drives it |

---

## Install

```bash
git clone https://github.com/yourname/scrape
cd scrape
pip install -r requirements.txt
```

---

## Usage

**Double-click** `scraper.py` or run from terminal:

```bash
python scraper.py
```

Paste a URL when prompted, pick output format (mp4 / mp3 / mkv / webm / original), wait.

You can also pass the URL as an argument:

```bash
python scraper.py https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

Output lands in a `videos/` folder next to the script.

---

## How it works

Sites go through layers in order, stopping at the first success:

```
URL
 │
 ├─ YouTube / Twitter?  ──► yt-dlp (native extractor)
 │
 ├─ [1] curl_cffi direct fetch  (Chrome TLS fingerprint)
 ├─ [2] Real Chrome + Cloudflare bypass  (DrissionPage)
 ├─ [3] HTML / iframe scan + base64 decode
 └─ [4] Browser network interception → CDN URL → ffmpeg/yt-dlp
```

---

## YouTube & 403 errors

YouTube enforces Proof of Origin (PO) tokens on stream downloads. If you hit a 403:

1. The script tries plain yt-dlp first (works for most videos)
2. Falls back to Edge → Chrome → Firefox cookies automatically
3. Make sure you're **logged into YouTube** in at least one browser

Keeping yt-dlp up to date (handled automatically on startup) is usually enough.

---

## Notes

- Downloads are saved to `videos/` — created automatically if it doesn't exist
- Existing files are skipped (no re-download)
- ffmpeg is optional but strongly recommended — without it format conversion is limited
