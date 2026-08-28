# scrape

A lightweight video downloader for sites that are difficult to extract from directly.

Paste a URL, choose a format, and let `scrape` handle the rest. It uses `yt-dlp` where possible, a real Chrome session for Cloudflare protected sites, and browser network interception when the video URL is only exposed after the page loads.


![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-26%20passing-brightgreen)


## Features

* **YouTube and Twitter/X support** through `yt-dlp`
* **Cloudflare handling** using a real Chrome browser controlled by DrissionPage
* **Token bound CDN detection** through browser network interception
* **Multiple extraction methods** with automatic fallback
* **Automatic yt-dlp updates** when a newer version is available
* **Format conversion** through FFmpeg
* **MP4, MP3, MKV, WebM, and original format output**
* **Existing downloads are skipped**
* **Terminal progress display** with a rainbow progress bar

## How it works

`scrape` does not rely on a single extraction method. It moves through several layers and stops when one successfully finds the media.

```text
URL
 |
 +-- YouTube / Twitter/X
 |       |
 |       +--> yt-dlp
 |
 +-- Direct request
 |       |
 |       +--> curl_cffi
 |
 +-- Cloudflare protected page
 |       |
 |       +--> Chrome + DrissionPage
 |
 +-- Page inspection
 |       |
 |       +--> HTML / iframe / base64 extraction
 |
 +-- Browser network interception
         |
         +--> Capture CDN stream URL
                  |
                  +--> yt-dlp / FFmpeg
```

The browser based fallback is useful for sites where the actual video URL is generated only after JavaScript runs or after Cloudflare has completed its checks.

## Requirements

### Python

Python 3.10 or newer is required.

### Python packages

Install the required packages with:

```bash
pip install -r requirements.txt
```

The main dependencies are:

| Package        | Purpose                                          |
| -------------- | ------------------------------------------------ |
| `yt-dlp`       | Video extraction and downloading                 |
| `curl_cffi`    | HTTP requests with browser like TLS fingerprints |
| `DrissionPage` | Chrome automation and browser based extraction   |

### System dependencies

| Dependency   | Purpose                               | Installation                             |
| ------------ | ------------------------------------- | ---------------------------------------- |
| Python 3.10+ | Runs the application                  | [python.org](https://python.org)         |
| Chrome       | Browser based extraction              | Install Google Chrome                    |
| FFmpeg       | Format conversion and post processing | `winget install ffmpeg` on Windows       |
| FFmpeg       | Format conversion and post processing | `brew install ffmpeg` on macOS           |
| FFmpeg       | Format conversion and post processing | `apt install ffmpeg` on Debian or Ubuntu |

Chrome must be installed for the DrissionPage fallback to work.

FFmpeg is optional, but recommended if you want reliable format conversion.

## Installation

Clone the repository:

```bash
git clone https://github.com/yourname/scrape.git
cd scrape
```

Install the Python dependencies:

```bash
pip install -r requirements.txt
```

Make sure Chrome and FFmpeg are available on your system.

You can verify FFmpeg with:

```bash
ffmpeg -version
```

Then start the downloader:

```bash
python scraper.py
```

## Usage

Run the application without arguments:

```bash
python scraper.py
```

Paste the video URL when prompted, then choose the desired output format.

You can also provide the URL directly:

```bash
python scraper.py https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

Supported output formats include:

```text
mp4
mp3
mkv
webm
original
```

Downloaded files are placed in:

```text
videos/
```

The directory is created automatically when needed.

## YouTube and 403 errors

YouTube can require browser authentication and additional verification when downloading certain streams.

`scrape` attempts the following when a normal `yt-dlp` download encounters a 403:

```text
yt-dlp
  |
  +-- Edge cookies
  |
  +-- Chrome cookies
  |
  +-- Firefox cookies
```

If you are logged into YouTube in one of these browsers, the corresponding cookies may allow `yt-dlp` to access streams that otherwise return a 403.

Keeping `yt-dlp` updated is also important. `scrape` checks for updates when it starts and updates the installed version when necessary.

## Cloudflare protected sites

For sites protected by Cloudflare, the downloader can launch an actual Chrome session rather than relying entirely on direct HTTP requests.

DrissionPage controls Chrome and allows the page to complete its JavaScript based checks normally.

Once the page is loaded, `scrape` can inspect the page and monitor browser network traffic for media requests.

This is particularly useful when a site does not expose the final video URL in its initial HTML.

## Token bound CDN URLs

Some sites generate temporary CDN URLs only after the video player starts.

In these cases, looking at the page source is not enough.

`scrape` can monitor browser network requests and identify media URLs generated during playback. When a usable stream URL is found, it is passed to the appropriate downloader or FFmpeg processing stage.

These URLs may be temporary or tied to the browser session, so they are not expected to remain valid indefinitely.

## Extraction order

The downloader attempts extraction in the following order:

1. Native `yt-dlp` extraction for supported platforms
2. Direct HTTP extraction through `curl_cffi`
3. Chrome based extraction through DrissionPage
4. HTML, iframe, and base64 inspection
5. Browser network interception
6. Download and post processing through `yt-dlp` or FFmpeg

This allows the simplest method to handle normal sites while keeping browser automation as a fallback for more difficult ones.

## Output

Files are saved automatically inside the `videos` directory:

```text
scrape/
├── scraper.py
├── requirements.txt
├── README.md
└── videos/
    └── downloaded_video.mp4
```

Existing files are skipped, so running the downloader again will not unnecessarily download the same file.

## FFmpeg

FFmpeg is used for operations such as:

* Converting between supported formats
* Extracting audio
* Merging separate audio and video streams
* Post processing downloads

Without FFmpeg, some downloads and conversions may be limited by the formats provided directly by the source.

## Limitations

`scrape` cannot guarantee that every site will work.

Modern video platforms can use DRM, encrypted streams, authentication, expiring tokens, browser fingerprints, or site specific APIs that change without notice.

Cloudflare handling also depends on the site configuration and the browser being able to complete its checks normally.

When a site changes its player or delivery system, the corresponding extraction layer may need to be updated.

## Legal and responsible use*

Only download content that you have permission to download and use.

This project is intended for personal use, testing, research, and legitimate media retrieval. Respect the terms of service, copyright, and access restrictions of the websites you use it with.

## License

This project is released under the MIT License.
