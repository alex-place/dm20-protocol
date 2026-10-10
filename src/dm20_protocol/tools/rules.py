"""
Rulebook tools: loading/unloading rulebooks, rules search and class/race/spell/monster lookups.

Split out of ``dm20_protocol.main``; every name defined here is re-exported
from ``dm20_protocol.main``. Shared server state (``storage``, managers, ...)
is read through ``_m`` (the main module) at call time so that tests and
callers that monkeypatch ``dm20_protocol.main.storage`` keep working.
"""

import logging
from pathlib import Path
from typing import Annotated, Literal
from pydantic import Field

from ..rulebooks.sources.srd import SRDSource
from ..rulebooks.sources.custom import CustomSource
from ..rulebooks.validators import CharacterValidator

from .. import main as _m

logger = logging.getLogger("dm20-protocol")
mcp = _m.mcp


# ----------------------------------------------------------------------
# Rulebook Management Tools
# ----------------------------------------------------------------------

@mcp.tool
async def load_rulebook(
    source: Annotated[
        Literal["srd", "custom", "open5e", "5etools"],
        Field(description="Source type: 'srd' for official D&D 5e SRD, 'custom' for local files, 'open5e' for Open5e API, '5etools' for 5etools JSON data")
    ],
    version: Annotated[
        str | None,
        Field(description="SRD version: '2014' (default) or '2024'. Ignored for custom sources.")
    ] = "2014",
    path: Annotated[
        str | None,
        Field(description="Path to custom rulebook file (JSON). Required for custom sources.")
    ] = None,
    sources: Annotated[
        list[str] | None,
        Field(description="5etools only: allowlist of source abbreviations, e.g. ['XPHB','XMM','EGW']. Default: all.")
    ] = None,
    exclude_sources: Annotated[
        list[str] | None,
        Field(description="5etools only: source abbreviations to skip, e.g. ['UA']")
    ] = None,
    edition: Annotated[
        Literal["2014", "2024"] | None,
        Field(description="5etools only: preferred edition when an entry has a 2014 and a 2024 version. Default: 2024.")
    ] = None,
    refresh: Annotated[
        bool,
        Field(description="5etools only: revalidate the download cache (ETag) before loading")
    ] = False,
) -> str:
    """Load a rulebook into the current campaign.

    SRD loads are tolerant per category: missing endpoints are skipped and listed
    in the result. 5etools loads can be filtered by source and edition.
    """
    if not _m.storage._current_campaign:
        return "❌ No campaign loaded. Use `load_campaign` first."

    # Initialize manager if not exists
    if not _m.storage.rulebook_manager:
        from ..rulebooks import RulebookManager
        campaign_dir = _m.storage._split_backend._get_campaign_dir(_m.storage._current_campaign.name)
        _m.storage._rulebook_manager = RulebookManager(campaign_dir)

    if source == "srd":
        srd_source = SRDSource(version=version or "2014", cache_dir=_m.storage.rulebook_cache_dir)
        await _m.storage.rulebook_manager.load_source(srd_source)
        counts = srd_source.content_counts()
        result = f"✅ Loaded SRD {version} rulebook\n📚 {counts.classes} classes, {counts.races} races, {counts.spells} spells, {counts.monsters} monsters"
        if getattr(srd_source, "load_report", None):
            result += f"\n🧾 {srd_source.report_text()}"
        return result

    elif source == "open5e":
        from ..rulebooks.sources.open5e import Open5eSource
        open5e_source = Open5eSource(cache_dir=_m.storage.rulebook_cache_dir)
        await _m.storage.rulebook_manager.load_source(open5e_source)
        counts = open5e_source.content_counts()
        return f"✅ Loaded Open5e rulebook\n📚 {counts.classes} classes, {counts.races} races, {counts.spells} spells, {counts.monsters} monsters"

    elif source == "5etools":
        from ..rulebooks.sources.fivetools import FiveToolsSource
        fivetools_source = FiveToolsSource(
            cache_dir=_m.storage.rulebook_cache_dir,
            sources=sources,
            exclude_sources=exclude_sources,
            edition=edition or "2024",
            refresh=refresh,
        )
        await _m.storage.rulebook_manager.load_source(fivetools_source)
        counts = fivetools_source.content_counts()
        filters = f"edition {fivetools_source.edition}"
        if fivetools_source.allowed_sources:
            filters += f", sources {', '.join(sorted(fivetools_source.allowed_sources))}"
        if fivetools_source.excluded_sources:
            filters += f", excluding {', '.join(sorted(fivetools_source.excluded_sources))}"
        return (
            f"✅ Loaded 5etools rulebook ({filters})\n"
            f"📚 {counts.classes} classes, {counts.subclasses} subclasses, {counts.races} races, "
            f"{counts.spells} spells, {counts.monsters} monsters, {counts.items} items, "
            f"{counts.feats} feats, {counts.optional_features} optional features"
        )

    elif source == "custom":
        if not path:
            return "❌ Custom source requires 'path' parameter"
        full_path = _m.storage.rulebooks_dir / path if _m.storage.rulebooks_dir else Path(path)
        custom_source = CustomSource(full_path)
        await _m.storage.rulebook_manager.load_source(custom_source)
        counts = custom_source.content_counts()
        return f"✅ Loaded custom rulebook: {path}\n📚 {counts.classes} classes, {counts.races} races, {counts.spells} spells"

    return "❌ Invalid source type. Use 'srd', 'custom', 'open5e', or '5etools'."

