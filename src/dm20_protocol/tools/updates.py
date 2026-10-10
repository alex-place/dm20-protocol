"""
Update-check and release-notes tools.

Split out of ``dm20_protocol.main``; every name defined here is re-exported
from ``dm20_protocol.main``. Shared server state (``storage``, managers, ...)
is read through ``_m`` (the main module) at call time so that tests and
callers that monkeypatch ``dm20_protocol.main.storage`` keep working.
"""

import json
import logging

from .. import main as _m

logger = logging.getLogger("dm20-protocol")
mcp = _m.mcp


# ─── Update Check ────────────────────────────────────────────────────────────

@mcp.tool
def check_for_updates() -> str:
    """Check if a newer version of dm20-protocol is available.

    Compares the installed version with the latest on GitHub.
    Returns update status, current/latest versions, and upgrade command if needed.
    Call this at session start to notify the user about available updates.
    """
    import urllib.request
    import re

    try:
        from importlib.metadata import version as get_version
        current = get_version("dm20-protocol")
    except Exception:
        from dm20_protocol import __version__
        current = __version__

    raw_url = "https://raw.githubusercontent.com/Polloinfilzato/dm20-protocol/main/pyproject.toml"
    try:
        req = urllib.request.Request(raw_url, headers={"User-Agent": "dm20-protocol-update-check"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            content = resp.read().decode("utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
        if not match:
            return json.dumps({"status": "error", "message": "Could not parse version from pyproject.toml"})
        latest = match.group(1)
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Could not check for updates: {e}"})

    # Compare versions (simple tuple comparison)
    def parse_ver(v: str) -> tuple:
        return tuple(int(x) for x in v.split(".") if x.isdigit())

    current_tuple = parse_ver(current)
    latest_tuple = parse_ver(latest)

    if latest_tuple > current_tuple:
        return json.dumps({
            "status": "update_available",
            "current_version": current,
            "latest_version": latest,
            "upgrade_command": 'bash <(curl -fsSL https://raw.githubusercontent.com/Polloinfilzato/dm20-protocol/main/install.sh) --upgrade',
            "message": f"A new version is available: {current} → {latest}",
        })
    else:
        return json.dumps({
            "status": "up_to_date",
            "current_version": current,
            "latest_version": latest,
            "message": f"You are running the latest version ({current}).",
        })


@mcp.tool
def get_release_notes() -> str:
    """Fetch the latest release notes from the CHANGELOG.

    Returns the most recent changelog entries (Unreleased + last released version)
    from the GitHub repository. Use this to show users what's new.
    """
    import urllib.request
    import re

    raw_url = "https://raw.githubusercontent.com/Polloinfilzato/dm20-protocol/main/CHANGELOG.md"
    try:
        req = urllib.request.Request(raw_url, headers={"User-Agent": "dm20-protocol-release-notes"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
    except Exception as e:
        return json.dumps({"status": "error", "message": f"Could not fetch changelog: {e}"})

    # Extract sections: find all ## [version] headers and return the first two
    sections = re.split(r'^(## \[)', content, flags=re.MULTILINE)

    result_parts = []
    # sections[0] is the preamble (title, description)
    # sections[1::2] are the "## [" markers, sections[2::2] are the section bodies
    section_count = 0
    for i in range(1, len(sections), 2):
        if section_count >= 2:
            break
        header = sections[i] + sections[i + 1] if i + 1 < len(sections) else sections[i]
        result_parts.append(header.strip())
        section_count += 1

    if not result_parts:
        return json.dumps({"status": "empty", "message": "No release notes found in CHANGELOG."})

    return json.dumps({
        "status": "ok",
        "notes": "\n\n".join(result_parts),
    })
