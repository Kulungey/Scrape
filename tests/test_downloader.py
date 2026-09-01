import os

import pytest

from scrape import downloader
from scrape.downloader import (safe_filename, download_file, _load_resume_state,
                               _save_resume_state, _clear_resume_state,
                               _resume_state_path)


def test_safe_filename_strips_known_extension():
    name = safe_filename("https://cdn.example.com/videos/movie.mp4", n=1, ext=".mkv")
    assert name.endswith("01_movie.mkv")


def test_safe_filename_falls_back_when_no_basename():
    name = safe_filename("https://cdn.example.com/", n=3, ext=".mp4")
    assert name.endswith("03_video_3.mp4")


def test_safe_filename_url_decodes():
    name = safe_filename("https://cdn.example.com/my%20video.mp4", n=1, ext=".mp4")
    assert "my video" in name


# ── Resume bookkeeping (pure file I/O, no network) ──────────────────────────

def test_load_resume_state_no_partial_file(tmp_path):
    tmp = str(tmp_path / "out.mp4.part")
    assert _load_resume_state(tmp, "https://cdn.example.com/a.mp4") == 0


def test_save_then_load_resume_state_roundtrip(tmp_path):
    tmp = str(tmp_path / "out.mp4.part")
    url = "https://cdn.example.com/a.mp4"
    with open(tmp, "wb") as f:
        f.write(b"x" * 1024)
    _save_resume_state(tmp, url, etag="abc123", last_modified="Mon, 01 Sep 2026")
    assert _load_resume_state(tmp, url) == 1024


def test_load_resume_state_url_mismatch_ignored(tmp_path):
    tmp = str(tmp_path / "out.mp4.part")
    with open(tmp, "wb") as f:
        f.write(b"x" * 512)
    _save_resume_state(tmp, "https://cdn.example.com/a.mp4", "", "")
    # a different url shouldn't be allowed to resume from someone else's bytes
    assert _load_resume_state(tmp, "https://cdn.example.com/b.mp4") == 0


def test_load_resume_state_missing_sidecar_ignored(tmp_path):
    tmp = str(tmp_path / "out.mp4.part")
    with open(tmp, "wb") as f:
        f.write(b"x" * 512)
    # .part exists but its .resume.json sidecar doesn't — untrusted leftover
    assert _load_resume_state(tmp, "https://cdn.example.com/a.mp4") == 0


def test_load_resume_state_corrupt_sidecar_ignored(tmp_path):
    tmp = str(tmp_path / "out.mp4.part")
    with open(tmp, "wb") as f:
        f.write(b"x" * 512)
    with open(_resume_state_path(tmp), "w") as f:
        f.write("{not valid json")
    assert _load_resume_state(tmp, "https://cdn.example.com/a.mp4") == 0


def test_clear_resume_state_removes_both_files(tmp_path):
    tmp = str(tmp_path / "out.mp4.part")
    url = "https://cdn.example.com/a.mp4"
    with open(tmp, "wb") as f:
        f.write(b"x" * 100)
    _save_resume_state(tmp, url, "", "")
    assert os.path.exists(tmp)
    assert os.path.exists(_resume_state_path(tmp))
    _clear_resume_state(tmp)
    assert not os.path.exists(tmp)
    assert not os.path.exists(_resume_state_path(tmp))


def test_clear_resume_state_noop_when_nothing_there(tmp_path):
    tmp = str(tmp_path / "out.mp4.part")
    _clear_resume_state(tmp)  # should not raise


# ── download_file: raw-HTTP resume path (network mocked) ───────────────────

class _FakeResp:
    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        yield self._body

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _no_ffmpeg(monkeypatch):
    # Force every test in this module past the ffmpeg-first branch and into
    # the raw-HTTP fallback, which is what these tests target.
    monkeypatch.setattr(downloader, "ffmpeg_ok", lambda: False)
    monkeypatch.setattr(downloader, "MIN_MB", 0)
    monkeypatch.setattr(downloader, "_ansi_ready", lambda: False)


