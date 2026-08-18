"""
update_checker.py
------------------
Checks GitHub Releases for a newer version than the one currently
running, so end users get notified inside the app instead of having to
remember to check for updates themselves.

Deliberately uses only the standard library (urllib, json) rather than
`requests`, so it doesn't add a dependency to the PyInstaller-frozen
.exe just for this one feature.

IMPORTANT: set GITHUB_REPO below to your actual "owner/repo" before
shipping - it's a placeholder until then.
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Optional

from version import __version__ as CURRENT_VERSION

# TODO: replace with your actual GitHub "owner/repo", e.g. "raj123/container-explorer"
GITHUB_REPO = "RajSingh2003/container-file-explorer"

RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"


def _parse_version(version: str):
    """'1.10.2' -> (1, 10, 2), for a correct numeric comparison (so 1.10.0
    is correctly seen as newer than 1.9.0, unlike a plain string compare)."""
    parts = []
    for piece in version.strip().lstrip("v").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str = CURRENT_VERSION) -> bool:
    """True if `latest` represents a newer version than `current`."""
    return _parse_version(latest) > _parse_version(current)


def get_latest_version(timeout: int = 3) -> Optional[str]:
    """Fetch the latest published release's version tag from GitHub, e.g.
    "1.2.0" (the leading 'v' in tags like 'v1.2.0' is stripped). Returns
    None on any failure (offline, repo not found, rate-limited, etc) -
    this is a best-effort convenience check, not something that should
    ever block or error out the app."""
    try:
        req = urllib.request.Request(
            RELEASES_API_URL, headers={"Accept": "application/vnd.github+json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = data.get("tag_name", "")
        return tag.lstrip("v") or None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return None


def check_for_update(timeout: int = 3) -> Optional[str]:
    """Convenience wrapper: returns the latest version string if it's
    newer than what's currently running, else None (either because
    we're already up to date, or the check failed/timed out)."""
    latest = get_latest_version(timeout=timeout)
    if latest and is_newer(latest, CURRENT_VERSION):
        return latest
    return None
