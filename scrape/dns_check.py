"""DNS preflight: verify the system resolver isn't broken before doing
anything network-dependent. Fail-open — never blocks the CLI, only warns.

If DNS is broken and we fix it, the original DNS is saved and restored
at process exit via atexit so we don't permanently alter the user's config.
"""

from __future__ import annotations

import atexit
import platform
import socket
import subprocess

CLOUDFLARE_DNS = "1.1.1.1"
_PROBE_HOST    = "one.one.one.one"
_original_dns: str | None = None   # saved before we change anything


def _dns_working() -> bool:
    try:
        socket.gethostbyname(_PROBE_HOST)
        return True
    except OSError:
        return False


# ── Windows helpers ───────────────────────────────────────────────────────────
def _get_windows_iface() -> str | None:
    try:
        out = subprocess.run(
            ["netsh", "interface", "show", "interface"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            if "Connected" in line and "Loopback" not in line:
                return line.split()[-1]
    except Exception:
        pass
    return None


def _get_windows_dns(iface: str) -> str | None:
    """Return the current primary DNS for iface, or None if unreadable."""
    try:
        out = subprocess.run(
            ["netsh", "interface", "ip", "show", "dns", iface],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            # Line looks like: "DNS Servers:      192.168.1.1"
            # or just an IP on its own continuation line
            if ":" in line:
                parts = line.split(":")
                candidate = parts[-1].strip()
            else:
                candidate = line.strip()
            # Validate it looks like an IP
            try:
                socket.inet_aton(candidate)
                return candidate
            except OSError:
                pass
    except Exception:
        pass
    return None


def _set_windows_dns(iface: str, dns: str) -> bool:
    try:
        subprocess.run(
            ["netsh", "interface", "ip", "set", "dns", iface, "static", dns],
            capture_output=True, timeout=5, check=True,
        )
        return True
    except Exception:
        return False


def _restore_windows_dns(iface: str, original: str) -> None:
    """Restore DNS to original value (or DHCP if original was a private addr)."""
    try:
        if original in ("0.0.0.0", ""):
            subprocess.run(
                ["netsh", "interface", "ip", "set", "dns", iface, "dhcp"],
                capture_output=True, timeout=5,
            )
        else:
            _set_windows_dns(iface, original)
        print(f"[dns] Restored DNS → {original or 'DHCP'}")
    except Exception:
        pass


# ── Linux helpers ─────────────────────────────────────────────────────────────
def _get_linux_dns() -> str | None:
    try:
        with open("/etc/resolv.conf") as f:
            for line in f:
                line = line.strip()
                if line.startswith("nameserver"):
                    return line.split()[-1]
    except OSError:
        pass
    return None


def _set_linux_dns(dns: str) -> bool:
    try:
        with open("/etc/resolv.conf", "w") as f:
            f.write(f"nameserver {dns}\n")
        return True
    except OSError:
        return False


# ── Public entry point ────────────────────────────────────────────────────────
def ensure_dns() -> None:
    """Check the resolver; if broken, switch to Cloudflare 1.1.1.1.
    Saves the original DNS and registers an atexit hook to restore it.
    Never raises — worst case warns and lets the pipeline fail on its own.
    """
    global _original_dns

    if _dns_working():
        return

    system = platform.system()
    print(f"[dns] Resolution failed — switching to {CLOUDFLARE_DNS} for this session...")

    if system == "Windows":
        iface = _get_windows_iface()
        if not iface:
            print("[dns] Could not detect network interface — continuing anyway.")
            return
        _original_dns = _get_windows_dns(iface)
        fixed = _set_windows_dns(iface, CLOUDFLARE_DNS)
        if fixed and _dns_working():
            print(f"[dns] DNS → {CLOUDFLARE_DNS}  (was: {_original_dns or 'unknown'})")
            # Restore at exit
            atexit.register(_restore_windows_dns, iface, _original_dns or "")
        else:
            print("[dns] Could not fix DNS (needs admin) — continuing anyway.")
    else:
        _original_dns = _get_linux_dns()
        fixed = _set_linux_dns(CLOUDFLARE_DNS)
        if fixed and _dns_working():
            print(f"[dns] DNS → {CLOUDFLARE_DNS}  (was: {_original_dns or 'unknown'})")
            atexit.register(_set_linux_dns, _original_dns or "8.8.8.8")
        else:
            print("[dns] Could not fix DNS (needs root) — continuing anyway.")
