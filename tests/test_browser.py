import pytest

from scrape.browser import PLAY_BUTTON_SELECTORS, try_play_click


class _FakeElement:
    def __init__(self):
        self.clicked = False

    def click(self):
        self.clicked = True


class _FakeDriver:
    """Mimics DrissionPage's driver.ele(selector, timeout=...) interface.

    `present` maps selector -> element (or omit the key for "not found",
    matching DrissionPage raising/returning falsy when nothing matches)."""

    def __init__(self, present: dict):
        self.present = present
        self.calls = []

    def ele(self, selector, timeout=0.6):
        self.calls.append(selector)
        if selector in self.present:
            return self.present[selector]
        raise Exception("element not found")  # DrissionPage raises on timeout


def test_selector_priority_video_js_and_jw_player_come_first():
    # Guards the ordering the research was built on — most specific/stable
    # selectors first, generic fallbacks last.
    assert PLAY_BUTTON_SELECTORS[0] == "css:.vjs-big-play-button"
    assert PLAY_BUTTON_SELECTORS[1] == "css:.jw-icon-display"
    assert "css:[class*='play']" in PLAY_BUTTON_SELECTORS


def test_clicks_video_js_button_when_present():
    btn = _FakeElement()
    driver = _FakeDriver({"css:.vjs-big-play-button": btn})
    result = try_play_click(driver)
    assert result == "css:.vjs-big-play-button"
    assert btn.clicked


def test_falls_through_to_jw_player_when_video_js_absent():
    btn = _FakeElement()
    driver = _FakeDriver({"css:.jw-icon-display": btn})
    result = try_play_click(driver)
    assert result == "css:.jw-icon-display"
    assert btn.clicked


def test_stops_at_first_match_does_not_click_later_selectors():
    vjs_btn = _FakeElement()
    generic_btn = _FakeElement()
    driver = _FakeDriver({
        "css:.vjs-big-play-button": vjs_btn,
        "css:.play-button": generic_btn,
    })
    result = try_play_click(driver)
    assert result == "css:.vjs-big-play-button"
    assert vjs_btn.clicked
    assert not generic_btn.clicked
    # never even checked selectors past the one it clicked
    assert "css:.play-button" not in driver.calls


def test_returns_none_when_nothing_matches():
    driver = _FakeDriver({})  # no selector present — plain <video> site
    result = try_play_click(driver)
    assert result is None
    assert driver.calls == PLAY_BUTTON_SELECTORS  # tried every selector, safely


def test_custom_selector_list_respected():
    btn = _FakeElement()
    driver = _FakeDriver({"css:custom-play": btn})
    result = try_play_click(driver, selectors=["css:custom-play"])
    assert result == "css:custom-play"
    assert btn.clicked
