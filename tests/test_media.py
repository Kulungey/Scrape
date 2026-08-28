from scrape.media import MediaKind, MediaResult


def test_media_kind_from_mp4_url():
    assert MediaKind.from_url("https://cdn.example.com/a.mp4") == MediaKind.MP4


def test_media_kind_from_hls_url():
    assert MediaKind.from_url("https://cdn.example.com/a.m3u8?token=x") == MediaKind.HLS


def test_media_kind_from_dash_url():
    assert MediaKind.from_url("https://cdn.example.com/a.mpd") == MediaKind.DASH


def test_media_kind_from_blob():
    assert MediaKind.from_url("blob:https://site.example.com/abc-123") == MediaKind.BLOB


def test_media_kind_unknown():
    assert MediaKind.from_url("https://cdn.example.com/a.jpg") == MediaKind.UNKNOWN


def test_media_kind_none_url():
    assert MediaKind.from_url(None) == MediaKind.UNKNOWN


def test_media_result_from_url_infers_kind():
    result = MediaResult.from_url("https://cdn.example.com/a.mp4", referer="https://x.com")
    assert result.kind == MediaKind.MP4
    assert result.referer == "https://x.com"


def test_media_result_truthiness():
    assert bool(MediaResult.from_url("https://cdn.example.com/a.mp4")) is True
    assert bool(MediaResult.from_url(None)) is False
