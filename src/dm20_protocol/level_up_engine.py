"""Level-Up Engine — handle character progression from one level to the next.

Given a Character and a RulebookManager, the engine increments level,
calculates HP increase, adds class features, updates spell slots, handles
ASI choices, and manages subclass selection.
"""

from __future__ import annotations

import random
from typing import Any

from pydantic import BaseModel

from .models import AbilityScore, Character, ClassResource, Feature
from .rulebooks.manager import RulebookManager
from .rulebooks.models import ClassDefinition, SubclassDefinition
from .rules_math import compute_spell_slots, default_resources


# Standard ASI levels (most classes)
STANDARD_ASI_LEVELS = {4, 8, 12, 16, 19}

# Fighter gets extra ASI at levels 6 and 14
FIGHTER_EXTRA_ASI_LEVELS = {6, 14}

# Rogue gets extra ASI at level 10
ROGUE_EXTRA_ASI_LEVEL = {10}

# Ability abbreviation → full name
ABILITY_NAMES = {
    "STR": "strength",
    "DEX": "dexterity",
    "CON": "constitution",
    "INT": "intelligence",
    "WIS": "wisdom",
    "CHA": "charisma",
}

ALL_ABILITIES = set(ABILITY_NAMES.values())

# Maximum ability score (without magic items)
MAX_ABILITY_SCORE = 20


class LevelUpError(Exception):
    """Raised when level-up cannot proceed."""


class LevelUpResult(BaseModel):
    """Summary of changes applied during level-up."""

    new_level: int
    hp_gained: int
    features_added: list[str]
    spell_slots_changed: bool
    asi_applied: dict[str, int] | None = None
    subclass_set: str | None = None
    proficiency_bonus_changed: bool
    summary: str


