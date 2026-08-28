"""Two logging modes:

Normal: the colored [tag] lines from ui.cprint (URLs redacted to host-only).
--debug: also emits structured key=value diagnostic lines via the stdlib
`logging` module (extractor=..., status=..., elapsed=... etc), and
ui.cprint/cprint_url switch to showing full URLs.
"""

import logging

from . import ui


def configure(debug: bool) -> None:
    ui.set_debug(debug)
    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(message)s" if debug else "%(levelname)s: %(message)s",
    )


def debug_event(**fields) -> None:
    """Emit a structured debug line, e.g.
    debug_event(extractor="browser", status="success", media_type="hls")
    -> 'extractor=browser status=success media_type=hls' (only under --debug)."""
    if not ui.is_debug():
        return
    logging.getLogger("scrape").debug(" ".join(f"{k}={v}" for k, v in fields.items()))