def test_download_file_fresh_download_no_prior_partial(tmp_path, monkeypatch):
    url = "https://cdn.example.com/a.mp4"
    out_path = str(tmp_path / "01_a.mp4")
    body = b"y" * 4096

    def fake_raw_get(u, headers, stream=True, timeout=30):
        assert "Range" not in headers  # nothing to resume from yet
        return _FakeResp(200, body, headers={"Content-Length": str(len(body))})

    monkeypatch.setattr(downloader, "raw_get", fake_raw_get)
    result = download_file(url, out_path, referer="https://example.com")

    assert result.startswith("SAVED:")
    assert os.path.exists(out_path)
    with open(out_path, "rb") as f:
        assert f.read() == body
    # success cleans up the .part + sidecar
    assert not os.path.exists(out_path + ".part")
    assert not os.path.exists(_resume_state_path(out_path + ".part"))


def test_download_file_resumes_from_leftover_partial(tmp_path, monkeypatch):
    url = "https://cdn.example.com/a.mp4"
    out_path = str(tmp_path / "01_a.mp4")
    tmp = out_path + ".part"

    already_have = b"A" * 1000
    remaining = b"B" * 500
    with open(tmp, "wb") as f:
        f.write(already_have)
    _save_resume_state(tmp, url, "", "")

    def fake_raw_get(u, headers, stream=True, timeout=30):
        assert headers.get("Range") == f"bytes={len(already_have)}-"
        return _FakeResp(206, remaining, headers={
            "Content-Length": str(len(remaining)),
            "Content-Range": f"bytes {len(already_have)}-1499/1500",
        })

    monkeypatch.setattr(downloader, "raw_get", fake_raw_get)
    result = download_file(url, out_path, referer="https://example.com")

    assert result.startswith("RESUMED:")
    with open(out_path, "rb") as f:
        assert f.read() == already_have + remaining  # appended, not overwritten


def test_download_file_falls_back_to_fresh_when_server_ignores_range(tmp_path, monkeypatch):
    url = "https://cdn.example.com/a.mp4"
    out_path = str(tmp_path / "01_a.mp4")
    tmp = out_path + ".part"

    with open(tmp, "wb") as f:
        f.write(b"A" * 1000)
    _save_resume_state(tmp, url, "", "")

    full_body = b"C" * 2000  # server sent the whole thing back, ignoring Range

    def fake_raw_get(u, headers, stream=True, timeout=30):
        return _FakeResp(200, full_body, headers={"Content-Length": str(len(full_body))})

    monkeypatch.setattr(downloader, "raw_get", fake_raw_get)
    result = download_file(url, out_path, referer="https://example.com")

    assert result.startswith("SAVED:")  # not RESUMED — had to restart
    with open(out_path, "rb") as f:
        assert f.read() == full_body  # overwritten, not appended on top of old bytes


def test_download_file_416_discards_stale_partial_and_restarts(tmp_path, monkeypatch):
    url = "https://cdn.example.com/a.mp4"
    out_path = str(tmp_path / "01_a.mp4")
    tmp = out_path + ".part"

    with open(tmp, "wb") as f:
        f.write(b"A" * 9999)  # offset no longer valid (source shrank, say)
    _save_resume_state(tmp, url, "", "")

    fresh_body = b"D" * 300
    calls = []

    def fake_raw_get(u, headers, stream=True, timeout=30):
        calls.append(headers.get("Range"))
        if len(calls) == 1:
            return _FakeResp(416, b"")
        return _FakeResp(200, fresh_body, headers={"Content-Length": str(len(fresh_body))})

    monkeypatch.setattr(downloader, "raw_get", fake_raw_get)
    result = download_file(url, out_path, referer="https://example.com")

    assert result.startswith("SAVED:")
    assert calls[0] == "bytes=9999-"
    assert calls[1] is None  # second request went out clean, no Range
    with open(out_path, "rb") as f:
        assert f.read() == fresh_body


def test_download_file_url_mismatch_does_not_resume(tmp_path, monkeypatch):
    out_path = str(tmp_path / "01_a.mp4")
    tmp = out_path + ".part"
    with open(tmp, "wb") as f:
        f.write(b"A" * 1000)
    _save_resume_state(tmp, "https://cdn.example.com/different-video.mp4", "", "")

    body = b"E" * 250

    def fake_raw_get(u, headers, stream=True, timeout=30):
        assert "Range" not in headers
        return _FakeResp(200, body, headers={"Content-Length": str(len(body))})

    monkeypatch.setattr(downloader, "raw_get", fake_raw_get)
    result = download_file("https://cdn.example.com/a.mp4", out_path, referer="https://example.com")

    assert result.startswith("SAVED:")
    with open(out_path, "rb") as f:
        assert f.read() == body