class LevelUpEngine:
    """Handle character level progression using rulebook data."""

    def __init__(self, rulebook_manager: RulebookManager, rules_version: str = "2024") -> None:
        self.rm = rulebook_manager
        self.rules_version = rules_version

    def level_up(
        self,
        character: Character,
        *,
        class_name: str | None = None,
        hp_method: str = "average",
        asi_choices: dict[str, int] | None = None,
        subclass: str | None = None,
        new_spells: list[str] | None = None,
    ) -> LevelUpResult:
        """Level up a character by one level.

        Args:
            character: The character to level up (modified in-place).
            class_name: Which class to level up (required for multiclass).
                        If None, levels up the primary (first) class.
            hp_method: "average" (default, PHB standard) or "roll".
            asi_choices: Ability score improvements, e.g. {"strength": 2}
                         or {"strength": 1, "dexterity": 1}. Total must be 2.
            subclass: Subclass name to set (required at subclass_level).
            new_spells: List of spell names to learn (informational, added to notes).

        Returns:
            LevelUpResult with summary of all changes.

        Raises:
            LevelUpError: If level-up cannot proceed. The character is left
                completely unchanged in that case (all work happens on a deep
                copy that is committed only on success).
        """
        work = character.model_copy(deep=True)
        result = self._level_up(
            work,
            class_name=class_name,
            hp_method=hp_method,
            asi_choices=asi_choices,
            subclass=subclass,
            new_spells=new_spells,
        )
        # Commit: copy every field of the successfully levelled copy back.
        for field_name in type(character).model_fields:
            setattr(character, field_name, getattr(work, field_name))
        return result

    def _level_up(
        self,
        character: Character,
        *,
        class_name: str | None,
        hp_method: str,
        asi_choices: dict[str, int] | None,
        subclass: str | None,
        new_spells: list[str] | None,
    ) -> LevelUpResult:
        """Level-up implementation. Mutates ``character`` (a working copy)."""
        # Check total level cap before proceeding
        total_before = character.total_level
        if total_before >= 20:
            raise LevelUpError("Character is already at maximum level (20).")

        # Find the target class to level up (may add a new class for multiclass dip)
        target_class, is_new_class = self._find_target_class(character, class_name)

        if is_new_class:
            # Multiclass dip: class already added at level 1 by _find_target_class.
            # total_level is now total_before + 1 (the new class at level 1).
            new_level = 1
            current_level = 0
        else:
            current_level = target_class.level
            new_level = current_level + 1
            target_class.level = new_level

        # Look up class definition
        class_def = self._get_class_def(target_class.name)

        # Validate inputs before anything else changes (fail fast).
        if hp_method not in ("average", "roll"):
            raise LevelUpError(
                f"Unknown hp_method: '{hp_method}'. Use 'average' or 'roll'."
            )
        resolved_subclass: tuple[str, str | None, str | None] | None = None
        subclass_level = class_def.subclass_level
        if subclass and new_level >= subclass_level:
            resolved_subclass = resolve_subclass(self.rm, class_def, subclass)
        if asi_choices and self._is_asi_level(new_level, class_def.name):
            self._validate_asi(asi_choices)

        old_prof_bonus = character.proficiency_bonus
        changes: list[str] = []

        # 1. Report level change
        if is_new_class:
            changes.append(f"Multiclass: added {target_class.name} (level 1)")
        else:
            changes.append(f"{target_class.name} Level: {current_level} -> {new_level}")

        # 2. Calculate and apply HP increase
        hp_gained = self._calculate_hp_increase(
            class_def.hit_die, character, hp_method
        )
        character.hit_points_max += hp_gained
        character.hit_points_current += hp_gained
        changes.append(f"HP: +{hp_gained} (max now {character.hit_points_max})")

        # 3. Update hit dice
        hit_dice_type = f"d{class_def.hit_die}"
        character.hit_dice_type = hit_dice_type
        total_level = character.total_level
        character.hit_dice_remaining = f"{total_level}{hit_dice_type}"
        target_class.hit_dice = f"{new_level}{hit_dice_type}"

        # 4. Add class features from this level
        features_added = self._add_level_features(character, class_def, new_level)
        if features_added:
            changes.append(f"Features: {', '.join(features_added)}")

        # 5. Handle subclass at the appropriate level
        subclass_set = None
        if resolved_subclass is not None:
            display, _index, warning = resolved_subclass
            target_class.subclass = display
            subclass_set = display
            changes.append(f"Subclass: {display}")
            if warning:
                changes.append(f"WARNING: {warning}")
        elif subclass:
            changes.append(
                f"NOTE: subclass '{subclass}' ignored: {class_def.name} chooses "
                f"a subclass at level {subclass_level}."
            )
        elif new_level == subclass_level and not target_class.subclass:
            options = subclass_options(self.rm, class_def)
            changes.append(
                f"NOTE: Level {new_level} is the subclass selection level "
                f"for {class_def.name}. Available: {', '.join(options) or 'any (free text)'}. "
                f"Use update_character with subclass=... to set it later."
            )

        # 5b. Subclass features gained at this level
        if target_class.subclass:
            sub_feats = add_subclass_features(
                self.rm, character, class_def, target_class.subclass, new_level, new_level
            )
            if sub_feats:
                features_added.extend(sub_feats)
                changes.append(f"Subclass features: {', '.join(sub_feats)}")

        # 6. Handle ASI at appropriate levels
        asi_applied = None
        if self._is_asi_level(new_level, class_def.name):
            if asi_choices:
                asi_applied = self._apply_asi(character, asi_choices)
                asi_desc = ", ".join(
                    f"{k.capitalize()} +{v}" for k, v in asi_applied.items()
                )
                changes.append(f"ASI: {asi_desc}")
            else:
                changes.append(
                    f"NOTE: Level {new_level} grants an Ability Score Improvement. "
                    "Use level_up_character with asi_choices to apply it later, "
                    "or use update_character to adjust ability scores."
                )

        # 7. Update spell slots for casters (all classes, incl. multiclass + pact)
        spell_slots_changed = self._recompute_spell_slots(character)
        if spell_slots_changed:
            if character.spell_slots:
                slots_str = ", ".join(
                    f"L{k}: {v}" for k, v in sorted(character.spell_slots.items())
                )
                changes.append(f"Spell slots: {slots_str}")
            if character.pact_slots:
                pact_str = ", ".join(
                    f"{v} x L{k}" for k, v in sorted(character.pact_slots.items())
                )
                changes.append(f"Pact slots (short rest): {pact_str}")

        # 7b. Class resources (Action Surge, Rage, Channel Divinity, ...)
        res_changes = apply_default_resources(character, self.rules_version)
        if res_changes:
            changes.append(f"Resources: {', '.join(res_changes)}")

        # 8. Handle new spells (informational)
        if new_spells:
            changes.append(f"New spells noted: {', '.join(new_spells)}")

        # 9. Proficiency bonus — model_validator only runs on construction,
        # so we must recalculate manually after mutating level in-place.
        character.proficiency_bonus = 2 + (character.total_level - 1) // 4
        new_prof_bonus = character.proficiency_bonus
        prof_changed = new_prof_bonus != old_prof_bonus
        if prof_changed:
            changes.append(
                f"Proficiency bonus: +{old_prof_bonus} -> +{new_prof_bonus}"
            )

        # Build summary
        summary = (
            f"{character.name} advanced to {target_class.name} level {new_level}"
            f" (total level {character.total_level})!\n"
            + "\n".join(f"  - {c}" for c in changes)
        )

        return LevelUpResult(
            new_level=new_level,
            hp_gained=hp_gained,
            features_added=features_added,
            spell_slots_changed=spell_slots_changed,
            asi_applied=asi_applied,
            subclass_set=subclass_set,
            proficiency_bonus_changed=prof_changed,
            summary=summary,
        )

    # ------------------------------------------------------------------
    # HP Calculation
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate_hp_increase(
        hit_die: int, character: Character, method: str
    ) -> int:
        """Calculate HP gained for one level.

        Average: hit_die // 2 + 1 + CON mod (PHB standard, minimum 1).
        Roll: random 1-hit_die + CON mod (minimum 1).
        """
        con_mod = character.abilities.get(
            "constitution", AbilityScore(score=10)
        ).mod

        if method == "average":
            avg_roll = hit_die // 2 + 1
            return max(avg_roll + con_mod, 1)
        elif method == "roll":
            roll = random.randint(1, hit_die)
            return max(roll + con_mod, 1)
        else:
            raise LevelUpError(
                f"Unknown hp_method: '{method}'. Use 'average' or 'roll'."
            )

    # ------------------------------------------------------------------
    # Features
    # ------------------------------------------------------------------

    @staticmethod
    def _add_level_features(
        character: Character, class_def: ClassDefinition, level: int
    ) -> list[str]:
        """Add features from ClassDefinition.class_levels for this level."""
        level_info = class_def.class_levels.get(level)
        if not level_info:
            return []

        added: list[str] = []
        for feat_name in level_info.features:
            desc = level_info.feature_details.get(feat_name, "")
            feature = Feature(
                name=feat_name,
                source=f"{class_def.name} {level}",
                description=desc,
                level_gained=level,
            )
            character.features.append(feature)
            if feat_name not in character.features_and_traits:
                character.features_and_traits.append(feat_name)
            added.append(feat_name)

        return added

    # ------------------------------------------------------------------
    # Subclass
    # ------------------------------------------------------------------

    def _set_subclass(
        self, target_class: "CharacterClass", class_def: ClassDefinition, subclass: str
    ) -> str:
        """Validate and set the target class's subclass. Returns the stored name."""
        display, _index, _warning = resolve_subclass(self.rm, class_def, subclass)
        target_class.subclass = display
        return display

    # ------------------------------------------------------------------
    # ASI
    # ------------------------------------------------------------------

    @staticmethod
    def _is_asi_level(level: int, class_name: str) -> bool:
        """Check if this level grants an ASI."""
        class_lower = class_name.lower()
        asi_levels = set(STANDARD_ASI_LEVELS)

        if class_lower == "fighter":
            asi_levels |= FIGHTER_EXTRA_ASI_LEVELS
        elif class_lower == "rogue":
            asi_levels |= ROGUE_EXTRA_ASI_LEVEL

        return level in asi_levels

    @staticmethod
    def _validate_asi(choices: dict[str, int]) -> dict[str, int]:
        """Validate ASI choices without applying them. Returns normalized choices."""
        normalized: dict[str, int] = {}
        for ability, bonus in choices.items():
            name = ABILITY_NAMES.get(ability.upper(), ability.lower())
            if name not in ALL_ABILITIES:
                raise LevelUpError(
                    f"Unknown ability: '{ability}'. "
                    f"Valid: {', '.join(sorted(ALL_ABILITIES))}"
                )
            if bonus not in (1, 2):
                raise LevelUpError(
                    f"Each ASI bonus must be 1 or 2 (got {ability}={bonus})"
                )
            normalized[name] = bonus
        total = sum(normalized.values())
        if total != 2:
            raise LevelUpError(
                f"ASI total must be exactly 2 (got {total}). "
                'Use {"ability": 2} or {"ability1": 1, "ability2": 1}.'
            )
        return normalized

    @staticmethod
    def _apply_asi(
        character: Character, choices: dict[str, int]
    ) -> dict[str, int]:
        """Apply Ability Score Improvement choices.

        Validates:
        - Total bonus is exactly 2
        - Each individual bonus is 1 or 2
        - Abilities exist
        - Scores don't exceed MAX_ABILITY_SCORE
        """
        # Normalize ability names
        normalized: dict[str, int] = {}
        for ability, bonus in choices.items():
            name = ABILITY_NAMES.get(ability.upper(), ability.lower())
            if name not in ALL_ABILITIES:
                raise LevelUpError(
                    f"Unknown ability: '{ability}'. "
                    f"Valid: {', '.join(sorted(ALL_ABILITIES))}"
                )
            if bonus not in (1, 2):
                raise LevelUpError(
                    f"Each ASI bonus must be 1 or 2 (got {ability}={bonus})"
                )
            normalized[name] = bonus

        total = sum(normalized.values())
        if total != 2:
            raise LevelUpError(
                f"ASI total must be exactly 2 (got {total}). "
                "Use {{\"ability\": 2}} or {{\"ability1\": 1, \"ability2\": 1}}."
            )

        # Apply bonuses
        applied: dict[str, int] = {}
        for ability, bonus in normalized.items():
            current = character.abilities.get(
                ability, AbilityScore(score=10)
            )
            new_score = min(current.score + bonus, MAX_ABILITY_SCORE)
            actual_bonus = new_score - current.score
            character.abilities[ability] = AbilityScore(score=new_score)
            if actual_bonus > 0:
                applied[ability] = actual_bonus

        return applied

    # ------------------------------------------------------------------
    # Spell Slots
    # ------------------------------------------------------------------

    def _recompute_spell_slots(self, character: Character) -> bool:
        """Recompute spell_slots and pact_slots from all classes. Returns True if changed."""
        class_defs: dict[str, Any] = {}
        for cls in character.classes:
            try:
                class_defs[cls.name.strip().lower()] = self._get_class_def(cls.name)
            except LevelUpError:
                continue
        return recompute_character_slots(character, self.rules_version, class_defs)

    @staticmethod
    def _update_spell_slots(
        character: Character, class_def: ClassDefinition, level: int
    ) -> bool:
        """Update spell slot maximums for the new level.

        Returns True if spell slots changed.
        """
        if not class_def.spellcasting or not class_def.spellcasting.spell_slots:
            return False

        slots_list = class_def.spellcasting.spell_slots.get(level)
        if not slots_list:
            return False

        new_slots: dict[int, int] = {}
        for idx, count in enumerate(slots_list):
            spell_level = idx + 1
            if count > 0:
                new_slots[spell_level] = count

        changed = new_slots != character.spell_slots
        if changed:
            character.spell_slots = new_slots

        return changed

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def _find_target_class(
        self, character: Character, class_name: str | None
    ) -> tuple["CharacterClass", bool]:
        """Find the class to level up, or add a new class (multiclass dip).

        Returns:
            Tuple of (target_class, is_new_class).
            is_new_class=True means this is a multiclass dip into a brand new class.
        """
        from .models import CharacterClass as _CC

        if class_name is None:
            return character.classes[0], False

        # Search by name (case-insensitive)
        name_lower = class_name.strip().lower()
        for cls in character.classes:
            if cls.name.lower() == name_lower:
                return cls, False

        # Not found — this is a multiclass dip into a new class.
        # Validate the class exists in rulebooks before adding.
        class_def = self._get_class_def(class_name)
        hit_dice_type = f"d{class_def.hit_die}"
        new_class = _CC(
            name=class_def.name,
            level=1,  # Starts at level 1
            hit_dice=hit_dice_type,
        )
        character.classes.append(new_class)
        return new_class, True

    def _get_class_def(self, class_name: str) -> ClassDefinition:
        """Look up class definition from rulebook manager."""
        index = class_name.strip().lower().replace(" ", "-").replace("_", "-")
        class_def = self.rm.get_class(index)
        if not class_def:
            raise LevelUpError(
                f"Class '{class_name}' not found in loaded rulebooks. "
                "Make sure a rulebook is loaded: load_rulebook source=\"srd\""
            )
        return class_def


