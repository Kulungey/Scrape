from unittest.mock import patch, MagicMock

from scrape.config import resolve_redirect


class _FakeResponse:
    def __init__(self, url):
        self.url = url
        self.closed = False

    def close(self):
        self.closed = True


def test_reddit_share_link_resolves_to_comments_url():
    """The exact bug: reddit.com/r/<sub>/s/<code> must resolve to the
    canonical /comments/... URL yt-dlp's regex actually expects."""
    short = "https://www.reddit.com/r/eFootball/s/zmfMsrgZoM"
    canonical = "https://www.reddit.com/r/eFootball/comments/1abcde/some_title/"
    with patch("scrape.config.raw_get", return_value=_FakeResponse(canonical)) as mock_get:
        result = resolve_redirect(short)
    assert result == canonical
    mock_get.assert_called_once()


def test_no_redirect_returns_same_url():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    with patch("scrape.config.raw_get", return_value=_FakeResponse(url)):
        assert resolve_redirect(url) == url


def test_request_failure_falls_back_to_original_url():
    """Network errors must never break the pipeline — always degrade to
    the original URL rather than raising."""
    url = "https://unreachable.example.com/x"
    with patch("scrape.config.raw_get", side_effect=Exception("connection refused")):
        assert resolve_redirect(url) == url


def test_response_closed_even_on_success():
    """Must not leave the connection open — we only wanted the final URL,
    not the body."""
    resp = _FakeResponse("https://resolved.example.com/final")
    with patch("scrape.config.raw_get", return_value=resp):
        resolve_redirect("https://short.example.com/abc")
    assert resp.closed is True


def test_response_closed_even_if_url_attr_missing():
    """Some response-like objects might lack .url — must not crash, and
    still attempt to close."""
    resp = MagicMock()
    del resp.url  # simulate missing attribute
    resp.url = None
    with patch("scrape.config.raw_get", return_value=resp):
        result = resolve_redirect("https://short.example.com/abc")
    assert result == "https://short.example.com/abc"
    resp.close.assert_called_once()
