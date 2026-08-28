"""Shared result types for the extraction pipeline.

Replaces the old convention of passing raw tuples (html, video_url) and
loosely-shaped dicts ({"video": ..., "subtitle": ..., ...}) between layers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class MediaKind(str, Enum):
    MP4     = "mp4"
    WEBM    = "webm"
    HLS     = "hls"      # .m3u8
    DASH    = "dash"     # .mpd
    BLOB    = "blob"     # blob: URLs — never downloadable directly
    UNKNOWN = "unknown"

    @classmethod
    def from_url(cls, url: str | None) -> "MediaKind":
        if not url:
            return cls.UNKNOWN
        if url.startswith("blob:"):
            return cls.BLOB
        m = re.search(r"\.(mp4|webm|m3u8|mpd)(?:[?#]|$)", url, re.I)
        if not m:
            return cls.UNKNOWN
        ext = m.group(1).lower()
        return {
            "mp4":  cls.MP4,
            "webm": cls.WEBM,
            "m3u8": cls.HLS,
            "mpd":  cls.DASH,
        }[ext]


@dataclass
class MediaResult:
    """A single located piece of media, plus the context needed to fetch it."""
    url: str | None
    kind: MediaKind = MediaKind.UNKNOWN
    referer: str | None = None
    source: str | None = None      # which extractor/layer found it, e.g. "browser-intercept"
    title: str | None = None

    @classmethod
    def from_url(cls, url: str | None, referer: str | None = None,
                 source: str | None = None, title: str | None = None) -> "MediaResult":
        return cls(url=url, kind=MediaKind.from_url(url), referer=referer,
                   source=source, title=title)

    def __bool__(self) -> bool:
        return bool(self.url)
