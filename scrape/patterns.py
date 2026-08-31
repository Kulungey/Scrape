"""Compiled regexes shared across extractors, the browser listener, and
yt-dlp progress parsing. Compiled once at import time."""

import re

# Extensions we treat as "this is the media file we're looking for" —
# shared by the HTML-scanning regexes below and by the browser network
# listener's URL match (see browser.MEDIA_CONTENT_TYPES for the
# Content-Type-based fallback used when a URL has no extension at all).
MEDIA_EXTS = r'mp4|m3u8|mpd|webm|ts|mp3'

MEDIA_RE = re.compile(
    r'https?://[^\s"\'<>{}\[\]]+\.(?:' + MEDIA_EXTS + r')(?:[?#][^\s"\'<>]*)?',
    re.I
)
SKIP_DOMAINS_RE = re.compile(
    r'jwpltx\.com|google-analytics|doubleclick|googlesyndication'
    r'|facebook\.com|twitter\.com|scorecardresearch|omtrdc\.net',
    re.I
)
TOKEN_BOUND_RE  = re.compile(r'\|\d{9,10}\|[0-9a-f]{16,}', re.I)
IFRAME_RE       = re.compile(r'<iframe[^>]+src=["\']([^"\']+)["\']', re.I)
IFRAME_SKIP_RE  = re.compile(
    r'google\.com/recaptcha|accounts\.google|facebook\.com/plugins'
    r'|twitter\.com/i/|disqus\.com|google|facebook|disqus',
    re.I
)
DIRECT_RE = re.compile(
    r'(?:(?:file|src|source|href|data-src)["\s]*[:=]["\s]*|["\'])'
    r'["\']?(https?://[^\s"\'<>{}\[\]]+\.(?:' + MEDIA_EXTS + r')(?:[?#][^\s"\'<>]*)?)',
    re.I,
)
YT_RE = re.compile(
    r'(?:https?://)?(?:www\.|m\.)?'
    r'(?:youtube\.com/(?:watch|shorts|live|embed)|youtu\.be/)',
    re.I
)

# Catches JWPlayer / VideoJS / bare JS assignments:
#   sources: [{file: "https://...m3u8"}]
#   "hls":"https://...m3u8"
SOURCES_RE = re.compile(
    r'(?:file|hls|src|source|video_url|stream_url|videoUrl|streamUrl)'
    r'\s*["\'\']?\s*[:=]\s*["\'\']'
    r'(https?://[^\s"\'\'<>{}\[\]]+\.(?:' + MEDIA_EXTS + r')(?:[?#][^\s"\'\'<>]*)?)',
    re.I,
)
