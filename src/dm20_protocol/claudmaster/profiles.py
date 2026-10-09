"""
Model quality profiles for Claudmaster AI DM.

Provides switchable preset configurations so users can trade quality vs
token cost mid-session. Three built-in tiers (Claude 5.5 generation):

- **quality**  — Opus 5.5 at high effort for narrator and arbiter
- **balanced** — Opus 5.5 at medium effort (the default)
- **economy**  — Sonnet 5.5: narrator at low effort, arbiter at medium

Opus 5.5's price and its ``low`` effort level make "Opus at low effort" a
better economy trade than a smaller model for the Claude Code DM agents, so
every profile keeps the DM/narrator/combat-handler agents on Opus and only
varies their effort. The arbiter always sits at or above narrator effort
because rules mistakes cost more than flat prose.

Each profile sets both the Python-side config (ClaudmasterConfig fields)
and the Claude Code agent file frontmatter (``model:`` and ``effort:``).
Sampling parameters (temperature) are no longer part of any profile:
Claude 5.5 models reject them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from .model_ids import HAIKU, OPUS, SONNET

if TYPE_CHECKING:
    from .config import ClaudmasterConfig

# ── Profile Definitions ────────────────────────────────────────────────────

VALID_PROFILES = {"quality", "balanced", "economy", "custom"}

# max_tokens include thinking (always on for Claude 5.5 models).
MODEL_PROFILES: dict[str, dict] = {
    "quality": {
        "model_profile": "quality",
        "llm_model": OPUS,
        "narrator_model": OPUS,
        "arbiter_model": OPUS,
        "prefetch_model": HAIKU,
        "effort": "high",
        "narrator_effort": "high",
        "arbiter_effort": "high",
        "prefetch_effort": "low",
        "narrator_max_tokens": 16000,
        "arbiter_max_tokens": 32000,
        "max_tokens": 32000,
    },
    "balanced": {
        "model_profile": "balanced",
        "llm_model": OPUS,
        "narrator_model": OPUS,
        "arbiter_model": OPUS,
        "prefetch_model": HAIKU,
        "effort": "medium",
        "narrator_effort": "medium",
        "arbiter_effort": "medium",
        "prefetch_effort": "low",
        "narrator_max_tokens": 8000,
        "arbiter_max_tokens": 16000,
        "max_tokens": 16000,
    },
    "economy": {
        "model_profile": "economy",
        "llm_model": SONNET,
        "narrator_model": SONNET,
        "arbiter_model": SONNET,
        "prefetch_model": HAIKU,
        "effort": "low",
        "narrator_effort": "low",
        "arbiter_effort": "medium",
        "prefetch_effort": "low",
        "narrator_max_tokens": 4096,
        "arbiter_max_tokens": 8192,
        "max_tokens": 8192,
    },
}

# Maps profile -> CC agent frontmatter (model alias + effort) for
# `.claude/agents/*.md`. Claude Code agent frontmatter supports `effort:`.
AGENT_MODEL_MAP: dict[str, dict[str, dict[str, str]]] = {
    "quality": {
        "narrator": {"model": "opus", "effort": "high"},
        "combat-handler": {"model": "opus", "effort": "high"},
        "rules-lookup": {"model": "sonnet", "effort": "medium"},
    },
    "balanced": {
        "narrator": {"model": "opus", "effort": "medium"},
        "combat-handler": {"model": "opus", "effort": "medium"},
        "rules-lookup": {"model": "haiku", "effort": "low"},
    },
    "economy": {
        "narrator": {"model": "opus", "effort": "low"},
        "combat-handler": {"model": "opus", "effort": "low"},
        "rules-lookup": {"model": "haiku", "effort": "low"},
    },
}

# Recommended CC main model (the DM) for display purposes.
CC_RECOMMENDATIONS: dict[str, dict[str, str]] = {
    "quality": {
        "model": "opus",
        "effort": "high",
        "description": "Opus 5.5 at high effort -- best narrative depth and rules care",
    },
    "balanced": {
        "model": "opus",
        "effort": "medium",
        "description": "Opus 5.5 at medium effort -- the recommended default",
    },
    "economy": {
        "model": "opus",
        "effort": "low",
        "description": "Opus 5.5 at low effort -- cheaper and faster than a smaller model at equal quality",
    },
}


# ── Core Functions ──────────────────────────────────────────────────────────


def apply_profile(config: "ClaudmasterConfig", profile_name: str) -> "ClaudmasterConfig":
    """Merge a profile preset into the given config, preserving non-model fields.

    Returns a new ClaudmasterConfig instance (does not mutate the original).

    Raises:
        ValueError: If *profile_name* is not a recognised built-in profile.
    """
    if profile_name not in MODEL_PROFILES:
        raise ValueError(
            f"Unknown profile '{profile_name}'. "
            f"Valid profiles: {', '.join(sorted(MODEL_PROFILES))}"
        )

    merged = config.model_dump()
    merged.update(MODEL_PROFILES[profile_name])

    from .config import ClaudmasterConfig as _Cfg
    return _Cfg.model_validate(merged)


def get_profile_summary(profile_name: str) -> str:
    """Return a human-readable summary of a profile's settings."""
    if profile_name not in MODEL_PROFILES:
        return f"Unknown profile: {profile_name}"

    p = MODEL_PROFILES[profile_name]
    rec = CC_RECOMMENDATIONS[profile_name]
    agents = AGENT_MODEL_MAP[profile_name]

    effort_main = p.get("effort", "none")
    effort_narrator = p.get("narrator_effort", "none")
    effort_arbiter = p.get("arbiter_effort", "none")

    lines = [
        f"**Profile: {profile_name.upper()}**",
        "",
        f"  LLM model: {p['llm_model']}",
        f"  Effort (main/narrator/arbiter): {effort_main}/{effort_narrator}/{effort_arbiter}",
        f"  Narrator model: {p['narrator_model']} (effort {effort_narrator})",
        f"  Arbiter model: {p['arbiter_model']} (effort {effort_arbiter})",
        f"  Prefetch model: {p['prefetch_model']} (effort {p['prefetch_effort']})",
        f"  Max tokens (narrator/arbiter/main): "
        f"{p['narrator_max_tokens']}/{p['arbiter_max_tokens']}/{p['max_tokens']}",
        "",
        "  CC Agent files:",
    ]
    for agent_name in ("narrator", "combat-handler", "rules-lookup"):
        entry = agents[agent_name]
        lines.append(
            f"    {agent_name}.md -> model: {entry['model']}, effort: {entry['effort']}"
        )
    lines += [
        "",
        f"  Recommended CC main model: /model {rec['model']} (effort {rec['effort']})",
        f"    {rec['description']}",
    ]
    return "\n".join(lines)


