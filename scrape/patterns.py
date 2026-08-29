"""Compiled regexes shared across extractors, the browser listener, and
yt-dlp progress parsing. Compiled once at import time."""

import re

MEDIA_RE = re.compile(
    r'https?://[^\s"\'<>{}\[\]]+\.(?:mp4|m3u8|mpd|webm|ts)(?:[?#][^\s"\'<>]*)?',
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
    r'["\']?(https?://[^\s"\'<>{}\[\]]+\.(?:mp4|m3u8|mpd|webm|ts)(?:[?#][^\s"\'<>]*)?)',
    re.I,
)
YT_RE = re.compile(
    r'(?:https?://)?(?:www\.|m\.)?'
    r'(?:youtube\.com/(?:watch|shorts|live|embed)|youtu\.be/)',
    re.I
)
