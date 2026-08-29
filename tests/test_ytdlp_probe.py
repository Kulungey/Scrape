import subprocess
from unittest.mock import patch, MagicMock

from scrape.ytdlp import ytdlp_probe
from scrape import ytdlp as ytdlp_module


def _fake_which(name):
    return "/usr/bin/yt-dlp" if name == "yt-dlp" else None


def test_probe_recognized_site_returns_true():
    ytdlp_module._YTDLP = None  # reset cache
    with patch("shutil.which", side_effect=_fake_which), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout=b"https://cdn.example.com/video.mp4\n"
        )
        assert ytdlp_probe("https://reddit.com/r/foo/comments/abc") is True


def test_probe_unrecognized_site_returns_false():
    ytdlp_module._YTDLP = None
    with patch("shutil.which", side_effect=_fake_which), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout=b"")
        assert ytdlp_probe("https://some-random-cloudflare-site.example.com") is False


def test_probe_times_out_returns_false():
    ytdlp_module._YTDLP = None
    with patch("shutil.which", side_effect=_fake_which), \
         patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="yt-dlp", timeout=5)):
        assert ytdlp_probe("https://slow-site.example.com") is False


def test_probe_no_ytdlp_installed_returns_false_without_calling_subprocess():
    ytdlp_module._YTDLP = None
    with patch("shutil.which", return_value=None), \
         patch("subprocess.run") as mock_run:
        assert ytdlp_probe("https://reddit.com/r/foo") is False
        mock_run.assert_not_called()


def test_probe_passes_referer_when_given():
    ytdlp_module._YTDLP = None
    with patch("shutil.which", side_effect=_fake_which), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"https://x\n")
        ytdlp_probe("https://player.vimeo.com/video/123", referer="https://site.example.com")
        called_cmd = mock_run.call_args[0][0]
        assert "--referer" in called_cmd
        assert "https://site.example.com" in called_cmd


def test_probe_empty_stdout_with_zero_returncode_is_false():
    # yt-dlp can exit 0 with no output in some edge cases — shouldn't count as a hit
    ytdlp_module._YTDLP = None
    with patch("shutil.which", side_effect=_fake_which), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"")
        assert ytdlp_probe("https://ambiguous.example.com") is False


# ── New platform detector tests ───────────────────────────────────────────────
from scrape.ytdlp import is_vimeo, is_dailymotion, is_reddit, is_tiktok, is_twitch

class TestPlatformDetectors:
    def test_vimeo_direct(self):
        assert is_vimeo("https://vimeo.com/123456789")
    def test_vimeo_www(self):
        assert is_vimeo("https://www.vimeo.com/123456789")
    def test_vimeo_negative(self):
        assert not is_vimeo("https://example.com/embed/vimeo")

    def test_dailymotion(self):
        assert is_dailymotion("https://www.dailymotion.com/video/x8abcde")
    def test_dailymotion_negative(self):
        assert not is_dailymotion("https://dailymotion.com/embed/video/x8abcde")

    def test_reddit_comments(self):
        assert is_reddit("https://www.reddit.com/r/videos/comments/abc123/title/")
    def test_reddit_share_link(self):
        assert is_reddit("https://www.reddit.com/r/eFootball/s/zmfMsrgZoM")
    def test_reddit_media(self):
        assert is_reddit("https://v.redd.it/abc123")
    def test_reddit_negative(self):
        assert not is_reddit("https://example.com/reddit-style")

    def test_tiktok_www(self):
        assert is_tiktok("https://www.tiktok.com/@user/video/7123456789")
    def test_tiktok_vm_shortlink(self):
        assert is_tiktok("https://vm.tiktok.com/ZMxxxxxx/")
    def test_tiktok_vt_shortlink(self):
        assert is_tiktok("https://vt.tiktok.com/ZSxxxxxx/")
    def test_tiktok_negative(self):
        assert not is_tiktok("https://example.com/tiktok-embed")

    def test_twitch_video(self):
        assert is_twitch("https://www.twitch.tv/videos/123456789")
    def test_twitch_clip(self):
        assert is_twitch("https://www.twitch.tv/clips/SomeClipName")
    def test_twitch_channel(self):
        assert is_twitch("https://www.twitch.tv/somechannel")
    def test_twitch_negative(self):
        assert is_twitch("https://clips.twitch.tv/SomeclipName")