@mcp.tool
def list_rulebooks() -> str:
    """List all active rulebooks in the current campaign."""
    if not _m.storage._current_campaign:
        return "❌ No campaign loaded."

    if not _m.storage.rulebook_manager or not _m.storage.rulebook_manager.sources:
        return "📚 No rulebooks loaded. Use `load_rulebook` to add one."

    rulebooks = []
    for source_id, source in _m.storage.rulebook_manager.sources.items():
        counts = source.content_counts()
        rulebooks.append({
            "id": source_id,
            "type": source.source_type.value,
            "loaded_at": source.loaded_at.isoformat() if source.loaded_at else None,
            "content": {
                "classes": counts.classes,
                "races": counts.races,
                "spells": counts.spells,
                "monsters": counts.monsters,
            }
        })

    # Markdown output
    lines = ["# Active Rulebooks\n"]
    for rb in rulebooks:
        lines.append(f"## {rb['id']}")
        lines.append(f"- **Type:** {rb['type']}")
        if rb['loaded_at']:
            lines.append(f"- **Loaded:** {rb['loaded_at']}")
        lines.append(f"- **Content:** {rb['content']['classes']} classes, {rb['content']['races']} races, {rb['content']['spells']} spells, {rb['content']['monsters']} monsters")
        lines.append("")

    return "\n".join(lines)

@mcp.tool
def unload_rulebook(
    source_id: Annotated[
        str,
        Field(description="ID of the rulebook to unload (from list_rulebooks)")
    ],
) -> str:
    """Remove a rulebook from the current campaign."""
    if not _m.storage._current_campaign:
        return "❌ No campaign loaded."

    if not _m.storage.rulebook_manager:
        return "❌ No rulebooks loaded."

    if _m.storage.rulebook_manager.unload_source(source_id):
        return f"✅ Unloaded rulebook: {source_id}"
    else:
        return f"❌ Rulebook not found: {source_id}"

# ----------------------------------------------------------------------
# Rulebook Query Tools
# ----------------------------------------------------------------------

@mcp.tool
def search_rules(
    query: Annotated[str, Field(description="Search term (name, partial match). Can be empty if class_filter is provided.")] = "",
    category: Annotated[
        Literal["all", "class", "subclass", "race", "subrace", "spell", "monster", "feat", "item",
                "background", "optionalfeature", "condition", "rule"] | None,
        Field(description="Filter by category. Default: all")
    ] = "all",
    limit: Annotated[int, Field(description="Max results", ge=1, le=50)] = 20,
    class_filter: Annotated[
        str | None,
        Field(description="Filter spells by class (e.g., 'ranger', 'wizard'). Only applies to spell category.")
    ] = None,
) -> str:
    """Search for rules content across all loaded rulebooks.

    Works without a campaign loaded (uses global rulebook manager).
    When a campaign is active, its rulebook manager takes priority.

    Examples:
        - search_rules(query="fire", category="spell") - Find spells with 'fire' in name
        - search_rules(class_filter="ranger", category="spell") - All ranger spells
        - search_rules(query="cure", class_filter="ranger", category="spell") - Ranger spells with 'cure' in name
    """
    manager = _m._get_rulebook_manager()
    if not manager:
        return "❌ No rulebooks loaded. Use `load_rulebook` first or ensure the global rulebook manager is initialized."

    if not query and not class_filter:
        return "❌ Please provide either a search query or a class_filter."

    categories = [category] if category and category != "all" else None
    results = manager.search(
        query=query,
        categories=categories,
        limit=limit,
        class_filter=class_filter,
    )

    if not results:
        filter_desc = f"class='{class_filter}'" if class_filter else f"'{query}'"
        return f"No results found for {filter_desc}."

    # Build header
    if class_filter and query:
        header = f"# Search Results: '{query}' (class: {class_filter})\n"
    elif class_filter:
        header = f"# Spells for class: {class_filter}\n"
    else:
        header = f"# Search Results: '{query}'\n"

    lines = [header]
    for r in results:
        lines.append(f"- **{r.name}** ({r.category}) — _{r.source}_")

    # Source attribution
    source_names = sorted({r.source for r in results if r.source})
    if source_names:
        lines.append(f"\n*Source: {', '.join(source_names)}*")

    return "\n".join(lines)

