"""
Claudmaster (AI DM) configuration, session and player-action tools.

Split out of ``dm20_protocol.main``; every name defined here is re-exported
from ``dm20_protocol.main``. Shared server state (``storage``, managers, ...)
is read through ``_m`` (the main module) at call time so that tests and
callers that monkeypatch ``dm20_protocol.main.storage`` keep working.
"""

import logging
from typing import Annotated, Literal
from pydantic import Field

from .. import main as _m

logger = logging.getLogger("dm20-protocol")
mcp = _m.mcp


# ----------------------------------------------------------------------
# Claudmaster Configuration Tools
# ----------------------------------------------------------------------

def _configure_claudmaster_impl(
    storage_ref,
    llm_model=None,
    temperature=None,
    max_tokens=None,
    narrative_style=None,
    dialogue_style=None,
    difficulty=None,
    improvisation_level=None,
    agent_timeout=None,
    fudge_rolls=None,
    model_profile=None,
    interaction_mode=None,
    reset_to_defaults=False,
) -> str:
    """Implementation for configure_claudmaster (testable without MCP wrapper)."""
    from dm20_protocol.claudmaster.config import ClaudmasterConfig
    from dm20_protocol.claudmaster.profiles import (
        apply_profile, update_agent_files, resolve_agents_dir,
        get_profile_summary, CC_RECOMMENDATIONS,
    )

    if not storage_ref._current_campaign:
        return "No active campaign. Load or create a campaign first."

    if reset_to_defaults:
        config = ClaudmasterConfig()
        storage_ref.save_claudmaster_config(config)
        # Also reset agent files to balanced defaults
        updated_agents = update_agent_files("balanced")
        # Also reset interaction mode to classic
        storage_ref.set_interaction_mode("classic")
        header = "Claudmaster Configuration Reset to Defaults (interaction mode: classic)"
        if updated_agents:
            header += f" (agents updated: {', '.join(updated_agents)})"
        return _format_claudmaster_config(config, header=header, interaction_mode="classic")

    # ── Interaction mode switch (orthogonal to model profile) ──
    if interaction_mode is not None:
        valid_modes = ("classic", "narrated", "immersive")
        if interaction_mode not in valid_modes:
            return f"❌ Invalid interaction_mode '{interaction_mode}'. Must be one of: {', '.join(valid_modes)}"

        # Check voice dependencies for non-classic modes
        if interaction_mode in ("narrated", "immersive"):
            try:
                from dm20_protocol.voice import TTSRouter  # noqa: F811
            except ImportError:
                return (
                    f"❌ Cannot switch to '{interaction_mode}' mode: voice dependencies not installed.\n"
                    "Run: pip install dm20-protocol[voice]"
                )

        try:
            storage_ref.set_interaction_mode(interaction_mode)
        except ValueError as e:
            return f"❌ {e}"

        mode_labels = {"classic": "text-only", "narrated": "TTS + text", "immersive": "TTS + STT"}
        return (
            f"🔄 **Interaction Mode Changed:** {interaction_mode} ({mode_labels[interaction_mode]})\n\n"
            f"Takes effect immediately. Model profile unchanged ({storage_ref.get_claudmaster_config().model_profile})."
        )

    config = storage_ref.get_claudmaster_config()

    # ── Profile switch: apply preset then update agent files ──
    if model_profile is not None:
        try:
            config = apply_profile(config, model_profile)
        except ValueError as e:
            return f"Configuration error: {e}"

        # Update CC agent .md files
        updated_agents = update_agent_files(model_profile)

        storage_ref.save_claudmaster_config(config)

        # Build rich output
        lines = [_format_claudmaster_config(config, header=f"Profile Applied: {model_profile.upper()}", interaction_mode=storage_ref.interaction_mode)]
        if updated_agents:
            lines.append("")
            lines.append(f"**CC Agent files updated:** {', '.join(a + '.md' for a in updated_agents)}")
        else:
            agents_dir = resolve_agents_dir()
            if agents_dir is None:
                lines.append("")
                lines.append("**Note:** Could not find .claude/agents/ directory. "
                             "Agent files were not updated. Set DM20_AGENTS_DIR env var to fix this.")

        rec = CC_RECOMMENDATIONS.get(model_profile, {})
        if rec:
            lines.append("")
            lines.append(f"**Tip:** Run `/model {rec['model']}` in Claude Code to match this profile.")
            lines.append(f"  {rec['description']}")

        return "\n".join(lines)

    # ── Individual field updates ──
    updates: dict = {}
    if llm_model is not None:
        updates["llm_model"] = llm_model
    if temperature is not None:
        updates["temperature"] = temperature
    if max_tokens is not None:
        updates["max_tokens"] = max_tokens
    if narrative_style is not None:
        updates["narrative_style"] = narrative_style
    if dialogue_style is not None:
        updates["dialogue_style"] = dialogue_style
    if difficulty is not None:
        updates["difficulty"] = difficulty
    if improvisation_level is not None:
        updates["improvisation_level"] = improvisation_level
    if agent_timeout is not None:
        updates["agent_timeout"] = agent_timeout
    if fudge_rolls is not None:
        updates["fudge_rolls"] = fudge_rolls

    if not updates:
        return _format_claudmaster_config(config, header="Claudmaster Configuration (Current)", interaction_mode=storage_ref.interaction_mode)

    # If user changed a model field individually, mark profile as "custom"
    model_fields = {"llm_model", "narrator_model", "arbiter_model",
                    "narrator_max_tokens", "arbiter_max_tokens", "max_tokens",
                    "temperature", "narrator_temperature", "arbiter_temperature",
                    "effort", "narrator_effort", "arbiter_effort"}
    if updates.keys() & model_fields:
        updates["model_profile"] = "custom"

    try:
        merged = config.model_dump()
        merged.update(updates)
        config = ClaudmasterConfig.model_validate(merged)
    except Exception as e:
        return f"Configuration error: {e}"

    storage_ref.save_claudmaster_config(config)
    changed = ", ".join(updates.keys())
    return _format_claudmaster_config(config, header=f"Claudmaster Configuration Updated ({changed})", interaction_mode=storage_ref.interaction_mode)


