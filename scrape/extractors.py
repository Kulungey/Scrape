"""Pure, network-free extraction logic: pulling media/player/iframe URLs out
of HTML, plus a small pluggable chain of Extractor objects that wraps them
for the pipeline.

Everything in this module takes strings in and returns MediaResult/str out —
no sockets, no browser, no subprocess. That's what makes it unit-testable
with fake HTML instead of live websites.
"""

from __future__ import annotations

import base64
import re
from urllib.parse import urljoin

from .media import MediaResult
from .patterns import DIRECT_RE, IFRAME_RE, IFRAME_SKIP_RE, SOURCES_RE


def find_direct_url(html: str) -> str | None:
    # Try attribute-style match first (src=, file=, href=)
    m = DIRECT_RE.search(html)
    if m:
        return m.group(1)
    # Fall back to bare JS assignment (JWPlayer sources, videoUrl, etc.)
    m = SOURCES_RE.search(html)
    return m.group(1) if m else None


def b64_try(s: str) -> str | None:
    try:
        decoded = base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-8")
        if decoded.startswith(("http", "/")):
            return decoded
    except Exception:
        pass
    return None


def extract_player_url(html: str, base_url: str) -> str | None:
    for m in IFRAME_RE.finditer(html):
        src = m.group(1).strip()
        if not src or IFRAME_SKIP_RE.search(src):
            continue
        resolved = src if src.startswith("http") else urljoin(base_url, src)
        if resolved.startswith("http"):
            return resolved
    return None


def extract_media_from_player(html: str, player_base: str) -> dict:
    """Dig a video/subtitle/thumb URL out of a player page's HTML. Returns
    a dict (kept as the original shape here since it carries three distinct
    optional URLs plus a follow-up player_url — a MediaResult is built from
    this by the caller once the final video URL is resolved)."""
    result = {"video": None, "subtitle": None, "thumb": None, "player_url": None}
    direct = find_direct_url(html)
    if direct:
        result["video"] = direct
        return result
    m = re.search(r'data-id=["\']([^"\']+)["\']', html)
    if not m:
        for token in re.findall(r'[A-Za-z0-9+/]{30,}={0,2}', html):
            decoded = b64_try(token)
            if decoded and re.search(r'\.(mp4|m3u8|mpd)', decoded, re.I):
                result["video"] = decoded
                return result
        return result
    data_id = m.group(1)
    result["player_url"] = urljoin(player_base, data_id)
    params = dict(p.split("=", 1) for p in data_id.split("?", 1)[-1].split("&") if "=" in p)
    for key, target in (("vid", "video"), ("s", "subtitle"), ("i", "thumb")):
        if key in params:
            decoded = b64_try(params[key])
            if decoded:
                result[target] = decoded
    return result


# ── Pluggable extractor chain ──────────────────────────────────────────────────
# Each extractor answers one question: "given this page's HTML, can you find
# the media?" If not, it returns None and the pipeline tries the next one.
# Adding support for a new pattern later means adding one class here, not
# editing a growing if/elif chain in the pipeline.

class Extractor:
    name = "base"

    def extract(self, html: str, base_url: str) -> MediaResult | None:
        raise NotImplementedError


def unpack_js(script_text: str) -> str | None:
    """Decode a Dean Edwards packed JS block: eval(function(p,a,c,k,e,d){...}).

    MissAV (and many other adult sites) hide their HLS source URL inside one
    of these packed blocks.  The packer base-encodes identifiers so a plain
    regex over the raw HTML never sees the .m3u8 URL — you only find it after
    deobfuscation.

    Algorithm mirrors the reference JS implementation:
      1. Extract the payload string, base, count, and key table.
      2. Build a lookup: base-N encoded index → real identifier.
      3. Replace every \\b word \\b token in the payload with its lookup value.
    """
    m = re.search(
        r"eval\(function\(p,a,c,k,e,(?:r|d)\)"
        r"\{.*?\}\s*\("
        r"'((?:[^'\\]|\\.)*)'"   # group 1: payload
        r"\s*,\s*(\d+)"          # group 2: base (radix)
        r"\s*,\s*(\d+)"          # group 3: count
        r"\s*,\s*'([^']*)'"      # group 4: keys joined by '|'
        r"\s*\.split\('\\|'\)",
        script_text,
        re.DOTALL,
    )
    if not m:
        return None

    payload  = m.group(1).replace("\\'", "'")
    base     = int(m.group(2))
    count    = int(m.group(3))
    keys     = m.group(4).split("|")

    digits = "0123456789abcdefghijklmnopqrstuvwxyz"

    def to_base_str(n: int, b: int) -> str:
        if n == 0:
            return "0"
        s = ""
        while n:
            s = digits[n % b] + s
            n //= b
        return s

    lookup = {
        to_base_str(i, base): (keys[i] if i < len(keys) and keys[i] else to_base_str(i, base))
        for i in range(count)
    }

    return re.sub(r"\b(\w+)\b", lambda mo: lookup.get(mo.group(0), mo.group(0)), payload)


def find_m3u8_in_packed_js(html: str) -> str | None:
    """Scan all <script> blocks for packed JS, unpack each, and return the
    first .m3u8 URL found.  This is how MissAV hides its HLS source."""
    for script in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL):
        if "eval(function" not in script:
            continue
        unpacked = unpack_js(script)
        if not unpacked:
            continue
        # Look for: source='https://...m3u8'  OR  file:"https://...m3u8"
        m = re.search(
            r'(?:source|file|src|video_url|hls)\s*[=:]\s*["\']'
            r'(https?://[^\s"\'<>]+\.m3u8(?:[?#][^\s"\'<>]*)?)',
            unpacked, re.I,
        )
        if not m:
            # bare URL fallback
            m = re.search(
                r'(https?://[^\s"\'<>]+\.m3u8(?:[?#][^\s"\'<>]*)?)',
                unpacked, re.I,
            )
        if m:
            return m.group(1)
    return None


class DirectHTMLExtractor(Extractor):
    """Layer 3a: a bare .mp4/.m3u8/.mpd URL sitting directly in the HTML
    (src=, data-src=, a raw string, etc)."""
    name = "html-direct"

    def extract(self, html: str, base_url: str) -> MediaResult | None:
        url = find_direct_url(html)
        if url:
            return MediaResult.from_url(url, referer=base_url, source=self.name)
        return None


class PackedJSExtractor(Extractor):
    """Layer 3b: M3U8 URL hidden inside a Dean Edwards packed JS block.

    MissAV and many JAV/adult sites use eval(function(p,a,c,k,e,d){...})
    obfuscation.  Plain regex over the raw HTML never sees the URL — we
    have to unpack first.  This runs before IframeExtractor so we don't
    waste a network round-trip chasing an iframe when the URL is right here.
    """
    name = "packed-js"

    def extract(self, html: str, base_url: str) -> MediaResult | None:
        url = find_m3u8_in_packed_js(html)
        if url:
            return MediaResult.from_url(url, referer=base_url, source=self.name)
        return None


class IframeExtractor(Extractor):
    """Layer 3c: no direct URL, but there's a player iframe — resolve it to
    an absolute player_url for the pipeline to fetch and re-scan."""
    name = "iframe"

    def extract(self, html: str, base_url: str) -> MediaResult | None:
        player_url = extract_player_url(html, base_url)
        if player_url:
            return MediaResult(url=None, referer=player_url, source=self.name)
        return None


DEFAULT_CHAIN: list[Extractor] = [
    DirectHTMLExtractor(),
    PackedJSExtractor(),   # ← new: unpack eval(function(p,a,c,k,e,d){...}) blocks
    IframeExtractor(),
]