@mcp.tool
def get_class_info(
    name: Annotated[str, Field(description="Class name (e.g., 'wizard', 'fighter')")],
    level: Annotated[int | None, Field(description="Show features up to this level", ge=1, le=20)] = None,
    subclass: Annotated[
        str | None,
        Field(description="Also show this subclass's features (e.g. 'echo knight')")
    ] = None,
) -> str:
    """Get full class definition from loaded rulebooks.

    Lists subclasses from every loaded source; with ``level`` shows class (and
    subclass) features by level and the spell slots at that level.

    Works without a campaign loaded (uses global rulebook manager).
    When a campaign is active, its rulebook manager takes priority.
    """
    manager = _m._get_rulebook_manager()
    if not manager:
        return "❌ No rulebooks loaded."

    class_def = manager.get_class(name.lower())
    if not class_def:
        return f"❌ Class '{name}' not found in loaded rulebooks."

    def _find_subclass(slug: str):
        key = slug.lower().replace(" ", "-")
        return manager.get_subclass(f"{class_def.index}-{key}") or manager.get_subclass(key)

    # Markdown format
    lines = [f"# {class_def.name}\n"]
    lines.append(f"**Hit Die:** d{class_def.hit_die}")
    lines.append(f"**Saving Throws:** {', '.join(class_def.saving_throws)}")
    sc = class_def.spellcasting
    if sc:
        lines.append(f"**Spellcasting:** {sc.spellcasting_ability} ({sc.caster_type} caster, from level {sc.level})")
    sub_names = []
    for slug in class_def.subclasses:
        sub = _find_subclass(slug)
        sub_names.append(f"{sub.name} [{sub.source_book or sub.source}]" if sub else slug)
    title = class_def.subclass_title or "Subclasses"
    lines.append(f"\n**{title} (choose at level {class_def.subclass_level}):** {', '.join(sub_names) if sub_names else 'None loaded'}")

    if level:
        lines.append(f"\n## Features up to level {level}")
        for lvl in sorted(k for k in class_def.class_levels if k <= level):
            feats = class_def.class_levels[lvl].features
            if feats:
                lines.append(f"- **{lvl}:** {', '.join(feats)}")
        if sc and sc.spell_slots:
            slots = sc.slots_at(level)
            slot_text = ", ".join(f"L{k}: {v}" for k, v in slots.items()) or "none"
            lines.append(f"\n**Spell slots at level {level}{' (Pact Magic)' if sc.pact_magic else ''}:** {slot_text}")

    if subclass:
        sub = _find_subclass(subclass)
        if not sub:
            lines.append(f"\n❌ Subclass '{subclass}' not found for {class_def.name}.")
        else:
            lines.append(f"\n## {sub.name}")
            for lvl, info in sorted(sub.subclass_levels.items()):
                if level and lvl > level:
                    continue
                for feat in info.features:
                    detail = info.feature_details.get(feat, "")
                    lines.append(f"- **{lvl} — {feat}:** {detail[:300] + ('…' if len(detail) > 300 else '')}".rstrip(": "))

    lines.append(f"\n*Source: {class_def.source}{f' ({class_def.source_book})' if class_def.source_book else ''}*")

    return "\n".join(lines)

@mcp.tool
def get_race_info(
    name: Annotated[str, Field(description="Race name (e.g., 'elf', 'dwarf')")],
) -> str:
    """Get full race definition from loaded rulebooks.

    Works without a campaign loaded (uses global rulebook manager).
    When a campaign is active, its rulebook manager takes priority.
    """
    manager = _m._get_rulebook_manager()
    if not manager:
        return "❌ No rulebooks loaded."

    race_def = manager.get_race(name.lower())
    if not race_def:
        return f"❌ Race '{name}' not found in loaded rulebooks."

    lines = [f"# {race_def.name}\n"]
    lines.append(f"**Size:** {race_def.size.value}")
    lines.append(f"**Speed:** {race_def.speed} ft.")
    if race_def.ability_bonuses:
        bonuses = ", ".join([f"{b.ability_score} +{b.bonus}" for b in race_def.ability_bonuses])
        lines.append(f"**Ability Bonuses:** {bonuses}")
    if race_def.traits:
        lines.append(f"\n**Traits:**")
        for trait in race_def.traits:
            lines.append(f"- **{trait.name}:** {trait.desc[0] if trait.desc else 'No description'}")
    if race_def.subraces:
        lines.append(f"\n**Subraces:** {', '.join(race_def.subraces)}")
    lines.append(f"\n*Source: {race_def.source}*")

    return "\n".join(lines)

