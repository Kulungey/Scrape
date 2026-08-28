# scraper

Video downloader with Cloudflare bypass. Paste a URL, pick a format, get the file.

YouTube routes directly to yt-dlp. Everything else goes through a 4-layer extraction stack: direct HTTP fetch, real Chrome with CF bypass, HTML/iframe scanning, then yt-dlp as a last resort.

---

## Requirements

**Python 3.10 or newer**

External tools (must be on PATH):

| Tool | Purpose | Required |
|------|---------|----------|
| ffmpeg | HLS/DASH muxing, format conversion | Strongly recommended |
| Chrome | CF bypass and token-bound CDN intercept | Required for protected sites |
| yt-dlp | YouTube and generic fallback | Recommended |

Python packages:

```
pip install DrissionPage curl_cffi yt-dlp
```

Install ffmpeg:
- Windows: https://ffmpeg.org/download.html, add the bin folder to PATH
- Or via winget: `winget install ffmpeg`

---

## Install

```bash
git clone https://github.com/yourusername/scraper.git
cd scraper
pip install DrissionPage curl_cffi yt-dlp
```

No virtual environment required, but use one if you prefer.

---

## Usage

```bash
python scraper.py https://example.com/video
```

Or run without arguments and paste the URL when prompted:

```bash
python scraper.py
```

You will then be asked for an output format:

```
Output format:
  1. mp4
  2. mp3
  3. mkv
  4. webm
  5. original  <- keeps original container/quality
Choice [1]:
```

Press Enter for mp4. Type a number or a custom extension (flac, opus, avi, etc).

Output lands in `./videos/`.

---

## How it works

**YouTube / Shorts / Live** — detected by URL, handed straight to yt-dlp with best quality up to 1080p merged to the chosen format. No browser, no scraping.

**Everything else** runs through four layers in order:

1. Direct HTTP fetch via curl_cffi (Chrome TLS fingerprint)
2. Real Chrome via DrissionPage if step 1 hits a 403 or CF challenge
3. HTML scan for media URLs, iframe player fetch, base64 decode
4. yt-dlp generic fallback

If a token-bound CDN URL is detected (pipe-signature pattern), the tool opens the player in Chrome, intercepts the live CDN request, then downloads with ffmpeg.

---

## Output formats

When you pick mp4, mkv, or webm: ffmpeg remuxes the stream into that container.

When you pick mp3, aac, flac, opus, m4a: audio is extracted, video discarded.

When you pick original: downloaded as-is, no remux.

Custom extensions work too: type `avi`, `mov`, `ts`, whatever ffmpeg supports.

---

## Config

All tunable constants are at the top of the file:

```python
OUTPUT_DIR     = "videos"    # output folder
MAX_RETRIES    = 3           # retry count on direct download failures
MIN_MB         = 2           # files smaller than this are rejected
YTDLP_TIMEOUT  = 3600        # max seconds for yt-dlp (1 hour)
FFMPEG_TIMEOUT = 3600        # max seconds for ffmpeg
STREAM_TIMEOUT = 30          # per-chunk connect/read timeout
```

---

## Planned

- GUI with queue, progress bar, output folder picker
- 4K / quality selector flag
- Batch mode: read URLs from a text file
- YouTube playlist support
- Resume support via Range header
- `--dry-run` flag
- Structured log file per session
- Twitter/X dedicated path (currently works via intercept)
- Instagram Reels
- Bilibili with cookie injection

---

## Repo setup (first time)

Create a new repo on GitHub with no README, no gitignore, no license (you will add these yourself).

Then in your project folder:

```bash
git init
git add scraper.py README.md .gitignore
git commit -m "init"
git branch -M main
git remote add origin https://github.com/yourusername/scraper.git
git push -u origin main
```

Suggested `.gitignore`:

```
videos/
__pycache__/
*.pyc
*.part
*.part.mp4
.env
```

For future changes:

```bash
git add scraper.py
git commit -m "what you changed"
git push
```

---

## License

MIT