# ── Agent File Resolution & Update ──────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"\A(\ufeff?---\r?\n)(.*?)(\r?\n---)", re.DOTALL)
_MODEL_LINE_RE = re.compile(r"^model:[^\r\n]*", re.MULTILINE)
_EFFORT_LINE_RE = re.compile(r"^effort:[^\r\n]*", re.MULTILINE)


def set_frontmatter_fields(content: str, model: str, effort: str) -> str | None:
    """Return *content* with ``model:`` and ``effort:`` set in its YAML frontmatter.

    ``effort:`` is inserted directly after ``model:`` when missing (or at the
    end of the frontmatter if there is no ``model:`` line either). Returns
    ``None`` when the file has no frontmatter block.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return None
    opener, body, closer = match.group(1), match.group(2), match.group(3)
    newline = "\r\n" if "\r\n" in opener else "\n"

    if _MODEL_LINE_RE.search(body):
        body = _MODEL_LINE_RE.sub(f"model: {model}", body, count=1)
    else:
        body = body + f"{newline}model: {model}"

    if _EFFORT_LINE_RE.search(body):
        body = _EFFORT_LINE_RE.sub(f"effort: {effort}", body, count=1)
    else:
        body = _MODEL_LINE_RE.sub(
            lambda m: f"{m.group(0)}{newline}effort: {effort}", body, count=1
        )

    return opener + body + closer + content[match.end():]


def resolve_agents_dir() -> Path | None:
    """Find the `.claude/agents/` directory using the standard resolution order.

    1. ``DM20_AGENTS_DIR`` env var (explicit override set by installer)
    2. Auto-discover: walk up from this file looking for ``.claude/agents/``
    3. ``DM20_STORAGE_DIR/../.claude/agents/`` (relative to play directory)

    Returns ``None`` if no agents directory can be found.
    """
    # 1. Explicit env var
    env_dir = os.environ.get("DM20_AGENTS_DIR")
    if env_dir:
        p = Path(env_dir)
        if p.is_dir():
            return p

    # 2. Walk up from this file
    current = Path(__file__).resolve().parent
    for _ in range(10):  # safety limit
        candidate = current / ".claude" / "agents"
        if candidate.is_dir():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent

    # 3. Relative to DM20_STORAGE_DIR
    storage_dir = os.environ.get("DM20_STORAGE_DIR")
    if storage_dir:
        candidate = Path(storage_dir).parent / ".claude" / "agents"
        if candidate.is_dir():
            return candidate

    return None


def update_agent_files(profile_name: str, agents_dir: Path | None = None) -> list[str]:
    """Update the ``model:`` and ``effort:`` frontmatter in CC agent .md files.

    ``effort:`` is inserted after ``model:`` when the file lacks it.

    Args:
        profile_name: One of 'quality', 'balanced', 'economy'.
        agents_dir: Path to the ``.claude/agents/`` directory.
            If ``None``, uses :func:`resolve_agents_dir`.

    Returns:
        List of agent names whose files were successfully updated.
    """
    if profile_name not in AGENT_MODEL_MAP:
        raise ValueError(f"Unknown profile '{profile_name}'")

    if agents_dir is None:
        agents_dir = resolve_agents_dir()
    if agents_dir is None:
        return []

    mapping = AGENT_MODEL_MAP[profile_name]
    updated: list[str] = []

    for agent_name, target in mapping.items():
        agent_file = agents_dir / f"{agent_name}.md"
        if not agent_file.is_file():
            continue

        content = agent_file.read_bytes().decode("utf-8")
        new_content = set_frontmatter_fields(content, target["model"], target["effort"])
        if new_content is None:
            continue
        if new_content != content:
            agent_file.write_bytes(new_content.encode("utf-8"))
        updated.append(agent_name)

    return updated


__all__ = [
    "VALID_PROFILES",
    "MODEL_PROFILES",
    "AGENT_MODEL_MAP",
    "CC_RECOMMENDATIONS",
    "apply_profile",
    "get_profile_summary",
    "resolve_agents_dir",
    "update_agent_files",
    "set_frontmatter_fields",
]
