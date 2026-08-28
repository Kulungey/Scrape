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
from .patterns import DIRECT_RE, IFRAME_RE, IFRAME_SKIP_RE


def find_direct_url(html: str) -> str | None:
    m = DIRECT_RE.search(html)
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


class DirectHTMLExtractor(Extractor):
    """Layer 3a: a bare .mp4/.m3u8/.mpd URL sitting directly in the HTML
    (src=, data-src=, a raw string, etc)."""
    name = "html-direct"

    def extract(self, html: str, base_url: str) -> MediaResult | None:
        url = find_direct_url(html)
        if url:
            return MediaResult.from_url(url, referer=base_url, source=self.name)
        return None


class IframeExtractor(Extractor):
    """Layer 3b: no direct URL, but there's a player iframe — resolve it to
    an absolute player_url for the pipeline to fetch and re-scan."""
    name = "iframe"

    def extract(self, html: str, base_url: str) -> MediaResult | None:
        player_url = extract_player_url(html, base_url)
        if player_url:
            # No media URL yet — the pipeline follows player_url and re-runs
            # extraction against the player page's own HTML.
            return MediaResult(url=None, referer=player_url, source=self.name)
        return None


DEFAULT_CHAIN: list[Extractor] = [DirectHTMLExtractor(), IframeExtractor()]
