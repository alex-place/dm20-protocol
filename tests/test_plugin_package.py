"""The Claude Code plugin in plugin/ must stay in sync with .claude/ and be well-formed."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugin" / "dm"


@pytest.fixture(scope="module")
def build_plugin():
    spec = importlib.util.spec_from_file_location("build_plugin", REPO / "plugin" / "build_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_is_up_to_date(build_plugin):
    assert build_plugin.check() == [], "run `python plugin/build_plugin.py`"


def test_manifests_are_valid():
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert re.fullmatch(r"[a-z][a-z0-9]*(-[a-z0-9]+)*", plugin["name"])
    market = json.loads((REPO / "plugin" / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    entry = next(p for p in market["plugins"] if p["name"] == plugin["name"])
    assert (REPO / "plugin" / entry["source"]).resolve() == PLUGIN.resolve()


def test_hooks_reference_existing_scripts():
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"PreCompact", "SessionStart"}
    assert hooks["SessionStart"][0]["matcher"] == "compact"
    for groups in hooks.values():
        for group in groups:
            for hook in group["hooks"]:
                script = re.search(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\"]+)", hook["command"]).group(1)
                assert (PLUGIN / script).is_file(), script


def test_commands_load_persona_from_plugin_root():
    for command in (PLUGIN / "commands").glob("*.md"):
        text = command.read_text(encoding="utf-8")
        assert "cat .claude/dm-persona.md" not in text, command.name


def test_agent_templates_carry_model_and_effort():
    for agent in (REPO / ".claude" / "agents").glob("*.md"):
        frontmatter = agent.read_text(encoding="utf-8").split("---")[1]
        assert re.search(r"^model: (opus|sonnet|haiku)$", frontmatter, re.MULTILINE), agent.name
        assert re.search(r"^effort: (low|medium|high|xhigh|max)$", frontmatter, re.MULTILINE), agent.name
