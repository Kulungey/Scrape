from scrape.downloader import safe_filename


def test_safe_filename_strips_known_extension():
    name = safe_filename("https://cdn.example.com/videos/movie.mp4", n=1, ext=".mkv")
    assert name.endswith("01_movie.mkv")


def test_safe_filename_falls_back_when_no_basename():
    name = safe_filename("https://cdn.example.com/", n=3, ext=".mp4")
    assert name.endswith("03_video_3.mp4")


def test_safe_filename_url_decodes():
    name = safe_filename("https://cdn.example.com/my%20video.mp4", n=1, ext=".mp4")
    assert "my video" in name
