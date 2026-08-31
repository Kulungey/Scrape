"""Solverr client — tier-3 CF/DDoS-GUARD fallback.

Solverr fuses FlareSolverr's Chrome engine with Byparr's Camoufox (hardened
Firefox) engine behind one process, with automatic fallback between them.
It speaks the same wire format as FlareSolverr/Byparr (`POST /v1`,
`cmd: request.get`), so this client is intentionally generic — it would
work unchanged against FlareSolverr or Byparr if either is swapped in later.

NOT wired into pipeline.py or browser.py yet — this is a standalone,
importable module so it can be reviewed and tested against a running
Solverr container before it touches the existing fallback chain.

Solverr itself is a separate process (Docker container), not a pip
dependency — nothing here belongs in requirements.txt. Run it with
something like:

    docker run -d -p 8191:8191 --name solverr <solverr-image>

and this module talks to it over localhost.
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error

SOLVERR_URL = "http://localhost:8191/v1"
DEFAULT_TIMEOUT_MS = 60_000  # Solverr's own challenge-solving budget
HTTP_TIMEOUT_S = 75          # local HTTP call budget; > DEFAULT_TIMEOUT_MS


def solverr_available(url: str = SOLVERR_URL, timeout: float = 2.0) -> bool:
    """Cheap reachability check — is a Solverr/FlareSolverr-compatible
    service actually listening on localhost? Call this before relying on
    the tier so a missing container degrades silently instead of stalling
    every request for 75s waiting on a connection that will never come."""
    try:
        req = urllib.request.Request(
            url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        # Any HTTP response (even 4xx for a malformed body) means something
        # is listening and speaking the protocol.
        return True
    except Exception:
        return False


def solverr_fetch(site: str, url: str = SOLVERR_URL,
                   timeout_ms: int = DEFAULT_TIMEOUT_MS) -> tuple[str | None, dict | None, str | None]:
    """Ask Solverr to load `site` and solve any CF/DDoS-GUARD challenge.

    Returns (html, cf_session, error):
      html       — rendered page HTML on success, else None
      cf_session — {'cookies': {name: value}, 'ua': <solving browser's UA>},
                   in the same shape browser.get_cf_session() returns, so
                   this can be handed straight to
                   browser_intercept_and_download(cf_session=...) or
                   ytdlp's --cookies path. None if no cf_clearance cookie
                   came back (e.g. IP-reputation block neither engine cleared).
      error      — human-readable failure reason, else None

    Solverr picks Chrome vs. Camoufox internally; callers don't choose.
    """
    payload = json.dumps({
        "cmd": "request.get",
        "url": site,
        "maxTimeout": timeout_ms,
    }).encode()

    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            body = json.loads(resp.read())
    except urllib.error.URLError as e:
        return None, None, f"Solverr unreachable: {e}"
    except Exception as e:
        return None, None, f"Solverr request failed: {e}"

    if body.get("status") != "ok":
        return None, None, body.get("message", "Solverr returned non-ok status")

    solution = body.get("solution") or {}
    html = solution.get("response")
    cookies_raw = solution.get("cookies") or []
    ua = (solution.get("userAgent")
          or next((c.get("userAgent") for c in cookies_raw if "userAgent" in c), None))

    has_clearance = any(c.get("name") == "cf_clearance" for c in cookies_raw)
    cf_session = {
        "cookies": {c["name"]: c["value"] for c in cookies_raw},
        "ua": ua or "",
    } if has_clearance else None

    return html, cf_session, None