# ----------------------------------------------------------------------
# Module-level helpers (shared with main.py tools and the character builder)
# ----------------------------------------------------------------------


def _norm_key(value: str) -> str:
    return value.strip().lower().replace("_", "-").replace(" ", "-").replace("'", "")


def _subclass_defs_for(rm: Any, class_def: Any) -> list[tuple[str, str]]:
    """Return (index, display_name) for every known subclass of ``class_def``.

    Combines ``class_def.subclasses`` with subclasses registered by any loaded
    source (e.g. custom packs) whose parent_class matches.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    parent = _norm_key(getattr(class_def, "name", "") or "")
    parent_index = _norm_key(getattr(class_def, "index", "") or "") if isinstance(
        getattr(class_def, "index", None), str) else parent

    for index in list(getattr(class_def, "subclasses", None) or []):
        if not isinstance(index, str):
            continue
        display = index.replace("-", " ").title()
        sub = None
        if rm is not None:
            try:
                sub = rm.get_subclass(index)
            except Exception:
                sub = None
        if isinstance(sub, SubclassDefinition):
            display = sub.name
        key = _norm_key(index)
        if key not in seen:
            seen.add(key)
            out.append((index, display))

    if rm is not None:
        try:
            results = list(rm.search("", categories=["subclass"], limit=1000) or [])
        except Exception:
            results = []
        for r in results:
            index = getattr(r, "index", None)
            if not isinstance(index, str) or _norm_key(index) in seen:
                continue
            try:
                sub = rm.get_subclass(index)
            except Exception:
                sub = None
            if not isinstance(sub, SubclassDefinition):
                continue
            if _norm_key(sub.parent_class) not in (parent, parent_index):
                continue
            seen.add(_norm_key(index))
            out.append((index, sub.name))
    return out


def subclass_options(rm: Any, class_def: Any) -> list[str]:
    """Display names of valid subclasses for ``class_def`` (may be empty)."""
    return [display for _index, display in _subclass_defs_for(rm, class_def)]


def resolve_subclass(
    rm: Any, class_def: Any, subclass: str
) -> tuple[str, str | None, str | None]:
    """Validate a subclass choice.

    Returns ``(name_to_store, rulebook_index_or_None, warning_or_None)``.
    Matching ignores case, spaces, hyphens and underscores, and accepts a
    unique partial match ("Evocation" -> "School of Evocation"). If the class
    has no known subclasses the name is accepted as free text with a warning.

    Raises:
        LevelUpError: if subclasses are known and none matches; the message
            lists the valid options.
    """
    name = subclass.strip()
    if not name:
        raise LevelUpError("Subclass name is empty.")
    options = _subclass_defs_for(rm, class_def)
    class_name = getattr(class_def, "name", "this class")
    if not options:
        return name, None, (
            f"No subclass list is loaded for {class_name}; stored '{name}' as free text "
            "(no subclass features were added automatically)."
        )

    key = _norm_key(name)
    for index, display in options:
        if key in (_norm_key(index), _norm_key(display)):
            # Keep the caller's spelling when it matches exactly (case-insensitive).
            return name, index, None

    partial = [
        (index, display) for index, display in options
        if key in _norm_key(index) or key in _norm_key(display)
    ]
    if len(partial) == 1:
        index, display = partial[0]
        return display, index, None

    raise LevelUpError(
        f"Invalid subclass '{subclass}' for {class_name}. "
        f"Available: {', '.join(display for _i, display in options)}"
    )


def add_subclass_features(
    rm: Any,
    character: Character,
    class_def: Any,
    subclass: str,
    from_level: int,
    to_level: int,
) -> list[str]:
    """Attach subclass features for class levels ``from_level..to_level``.

    Uses the rulebook SubclassDefinition when one can be found; features the
    character already has (same name and source) are skipped. Returns the
    names of features added.
    """
    if rm is None:
        return []
    key = _norm_key(subclass)
    sub_def = None
    for index, display in _subclass_defs_for(rm, class_def):
        if key in (_norm_key(index), _norm_key(display)):
            try:
                candidate = rm.get_subclass(index)
            except Exception:
                candidate = None
            if isinstance(candidate, SubclassDefinition):
                sub_def = candidate
                break
    if sub_def is None:
        return []

    existing = {(f.name, f.source) for f in character.features}
    added: list[str] = []
    for lvl in range(max(1, from_level), to_level + 1):
        info = sub_def.subclass_levels.get(lvl)
        if not info:
            continue
        for feat_name in info.features:
            source = f"{sub_def.name} {lvl}"
            if (feat_name, source) in existing:
                continue
            character.features.append(Feature(
                name=feat_name,
                source=source,
                description=info.feature_details.get(feat_name, ""),
                level_gained=lvl,
            ))
            if feat_name not in character.features_and_traits:
                character.features_and_traits.append(feat_name)
            added.append(feat_name)
    return added


def _is_caster(character: Character, class_defs: dict[str, Any] | None = None) -> bool:
    from .rules_math import caster_type_for

    class_defs = class_defs or {}
    return any(
        caster_type_for(c.name, c.subclass, class_defs.get(c.name.strip().lower())) is not None
        for c in character.classes
    )


def recompute_character_slots(
    character: Character,
    rules_version: str = "2024",
    class_defs: dict[str, Any] | None = None,
) -> bool:
    """Set ``spell_slots``/``pact_slots`` from the character's classes.

    Non-caster characters are left untouched (manually set slots survive).
    Used-slot counters are clamped to the new maximums. Returns True if the
    maximums changed.
    """
    if not _is_caster(character, class_defs):
        return False
    slots, pact = compute_spell_slots(character.classes, rules_version, class_defs)
    changed = slots != character.spell_slots or pact != character.pact_slots
    character.spell_slots = slots
    character.pact_slots = pact
    character.spell_slots_used = {
        lvl: min(used, slots[lvl]) for lvl, used in character.spell_slots_used.items()
        if lvl in slots and used > 0
    }
    character.pact_slots_used = {
        lvl: min(used, pact[lvl]) for lvl, used in character.pact_slots_used.items()
        if lvl in pact and used > 0
    }
    return changed


def apply_default_resources(character: Character, rules_version: str = "2024") -> list[str]:
    """Seed or grow default class resources for the character's current levels.

    New resources start full. Existing ones get the new maximum; if the
    maximum grew, current grows by the same amount. Resources that are not in
    the defaults (custom ones) are left alone. Returns change descriptions.
    """
    changes: list[str] = []
    defaults = default_resources(character.classes, character.abilities, rules_version)
    for name, spec in defaults.items():
        existing = character.resources.get(name)
        if existing is None:
            character.resources[name] = ClassResource(
                max=spec["max"], current=spec["max"], recharge=spec["recharge"],
                short_rest_regain=spec["short_rest_regain"],
            )
            changes.append(f"{name} {spec['max']}/{spec['max']}")
            continue
        if existing.max != spec["max"] or existing.recharge != spec["recharge"]:
            delta = spec["max"] - existing.max
            existing.current = max(0, min(spec["max"], existing.current + max(delta, 0)))
            existing.max = spec["max"]
            existing.recharge = spec["recharge"]
            existing.short_rest_regain = spec["short_rest_regain"]
            changes.append(f"{name} {existing.current}/{existing.max}")
    return changes