@mcp.tool
def configure_claudmaster(
    llm_model: Annotated[str | None, Field(description="LLM model identifier (e.g., 'claude-opus-5-5')")] = None,
    temperature: Annotated[float | None, Field(description="Deprecated and ignored: current Claude models reject sampling parameters. Use effort instead.")] = None,
    max_tokens: Annotated[int | None, Field(description="Maximum tokens in LLM response (256-200000)")] = None,
    narrative_style: Annotated[str | None, Field(description="Narrative style: descriptive, concise, dramatic, cinematic, etc.")] = None,
    dialogue_style: Annotated[str | None, Field(description="Dialogue style: natural, theatrical, formal, casual, etc.")] = None,
    difficulty: Annotated[Literal["easy", "normal", "hard", "deadly"] | None, Field(description="Game difficulty")] = None,
    improvisation_level: Annotated[int | None, Field(description="AI improvisation level: 0=None, 1=Low, 2=Medium, 3=High, 4=Full")] = None,
    agent_timeout: Annotated[float | None, Field(description="Maximum seconds per agent call (> 0)")] = None,
    fudge_rolls: Annotated[bool | None, Field(description="Whether DM can fudge dice rolls for narrative purposes")] = None,
    model_profile: Annotated[Literal["quality", "balanced", "economy"] | None, Field(description="Switch model quality profile. Updates all model settings and CC agent files at once.")] = None,
    interaction_mode: Annotated[Literal["classic", "narrated", "immersive"] | None, Field(description="Switch interaction mode: 'classic' (text-only), 'narrated' (TTS + text), 'immersive' (TTS + STT). Takes effect immediately.")] = None,
    reset_to_defaults: Annotated[bool, Field(description="Reset all settings to defaults")] = False,
) -> str:
    """Configure the Claudmaster AI DM settings for the current campaign.

    Call with no arguments to view current configuration.
    Provide specific fields to update only those settings (partial update).
    Set model_profile to switch all model settings at once (quality/balanced/economy).
    Set interaction_mode to switch how the DM communicates (independent of model_profile).
    Set reset_to_defaults=True to restore all settings to their default values.
    """
    return _configure_claudmaster_impl(
        _m.storage, llm_model=llm_model, temperature=temperature, max_tokens=max_tokens,
        narrative_style=narrative_style, dialogue_style=dialogue_style, difficulty=difficulty,
        improvisation_level=improvisation_level, agent_timeout=agent_timeout,
        fudge_rolls=fudge_rolls, model_profile=model_profile,
        interaction_mode=interaction_mode, reset_to_defaults=reset_to_defaults,
    )


# Claudmaster session management tools
from ..claudmaster.tools.session_tools import (
    start_claudmaster_session as _start_claudmaster_session,
    end_session as _end_session,
    get_session_state as _get_session_state,
    set_storage as _set_session_storage,
)
_set_session_storage(_m.storage)


@mcp.tool
async def start_claudmaster_session(
    campaign_name: Annotated[str, Field(description="Name of the campaign to play")],
    module_id: Annotated[str | None, Field(description="Optional D&D module to load")] = None,
    session_id: Annotated[str | None, Field(description="Session ID to resume (required if resume=True)")] = None,
    resume: Annotated[bool, Field(description="Whether to resume an existing session")] = False,
) -> dict:
    """Start or resume a Claudmaster AI DM session."""
    return await _start_claudmaster_session(
        campaign_name=campaign_name,
        module_id=module_id,
        session_id=session_id,
        resume=resume,
    )


