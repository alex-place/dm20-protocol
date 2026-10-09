#!/usr/bin/env python3
"""Build the `dm` Claude Code plugin in plugin/dm/ from the repository's .claude/ files.

`.claude/` stays the single source of truth for the persona, the /dm commands and the
agent templates. This script copies them into the plugin layout and rewrites the one
thing that differs: commands load the persona from the plugin root instead of the
project's `.claude/` directory.

    python plugin/build_plugin.py          # regenerate plugin/dm/
    python plugin/build_plugin.py --check  # exit 1 if plugin/dm/ is out of date

Hand-written plugin files (hooks/) are not touched.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / ".claude"
PLUGIN = REPO / "plugin" / "dm"

PERSONA_PROJECT = "!`cat .claude/dm-persona.md`"
PERSONA_PLUGIN = '!`cat "${CLAUDE_PLUGIN_ROOT}/dm-persona.md"`'

# Agent templates ship with the plugin but are installed into the project's
# .claude/agents/, because /dm:profile rewrites their model:/effort: lines.
AGENT_TEMPLATES = ("narrator.md", "combat-handler.md", "rules-lookup.md")


def _version() -> str:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("version not found in pyproject.toml")
    return match.group(1)


def _manifest() -> str:
    manifest = {
        "name": "dm",
        "version": _version(),
        "description": (
            "AI Dungeon Master for D&D 5e on the dm20-protocol MCP server: "
            "/dm commands, DM persona, and compaction checkpoint hooks."
        ),
        "author": {"name": "Polloinfilzato"},
        "homepage": "https://github.com/Polloinfilzato/dm20-protocol",
        "repository": "https://github.com/Polloinfilzato/dm20-protocol",
        "license": "MIT",
        "keywords": ["dnd", "dungeon-master", "ttrpg", "mcp"],
    }
    return json.dumps(manifest, indent=2) + "\n"


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n")


def expected_files() -> dict[Path, str]:
    """Map each generated plugin file to its expected content."""
    files: dict[Path, str] = {
        PLUGIN / ".claude-plugin" / "plugin.json": _manifest(),
        PLUGIN / "dm-persona.md": _normalize(
            (SRC / "dm-persona.md").read_text(encoding="utf-8")
        ),
    }
    for command in sorted((SRC / "commands" / "dm").glob("*.md")):
        text = _normalize(command.read_text(encoding="utf-8"))
        files[PLUGIN / "commands" / command.name] = text.replace(PERSONA_PROJECT, PERSONA_PLUGIN)
    for name in AGENT_TEMPLATES:
        files[PLUGIN / "agent-templates" / name] = _normalize(
            (SRC / "agents" / name).read_text(encoding="utf-8")
        )
    return files


def _generated_dirs() -> list[Path]:
    return [PLUGIN / "commands", PLUGIN / "agent-templates"]


def check() -> list[str]:
    """Return a list of human-readable differences (empty when up to date)."""
    problems: list[str] = []
    expected = expected_files()
    for path, content in expected.items():
        if not path.is_file():
            problems.append(f"missing: {path.relative_to(REPO)}")
        elif _normalize(path.read_text(encoding="utf-8")) != content:
            problems.append(f"stale: {path.relative_to(REPO)}")
    for directory in _generated_dirs():
        for path in directory.glob("*.md") if directory.is_dir() else []:
            if path not in expected:
                problems.append(f"unexpected: {path.relative_to(REPO)}")
    return problems


def build() -> None:
    expected = expected_files()
    for directory in _generated_dirs():
        for path in directory.glob("*.md") if directory.is_dir() else []:
            if path not in expected:
                path.unlink()
    for path, content in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="Verify instead of writing.")
    args = parser.parse_args(argv)
    if args.check:
        problems = check()
        for problem in problems:
            print(problem)
        if problems:
            print("plugin/dm is out of date: run `python plugin/build_plugin.py`.")
            return 1
        print("plugin/dm is up to date.")
        return 0
    build()
    print(f"Built {PLUGIN.relative_to(REPO)} from .claude/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
