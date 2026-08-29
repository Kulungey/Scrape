"""DNS preflight: verify the system resolver isn't broken before doing
anything network-dependent. Fail-open — never blocks the CLI, only warns.
"""

from __future__ import annotations

import platform
import socket
import subprocess

CLOUDFLARE_DNS = "1.1.1.1"
_PROBE_HOST = "one.one.one.one"


def _dns_working() -> bool:
    try:
        socket.gethostbyname(_PROBE_HOST)
        return True
    except OSError:
        return False


def _set_windows_dns() -> bool:
    try:
        iface_out = subprocess.run(
            ["netsh", "interface", "show", "interface"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        iface = None
        for line in iface_out.splitlines():
            if "Connected" in line and "Loopback" not in line:
                iface = line.split()[-1]
                break
        if not iface:
            return False
        subprocess.run(
            ["netsh", "interface", "ip", "set", "dns", iface, "static", CLOUDFLARE_DNS],
            capture_output=True, timeout=5, check=True,
        )
        return True
    except Exception:
        return False


def _set_linux_dns() -> bool:
    try:
        with open("/etc/resolv.conf", "w") as f:
            f.write(f"nameserver {CLOUDFLARE_DNS}\n")
        return True
    except OSError:
        return False


def ensure_dns() -> None:
    """Check the resolver works; if not, try switching to Cloudflare DNS.
    Never raises — worst case it warns and lets the rest of the pipeline
    fail with its own, more specific errors."""
    if _dns_working():
        return

    print(f"[dns] Resolution failed — attempting to switch to {CLOUDFLARE_DNS}...")
    system = platform.system()
    fixed = _set_windows_dns() if system == "Windows" else _set_linux_dns()

    if fixed and _dns_working():
        print(f"[dns] DNS fixed -> {CLOUDFLARE_DNS}")
    else:
        print("[dns] Could not fix DNS automatically (needs admin/root) — continuing anyway.")
