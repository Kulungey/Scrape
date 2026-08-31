import pytest
from unittest.mock import patch

from scrape import config, pipeline


def _reset_config():
    config.set_allow_browser(True)
    config.set_allow_ytdlp(True)


def test_recognized_site_bypasses_html_and_browser_layers():
    """The whole point of the probe-first flip: if yt-dlp recognizes the
    site, _simple_fetch/drission_fetch (HTML + browser layers) must never
    be called at all."""
    _reset_config()
    with patch("scrape.pipeline.ytdlp_ok", return_value=True), \
         patch("scrape.pipeline.ytdlp_probe", return_value=True), \
         patch("scrape.pipeline.ytdlp_download", return_value=True) as mock_dl, \
         patch("scrape.pipeline._simple_fetch") as mock_fetch, \
         patch("scrape.pipeline.drission_fetch") as mock_browser:
        with pytest.raises(SystemExit) as exc:
            pipeline.scrape("https://www.reddit.com/r/foo/comments/abc123", "mp4")
        assert exc.value.code == 0
        mock_dl.assert_called_once()
        mock_fetch.assert_not_called()
        mock_browser.assert_not_called()


def test_unrecognized_site_tries_browser_before_direct_fetch():
    """New order: probe → browser → (extractor) → intercept → direct fetch last.
    If the browser gets HTML with a video URL, direct fetch is never called."""
    _reset_config()
    with patch("scrape.pipeline.ytdlp_ok", return_value=True), \
         patch("scrape.pipeline.ytdlp_probe", return_value=False), \
         patch("scrape.pipeline.drission_fetch",
               return_value=('<video src="https://example.com/x.mp4"></video>', None, None, None)) as mock_browser, \
         patch("scrape.pipeline._simple_fetch") as mock_fetch, \
         patch("scrape.pipeline._intercept", return_value=False), \
         patch("scrape.pipeline.download_file", return_value="[1/1] SAVED: x.mp4"), \
         patch("os.makedirs"):
        pipeline.scrape("https://custom-site.example.com/watch/1", "mp4")
        mock_browser.assert_called_once()
        mock_fetch.assert_not_called()   # direct fetch never needed


def test_direct_fetch_used_as_last_resort_when_browser_gets_no_html():
    """If the browser returns no HTML and intercept fails, direct fetch
    is the last layer tried."""
    _reset_config()
    with patch("scrape.pipeline.ytdlp_ok", return_value=True), \
         patch("scrape.pipeline.ytdlp_probe", return_value=False), \
         patch("scrape.pipeline.drission_fetch", return_value=(None, None, None, None)), \
         patch("scrape.pipeline._intercept", return_value=False), \
         patch("scrape.pipeline._simple_fetch",
               return_value=('<video src="https://example.com/x.mp4"></video>', None)) as mock_fetch, \
         patch("scrape.pipeline.download_file", return_value="[1/1] SAVED: x.mp4"), \
         patch("os.makedirs"):
        pipeline.scrape("https://custom-site.example.com/watch/1", "mp4")
        mock_fetch.assert_called_once()


def test_no_ytdlp_probe_skipped_when_no_ytdlp_flag_set():
    """--no-ytdlp must disable the probe entirely, not just the fallback."""
    _reset_config()
    config.set_allow_ytdlp(False)
    with patch("scrape.pipeline.ytdlp_ok", return_value=True), \
         patch("scrape.pipeline.ytdlp_probe") as mock_probe, \
         patch("scrape.pipeline.drission_fetch", return_value=(None, None)), \
         patch("scrape.pipeline._intercept", return_value=False), \
         patch("scrape.pipeline._simple_fetch", return_value=("<p>nothing</p>", None)):
        with pytest.raises(SystemExit):
            pipeline.scrape("https://www.reddit.com/r/foo/comments/abc123", "mp4")
        mock_probe.assert_not_called()
    _reset_config()


def test_embedded_vimeo_iframe_probed_with_page_referer():
    """A Vimeo iframe found by the extractor chain on an unrelated hosting
    site should be probed directly (with the hosting page as Referer)
    rather than falling through to raw HTML parsing of the player page."""
    _reset_config()
    html = '<iframe src="https://player.vimeo.com/video/123456"></iframe>'
    with patch("scrape.pipeline.ytdlp_ok", return_value=True), \
         patch("scrape.pipeline.ytdlp_probe", return_value=False) as mock_probe_site, \
         patch("scrape.pipeline.drission_fetch", return_value=(html, None, None, None)), \
         patch("scrape.pipeline.ytdlp_download", return_value=True) as mock_dl:

        def probe_side_effect(url, referer=None):
            return "player.vimeo.com" in url

        mock_probe_site.side_effect = probe_side_effect

        with pytest.raises(SystemExit) as exc:
            pipeline.scrape("https://blog.example.com/my-post", "mp4")
        assert exc.value.code == 0
        mock_dl.assert_called_once()
        call_args = mock_dl.call_args[0]
        assert call_args[0] == "https://player.vimeo.com/video/123456"
        assert call_args[1] == "https://blog.example.com/my-post"


