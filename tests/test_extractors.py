import base64

from scrape.extractors import (
    find_direct_url, b64_try, extract_player_url, extract_media_from_player,
    DirectHTMLExtractor, IframeExtractor,
)
from scrape.media import MediaKind


def test_find_direct_url_src_attr():
    html = '<video src="https://cdn.example.com/test.m3u8"></video>'
    assert find_direct_url(html) == "https://cdn.example.com/test.m3u8"


def test_find_direct_url_no_match():
    assert find_direct_url("<div>nothing here</div>") is None


def test_find_direct_url_mp4():
    html = '<a href="https://cdn.example.com/movie.mp4?token=abc">watch</a>'
    assert find_direct_url(html) == "https://cdn.example.com/movie.mp4?token=abc"


def test_b64_try_valid_url():
    encoded = base64.b64encode(b"https://cdn.example.com/video.mp4").decode()
    assert b64_try(encoded) == "https://cdn.example.com/video.mp4"


def test_b64_try_invalid_padding_still_decodes():
    # base64 without padding — b64_try should pad it itself
    encoded = base64.b64encode(b"https://cdn.example.com/x.mp4").decode().rstrip("=")
    assert b64_try(encoded) == "https://cdn.example.com/x.mp4"


def test_b64_try_garbage_returns_none():
    assert b64_try("not valid base64!!!") is None


def test_b64_try_decodes_but_not_url_returns_none():
    encoded = base64.b64encode(b"just some text").decode()
    assert b64_try(encoded) is None


def test_extract_player_url_absolute():
    html = '<iframe src="https://player.example.com/embed/123"></iframe>'
    assert extract_player_url(html, "https://site.example.com") == \
        "https://player.example.com/embed/123"


def test_extract_player_url_relative_resolves_against_base():
    html = '<iframe src="/embed/123"></iframe>'
    assert extract_player_url(html, "https://site.example.com/page") == \
        "https://site.example.com/embed/123"


def test_extract_player_url_skips_recaptcha():
    html = '<iframe src="https://google.com/recaptcha/api2/anchor"></iframe>'
    assert extract_player_url(html, "https://site.example.com") is None


def test_extract_media_from_player_direct_video():
    html = '<video src="https://cdn.example.com/movie.mp4"></video>'
    result = extract_media_from_player(html, "https://player.example.com")
    assert result["video"] == "https://cdn.example.com/movie.mp4"


def test_extract_media_from_player_no_match():
    result = extract_media_from_player("<p>nothing</p>", "https://player.example.com")
    assert result["video"] is None
    assert result["player_url"] is None


def test_direct_html_extractor_returns_media_result():
    html = '<video src="https://cdn.example.com/clip.m3u8"></video>'
    result = DirectHTMLExtractor().extract(html, "https://site.example.com")
    assert result is not None
    assert result.url == "https://cdn.example.com/clip.m3u8"
    assert result.kind == MediaKind.HLS


def test_direct_html_extractor_no_match_returns_none():
    result = DirectHTMLExtractor().extract("<p>nothing</p>", "https://site.example.com")
    assert result is None


def test_iframe_extractor_returns_result_without_url():
    html = '<iframe src="https://player.example.com/embed/1"></iframe>'
    result = IframeExtractor().extract(html, "https://site.example.com")
    assert result is not None
    assert result.url is None
    assert result.referer == "https://player.example.com/embed/1"