@mcp.tool
def get_spell_info(
    name: Annotated[str, Field(description="Spell name (e.g., 'fireball', 'cure wounds')")],
) -> str:
    """Get spell details from loaded rulebooks.

    Works without a campaign loaded (uses global rulebook manager).
    When a campaign is active, its rulebook manager takes priority.
    """
    manager = _m._get_rulebook_manager()
    if not manager:
        return "❌ No rulebooks loaded."

    # Normalize name for lookup
    spell_index = name.lower().replace(" ", "-")
    spell = manager.get_spell(spell_index)
    if not spell:
        return f"❌ Spell '{name}' not found."

    # D&D-style spell card format
    components = ", ".join(spell.components)
    if spell.material:
        components += f" ({spell.material})"

    lines = [f"# {spell.name}"]
    lines.append(f"*{spell.level_text} {spell.school.value}*\n")
    lines.append(f"**Casting Time:** {spell.casting_time}")
    lines.append(f"**Range:** {spell.range}")
    lines.append(f"**Components:** {components}")
    lines.append(f"**Duration:** {spell.duration}")
    if spell.concentration:
        lines.append("**Concentration:** Yes")
    if spell.ritual:
        lines.append("**Ritual:** Yes")
    lines.append(f"\n{chr(10).join(spell.desc)}")
    if spell.higher_level:
        lines.append(f"\n**At Higher Levels:** {chr(10).join(spell.higher_level)}")
    lines.append(f"\n*Source: {spell.source}*")

    return "\n".join(lines)

@mcp.tool
def get_monster_info(
    name: Annotated[str, Field(description="Monster name (e.g., 'goblin', 'adult red dragon')")],
) -> str:
    """Get monster stat block from loaded rulebooks.

    Works without a campaign loaded (uses global rulebook manager).
    When a campaign is active, its rulebook manager takes priority.
    """
    manager = _m._get_rulebook_manager()
    if not manager:
        return "❌ No rulebooks loaded."

    monster_index = name.lower().replace(" ", "-")
    monster = manager.get_monster(monster_index)
    if not monster:
        return f"❌ Monster '{name}' not found."

    # D&D stat block format
    lines = [f"# {monster.name}"]
    lines.append(f"*{monster.size.value} {monster.type}, {monster.alignment}*\n")
    lines.append(f"**Armor Class:** {monster.armor_class[0].value}")
    lines.append(f"**Hit Points:** {monster.hit_points} ({monster.hit_dice})")
    speeds = ", ".join([f"{k} {v}" for k, v in monster.speed.items()])
    lines.append(f"**Speed:** {speeds}\n")

    # Ability scores
    lines.append("| STR | DEX | CON | INT | WIS | CHA |")
    lines.append("|-----|-----|-----|-----|-----|-----|")
    lines.append(f"| {monster.strength} ({monster.get_ability_modifier('strength'):+d}) | {monster.dexterity} ({monster.get_ability_modifier('dexterity'):+d}) | {monster.constitution} ({monster.get_ability_modifier('constitution'):+d}) | {monster.intelligence} ({monster.get_ability_modifier('intelligence'):+d}) | {monster.wisdom} ({monster.get_ability_modifier('wisdom'):+d}) | {monster.charisma} ({monster.get_ability_modifier('charisma'):+d}) |\n")

    lines.append(f"**Challenge:** {monster.challenge_rating} ({monster.xp} XP)")
    lines.append(f"\n*Source: {monster.source}*")

    return "\n".join(lines)

@mcp.tool
def validate_character_rules(
    name_or_id: Annotated[str, Field(description="Character name or ID to validate")],
) -> str:
    """Validate a character against loaded rulebooks."""
    character = _m.storage.get_character(name_or_id)
    if not character:
        return f"❌ Character '{name_or_id}' not found."

    if not _m.storage.rulebook_manager:
        return "⚠️ No rulebooks loaded. Cannot validate without rules."

    validator = CharacterValidator(_m.storage.rulebook_manager)
    report = validator.validate(character)

    # Markdown format
    status = "✅ Valid" if report.valid else "❌ Invalid"
    lines = [f"# Validation Report: {character.name}"]
    lines.append(f"**Status:** {status}\n")

    if report.errors:
        lines.append("## Errors")
        for issue in report.errors:
            lines.append(f"- **{issue.type}:** {issue.message}")
            if issue.suggestion:
                lines.append(f"  💡 {issue.suggestion}")

    if report.warnings:
        lines.append("\n## Warnings")
        for issue in report.warnings:
            lines.append(f"- **{issue.type}:** {issue.message}")
            if issue.suggestion:
                lines.append(f"  💡 {issue.suggestion}")

    info_issues = [i for i in report.issues if i.severity.value == "info"]
    if info_issues:
        lines.append("\n## Info")
        for issue in info_issues:
            lines.append(f"- {issue.message}")

    return "\n".join(lines)