def test_reddit_share_link_resolved_before_detection():
    """The actual reported bug: reddit.com/r/<sub>/s/<code> must be
    resolved to its canonical /comments/... form BEFORE platform detection,
    since the share-link and the canonical URL both match is_reddit() but
    the canonical form is what yt-dlp needs to auth against the Reddit API.
    With the fast-path detector, no probe is needed — it goes straight to
    ytdlp_download with the resolved URL."""
    _reset_config()
    short = "https://www.reddit.com/r/eFootball/s/zmfMsrgZoM"
    canonical = "https://www.reddit.com/r/eFootball/comments/1abcde/some_title/"
    with patch("scrape.pipeline.resolve_redirect", return_value=canonical) as mock_resolve, \
         patch("scrape.pipeline.ytdlp_ok", return_value=True), \
         patch("scrape.pipeline.ytdlp_probe") as mock_probe, \
         patch("scrape.pipeline.ytdlp_download", return_value=True) as mock_dl, \
         patch("scrape.pipeline._simple_fetch") as mock_fetch:
        with pytest.raises(SystemExit) as exc:
            pipeline.scrape(short, "mp4")
        assert exc.value.code == 0
        mock_resolve.assert_called_once_with(short)
        # Fast-path — probe never called; download sees the RESOLVED url
        mock_probe.assert_not_called()
        mock_dl.assert_called_once_with(canonical, canonical, "mp4", cf_session=None)
        mock_fetch.assert_not_called()


def test_redirect_resolution_failure_still_proceeds_with_original_url():
    """If resolve_redirect can't reach the network, the pipeline must
    still proceed with the original URL rather than dying."""
    _reset_config()
    url = "https://custom-site.example.com/watch/1"
    with patch("scrape.pipeline.resolve_redirect", return_value=url), \
         patch("scrape.pipeline.ytdlp_ok", return_value=True), \
         patch("scrape.pipeline.ytdlp_probe", return_value=False), \
         patch("scrape.pipeline.drission_fetch",
               return_value=('<video src="https://example.com/x.mp4"></video>', None, None, None)), \
         patch("scrape.pipeline._intercept", return_value=False), \
         patch("scrape.pipeline.download_file", return_value="[1/1] SAVED: x.mp4"), \
         patch("os.makedirs"):
        pipeline.scrape(url, "mp4")


# ── Fast-path platform routing tests ─────────────────────────────────────────


def test_vimeo_fast_path_uses_ytdlp_vimeo():
    """Vimeo fast-path calls ytdlp_vimeo (cookie-aware); on success exits 0."""
    _reset_config()
    url = "https://vimeo.com/123456789"
    with patch("scrape.pipeline.resolve_redirect", return_value=url), \
         patch("scrape.pipeline.ytdlp_ok", return_value=True), \
         patch("scrape.pipeline.ytdlp_probe") as mock_probe, \
         patch("scrape.pipeline.ytdlp_vimeo", return_value=True) as mock_vimeo, \
         patch("scrape.pipeline.ytdlp_download") as mock_dl:
        with pytest.raises(SystemExit) as exc:
            pipeline.scrape(url, "mp4")
        assert exc.value.code == 0
        mock_probe.assert_not_called()   # skipped — fast-path
        mock_vimeo.assert_called_once_with(url, "mp4")
        mock_dl.assert_not_called()


def test_vimeo_falls_through_to_browser_when_ytdlp_fails():
    """When ytdlp_vimeo exhausts all cookie attempts, the pipeline must fall
    through to browser intercept rather than hard-exiting with failure.
    This covers the 'no Vimeo session in any browser' case."""
    _reset_config()
    url = "https://vimeo.com/1218375109"
    with patch("scrape.pipeline.resolve_redirect", return_value=url), \
         patch("scrape.pipeline.ytdlp_ok", return_value=True), \
         patch("scrape.pipeline.ytdlp_vimeo", return_value=False) as mock_vimeo, \
         patch("scrape.pipeline._intercept", return_value=True) as mock_intercept:
        with pytest.raises(SystemExit) as exc:
            pipeline.scrape(url, "mp4")
        assert exc.value.code == 0
        mock_vimeo.assert_called_once_with(url, "mp4")
        mock_intercept.assert_called_once_with(url, url, "mp4")


def test_vimeo_no_browser_flag_raises_when_ytdlp_fails():
    """With --no-browser, a Vimeo yt-dlp failure must raise SystemExit
    rather than silently falling through."""
    _reset_config()
    config.set_allow_browser(False)
    url = "https://vimeo.com/123456789"
    with patch("scrape.pipeline.resolve_redirect", return_value=url), \
         patch("scrape.pipeline.ytdlp_ok", return_value=True), \
         patch("scrape.pipeline.ytdlp_vimeo", return_value=False):
        with pytest.raises(SystemExit) as exc:
            pipeline.scrape(url, "mp4")
        assert exc.value.code != 0 or isinstance(exc.value.code, str)
    _reset_config()