@mcp.tool
async def end_claudmaster_session(
    session_id: Annotated[str, Field(description="The session ID to end or pause")],
    mode: Annotated[str, Field(description="'pause' to save for later, 'end' for final termination")] = "pause",
    summary_notes: Annotated[str | None, Field(description="Optional DM notes to save with the session")] = None,
    campaign_path: Annotated[str | None, Field(description="Optional path for disk persistence")] = None,
) -> dict:
    """End or pause a Claudmaster AI DM session, saving all state."""
    return await _end_session(
        session_id=session_id,
        mode=mode,
        summary_notes=summary_notes,
        campaign_path=campaign_path,
    )


@mcp.tool
async def get_claudmaster_session_state(
    session_id: Annotated[str, Field(description="The session ID to query")],
    detail_level: Annotated[str, Field(description="Detail level: 'minimal', 'standard', or 'full'")] = "standard",
    include_history: Annotated[bool, Field(description="Whether to include action history")] = True,
    history_limit: Annotated[int, Field(description="Max number of history entries to return")] = 10,
) -> dict:
    """Get the current state of a Claudmaster AI DM session."""
    return await _get_session_state(
        session_id=session_id,
        detail_level=detail_level,
        include_history=include_history,
        history_limit=history_limit,
    )


# Claudmaster player action tool
from ..claudmaster.tools.action_tools import player_action as _player_action


@mcp.tool
async def player_action(
    session_id: Annotated[str, Field(description="The active session ID to process the action in")],
    action: Annotated[str, Field(description="The player's action as natural language text")],
    character_name: Annotated[str | None, Field(description="Optional name of the character performing the action")] = None,
    context: Annotated[str | None, Field(description="Optional additional context about the action")] = None,
) -> dict:
    """Process a player action in the current Claudmaster session."""
    return await _player_action(
        session_id=session_id,
        action=action,
        character_name=character_name,
        context=context,
    )


def _format_claudmaster_config(config, header: str = "Claudmaster Configuration", interaction_mode: str | None = None) -> str:
    """Format ClaudmasterConfig as a readable string."""
    improv_labels = {"none": "None", "low": "Low", "medium": "Medium", "high": "High", "full": "Full"}
    improv_index = {"none": 0, "low": 1, "medium": 2, "high": 3, "full": 4}
    level_value = config.improvisation_level.value if hasattr(config.improvisation_level, 'value') else str(config.improvisation_level)
    improv_display = improv_labels.get(level_value, str(config.improvisation_level))
    improv_num = improv_index.get(level_value, "?")

    profile_display = getattr(config, "model_profile", "balanced").upper()
    mode_labels = {"classic": "text-only", "narrated": "TTS + text", "immersive": "TTS + STT"}

    lines = [
        f"**{header}**",
        "",
        f"**Model Profile:** {profile_display}",
    ]
    if interaction_mode:
        lines.append(f"**Interaction Mode:** {interaction_mode} ({mode_labels.get(interaction_mode, interaction_mode)})")
    lines += [
        "",
        "**LLM Settings:**",
        f"  Provider: {config.llm_provider}",
        f"  Main Model: {config.llm_model}",
        f"  Narrator Model: {config.narrator_model}",
        f"  Arbiter Model: {config.arbiter_model}",
        f"  Effort (main/narrator/arbiter): {config.effort or 'none'}/{config.narrator_effort or 'none'}/{config.arbiter_effort or 'none'}",
        f"  Max Tokens (main/narrator/arbiter): {config.max_tokens}/{config.narrator_max_tokens}/{config.arbiter_max_tokens}",
        "",
        "**Narrative Settings:**",
        f"  Style: {config.narrative_style}",
        f"  Dialogue: {config.dialogue_style}",
        "",
        "**Game Settings:**",
        f"  Difficulty: {config.difficulty}",
        f"  Fudge Rolls: {'enabled' if config.fudge_rolls else 'disabled'}",
        "",
        "**Agent Settings:**",
        f"  Improvisation Level: {improv_display} ({improv_num}/4)",
        f"  Agent Timeout: {config.agent_timeout}s",
        "",
        "**Intent Classification:**",
        f"  Ambiguity Threshold: {config.ambiguity_threshold}",
        f"  Fallback Confidence: {config.fallback_confidence}",
    ]

    if config.house_rules:
        lines.append("")
        lines.append("**House Rules:**")
        for key, value in config.house_rules.items():
            lines.append(f"  {key}: {value}")

    return "\n".join(lines)