def test_tiktok_fast_path_uses_ytdlp():
    """TikTok fast-path calls yt-dlp first; on success exits 0 without
    ever touching the browser layer."""
    _reset_config()
    url = "https://www.tiktok.com/@user/video/12345"
    with patch("scrape.pipeline.resolve_redirect", return_value=url), \
         patch("scrape.pipeline.ytdlp_ok", return_value=True), \
         patch("scrape.pipeline.ytdlp_probe") as mock_probe, \
         patch("scrape.pipeline.ytdlp_download", return_value=True) as mock_dl, \
         patch("scrape.pipeline._intercept") as mock_intercept:
        with pytest.raises(SystemExit) as exc:
            pipeline.scrape(url, "mp4")
        assert exc.value.code == 0
        mock_probe.assert_not_called()   # skipped — fast-path
        mock_dl.assert_called_once_with(url, url, "mp4", cf_session=None)
        mock_intercept.assert_not_called()


def test_tiktok_falls_through_to_browser_when_ytdlp_fails():
    """The actual reported bug: yt-dlp's TikTok extractor breaks whenever
    TikTok restructures its page JSON ('Unable to extract universal data
    for rehydration'). When that happens the pipeline must fall through
    to browser intercept rather than hard-exiting with failure."""
    _reset_config()
    url = "https://www.tiktok.com/@progearcambodia/video/7610985031191891221"
    with patch("scrape.pipeline.resolve_redirect", return_value=url), \
         patch("scrape.pipeline.ytdlp_ok", return_value=True), \
         patch("scrape.pipeline.ytdlp_download", return_value=False) as mock_dl, \
         patch("scrape.pipeline._intercept", return_value=True) as mock_intercept:
        with pytest.raises(SystemExit) as exc:
            pipeline.scrape(url, "mp4")
        assert exc.value.code == 0
        mock_dl.assert_called_once_with(url, url, "mp4", cf_session=None)
        mock_intercept.assert_called_once_with(url, url, "mp4")


def test_tiktok_no_browser_flag_raises_when_ytdlp_fails():
    """With --no-browser, a TikTok yt-dlp failure must raise SystemExit
    rather than silently falling through."""
    _reset_config()
    config.set_allow_browser(False)
    url = "https://www.tiktok.com/@user/video/12345"
    with patch("scrape.pipeline.resolve_redirect", return_value=url), \
         patch("scrape.pipeline.ytdlp_ok", return_value=True), \
         patch("scrape.pipeline.ytdlp_download", return_value=False):
        with pytest.raises(SystemExit) as exc:
            pipeline.scrape(url, "mp4")
        assert exc.value.code != 0 or isinstance(exc.value.code, str)
    _reset_config()


@pytest.mark.parametrize("url,label", [
    ("https://www.dailymotion.com/video/x8abcde",     "dailymotion"),
    ("https://www.twitch.tv/videos/123456789",        "twitch"),
    ("https://clips.twitch.tv/SomeClipName",          "twitch"),
])
def test_known_platform_fast_path(url, label):
    """Known platforms (non-Vimeo) skip the probe and go straight to ytdlp_download."""
    _reset_config()
    with patch("scrape.pipeline.resolve_redirect", return_value=url), \
         patch("scrape.pipeline.ytdlp_ok", return_value=True), \
         patch("scrape.pipeline.ytdlp_probe") as mock_probe, \
         patch("scrape.pipeline.ytdlp_download", return_value=True) as mock_dl:
        with pytest.raises(SystemExit) as exc:
            pipeline.scrape(url, "mp4")
        assert exc.value.code == 0
        mock_probe.assert_not_called()
        mock_dl.assert_called_once_with(url, url, "mp4", cf_session=None)


def test_probe_before_browser_and_direct_fetch_for_unknown_sites():
    """Order: probe → browser → direct fetch.  If probe succeeds, neither
    drission_fetch nor _simple_fetch should be called."""
    _reset_config()
    url = "https://bilibili.com/video/BV1xx411c7mD"
    with patch("scrape.pipeline.resolve_redirect", return_value=url), \
         patch("scrape.pipeline.ytdlp_ok", return_value=True), \
         patch("scrape.pipeline.ytdlp_probe", return_value=True) as mock_probe, \
         patch("scrape.pipeline.ytdlp_download", return_value=True) as mock_dl, \
         patch("scrape.pipeline.drission_fetch") as mock_browser, \
         patch("scrape.pipeline._simple_fetch") as mock_fetch:
        with pytest.raises(SystemExit) as exc:
            pipeline.scrape(url, "mp4")
        assert exc.value.code == 0
        mock_probe.assert_called_once()
        mock_dl.assert_called_once()
        mock_browser.assert_not_called()  # probe won — browser never needed
        mock_fetch.assert_not_called()    # probe won — direct fetch never ran
