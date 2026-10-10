"""
Custom rulebook source for loading local JSON/YAML files.

This source allows users to define homebrew content in structured files
that integrate seamlessly with official SRD content.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import yaml
from pydantic import ValidationError

from ..models import (
    ClassDefinition,
    SubclassDefinition,
    RaceDefinition,
    SubraceDefinition,
    SpellDefinition,
    MonsterDefinition,
    FeatDefinition,
    BackgroundDefinition,
    ItemDefinition,
    RulebookSource as RulebookSourceType,
)
from ..spell_progression import (
    caster_type_for,
    first_casting_level,
    normalize_progression,
    normalize_slot_table,
    standard_slot_table,
)
from .base import RulebookSourceBase, SearchResult, ContentCounts


logger = logging.getLogger("dm20-protocol")


class CustomSourceError(Exception):
    """Error loading or parsing a custom rulebook."""
    pass


class CustomSource(RulebookSourceBase):
    """
    Rulebook source for loading local JSON or YAML files.

    Supports partial rulebooks (e.g., a file with only custom races)
    and provides schema validation for content.

    File formats supported:
    - .json - JSON files
    - .yaml / .yml - YAML files

    Expected file structure:
    ```json
    {
      "$schema": "dm20-protocol/rulebook-v1",
      "name": "My Homebrew",
      "version": "1.0",
      "content": {
        "classes": [...],
        "races": [...],
        "spells": [...],
        ...
      }
    }
    ```
    """

    SUPPORTED_EXTENSIONS = {".json", ".yaml", ".yml"}
    CURRENT_SCHEMA = "dm20-protocol/rulebook-v1"

    def __init__(
        self,
        path: Path | str,
        source_id: str | None = None,
    ):
        """
        Initialize a custom source from a file path.

        Args:
            path: Path to the JSON or YAML file
            source_id: Optional custom ID. If not provided, derived from filename.
        """
        self.path = Path(path)

        if source_id is None:
            # Derive source_id from filename: "my_races.json" -> "custom-my-races"
            source_id = f"custom-{self.path.stem.replace('_', '-').lower()}"

        super().__init__(
            source_id=source_id,
            source_type=RulebookSourceType.CUSTOM,
            name=None,  # Will be set from file content
        )

        # Content storage
        self._classes: dict[str, ClassDefinition] = {}
        self._subclasses: dict[str, SubclassDefinition] = {}
        self._races: dict[str, RaceDefinition] = {}
        self._subraces: dict[str, SubraceDefinition] = {}
        self._spells: dict[str, SpellDefinition] = {}
        self._monsters: dict[str, MonsterDefinition] = {}
        self._feats: dict[str, FeatDefinition] = {}
        self._backgrounds: dict[str, BackgroundDefinition] = {}
        self._items: dict[str, ItemDefinition] = {}

    async def load(self) -> None:
        """Load and parse the rulebook file."""
        if not self.path.exists():
            raise CustomSourceError(f"Rulebook file not found: {self.path}")

        suffix = self.path.suffix.lower()
        if suffix not in self.SUPPORTED_EXTENSIONS:
            raise CustomSourceError(
                f"Unsupported file format: {suffix}. "
                f"Supported: {', '.join(self.SUPPORTED_EXTENSIONS)}"
            )

        try:
            raw_content = self.path.read_text(encoding="utf-8")
        except OSError as e:
            raise CustomSourceError(f"Failed to read file: {e}") from e

        try:
            if suffix == ".json":
                data = json.loads(raw_content)
            else:  # .yaml or .yml
                data = yaml.safe_load(raw_content)
        except (json.JSONDecodeError, yaml.YAMLError) as e:
            raise CustomSourceError(f"Failed to parse {suffix} file: {e}") from e

        if not isinstance(data, dict):
            raise CustomSourceError("Rulebook must be a JSON/YAML object at the top level")

        # Validate schema if present
        schema = data.get("$schema")
        if schema and schema != self.CURRENT_SCHEMA:
            logger.warning(
                f"Rulebook schema '{schema}' differs from current '{self.CURRENT_SCHEMA}'. "
                "Some features may not work correctly."
            )

        # Extract metadata
        self.name = data.get("name", self.source_id)
        self._version = data.get("version", "1.0")

        # Parse content
        content = data.get("content", data)  # Support both nested and flat structure

        self._parse_content(content)

        self._loaded = True
        self.loaded_at = datetime.now()

        logger.info(f"Loaded custom rulebook '{self.name}' from {self.path}: {self.stats_summary()}")

    def _parse_content(self, content: dict[str, Any]) -> None:
        """Parse all content sections from the rulebook."""
        # Classes
        for item in content.get("classes", []):
            try:
                cls = self._parse_class(item)
                self._classes[cls.index] = cls
            except ValidationError as e:
                logger.warning(f"Invalid class definition in {self.path}: {e}")

        # Subclasses
        for item in content.get("subclasses", []):
            try:
                subcls = self._parse_subclass(item)
                self._subclasses[subcls.index] = subcls
            except ValidationError as e:
                logger.warning(f"Invalid subclass definition in {self.path}: {e}")

        # Races
        for item in content.get("races", []):
            try:
                race = self._parse_race(item)
                self._races[race.index] = race
            except ValidationError as e:
                logger.warning(f"Invalid race definition in {self.path}: {e}")

        # Subraces
        for item in content.get("subraces", []):
            try:
                subrace = self._parse_subrace(item)
                self._subraces[subrace.index] = subrace
            except ValidationError as e:
                logger.warning(f"Invalid subrace definition in {self.path}: {e}")

        # Spells
        for item in content.get("spells", []):
            try:
                spell = self._parse_spell(item)
                self._spells[spell.index] = spell
            except ValidationError as e:
                logger.warning(f"Invalid spell definition in {self.path}: {e}")

        # Monsters
        for item in content.get("monsters", []):
            try:
                monster = self._parse_monster(item)
                self._monsters[monster.index] = monster
            except ValidationError as e:
                logger.warning(f"Invalid monster definition in {self.path}: {e}")

        # Feats
        for item in content.get("feats", []):
            try:
                feat = self._parse_feat(item)
                self._feats[feat.index] = feat
            except ValidationError as e:
                logger.warning(f"Invalid feat definition in {self.path}: {e}")

        # Backgrounds
        for item in content.get("backgrounds", []):
            try:
                bg = self._parse_background(item)
                self._backgrounds[bg.index] = bg
            except ValidationError as e:
                logger.warning(f"Invalid background definition in {self.path}: {e}")

        # Items
        for item in content.get("items", []):
            try:
                itm = self._parse_item(item)
                self._items[itm.index] = itm
            except ValidationError as e:
                logger.warning(f"Invalid item definition in {self.path}: {e}")

    def _ensure_index(self, data: dict, name_field: str = "name") -> dict:
        """Ensure the data has an index field, deriving from name if needed."""
        if "index" not in data:
            name = data.get(name_field, "unknown")
            data["index"] = name.lower().replace(" ", "-").replace("'", "")
        return data

    def _ensure_source(self, data: dict) -> dict:
        """Ensure the data has source field set to this source."""
        data["source"] = self.source_id
        return data

    # -------------------------------------------------------------------------
    # Friendly class / subclass format (features + spellcasting progressions)
    # -------------------------------------------------------------------------

    @staticmethod
    def _slug(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")

    @staticmethod
    def _features_by_level(raw: Any) -> dict[int, list[tuple[str, str]]]:
        """
        Accept either ``{"3": [{"name", "desc"}, "Name", ...]}`` or
        ``[{"name", "level", "desc"}, ...]``. ``desc`` may be a string or a
        list of paragraphs.
        """
        out: dict[int, list[tuple[str, str]]] = {}

        def add(level: Any, feat: Any) -> None:
            try:
                lvl = int(level)
            except (TypeError, ValueError):
                return
            if isinstance(feat, str):
                out.setdefault(lvl, []).append((feat, ""))
            elif isinstance(feat, dict) and feat.get("name"):
                desc = feat.get("desc", feat.get("description", ""))
                if isinstance(desc, list):
                    desc = "\n".join(str(d) for d in desc)
                out.setdefault(lvl, []).append((str(feat["name"]), str(desc or "")))

        if isinstance(raw, dict):
            for level, feats in raw.items():
                for feat in feats if isinstance(feats, list) else [feats]:
                    add(level, feat)
        elif isinstance(raw, list):
            for feat in raw:
                if isinstance(feat, dict):
                    add(feat.get("level"), feat)
        return out

    @staticmethod
    def _spellcasting(raw: Any) -> dict[str, Any] | None:
        """
        Normalise a spellcasting block. Accepts the model shape plus:

        - ``ability`` (alias of ``spellcasting_ability``)
        - ``progression``: full | half | third | pact | artificer | 1/2 | 1/3
          (fills ``spell_slots`` and ``caster_type`` from the standard tables)
        - ``spell_slots`` as ``{level: [1st, 2nd, ...]}`` or
          ``{level: {slot_level: count}}``
        """
        if not isinstance(raw, dict):
            return None
        sc = dict(raw)
        if "ability" in sc and "spellcasting_ability" not in sc:
            sc["spellcasting_ability"] = sc.pop("ability")
        sc["spellcasting_ability"] = str(sc.get("spellcasting_ability", "INT")).upper()[:3]
        progression = sc.pop("progression", None) or sc.get("caster_type")
        slots = normalize_slot_table(sc.get("spell_slots")) if sc.get("spell_slots") else {}
        if not slots and progression:
            slots = standard_slot_table(progression)
        if slots:
            sc["spell_slots"] = slots
        if progression:
            sc["caster_type"] = caster_type_for(progression)
        if normalize_progression(progression) == "pact":
            sc.setdefault("pact_magic", True)
        sc.setdefault("level", first_casting_level(slots, 1))
        return sc

    def _normalize_class_like(self, data: dict, levels_key: str) -> dict:
        data = dict(data)
        feats = self._features_by_level(data.pop("features", None))
        levels: dict[Any, Any] = dict(data.get(levels_key) or {})
        for lvl, entries in feats.items():
            info = dict(levels.pop(lvl, None) or levels.pop(str(lvl), None) or {})
            info.setdefault("level", lvl)
            info["features"] = list(info.get("features", []))
            info["feature_details"] = dict(info.get("feature_details", {}))
            for name, desc in entries:
                if name not in info["features"]:
                    info["features"].append(name)
                if desc:
                    info["feature_details"][name] = desc
            levels[lvl] = info
        fixed: dict[int, Any] = {}
        for key, info in levels.items():
            try:
                lvl = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(info, dict):
                info = dict(info)
                info.setdefault("level", lvl)
                info.setdefault("proficiency_bonus", (lvl - 1) // 4 + 2)
            fixed[lvl] = info
        if fixed:
            data[levels_key] = fixed
        if data.get("spellcasting") is not None:
            data["spellcasting"] = self._spellcasting(data["spellcasting"])
        return data

    def _parse_class(self, data: dict) -> ClassDefinition:
        """Parse a class definition (model shape or friendly ``features`` shape)."""
        data = self._ensure_index(data)
        data = self._ensure_source(data)
        data = self._normalize_class_like(data, "class_levels")
        return ClassDefinition.model_validate(data)

    def _parse_subclass(self, data: dict) -> SubclassDefinition:
        """
        Parse a subclass definition.

        Besides the model shape, accepts ``class`` (alias of ``parent_class``,
        a class index or name), ``features`` keyed by level, and a
        ``spellcasting`` block with a ``progression``. Example::

            {"name": "Echo Knight", "class": "fighter",
             "features": {"3": [{"name": "Manifest Echo", "desc": "..."}]}}
        """
        data = self._ensure_index(data)
        data = self._ensure_source(data)
        if "parent_class" not in data and data.get("class"):
            data["parent_class"] = self._slug(data.pop("class"))
        elif data.get("parent_class"):
            data["parent_class"] = self._slug(data["parent_class"])
        data = self._normalize_class_like(data, "subclass_levels")
        return SubclassDefinition.model_validate(data)

    def _parse_race(self, data: dict) -> RaceDefinition:
        """Parse a race definition."""
        data = self._ensure_index(data)
        data = self._ensure_source(data)
        return RaceDefinition.model_validate(data)

    def _parse_subrace(self, data: dict) -> SubraceDefinition:
        """Parse a subrace definition."""
        data = self._ensure_index(data)
        data = self._ensure_source(data)
        return SubraceDefinition.model_validate(data)

    def _parse_spell(self, data: dict) -> SpellDefinition:
        """Parse a spell definition."""
        data = self._ensure_index(data)
        data = self._ensure_source(data)
        return SpellDefinition.model_validate(data)

    def _parse_monster(self, data: dict) -> MonsterDefinition:
        """Parse a monster definition."""
        data = self._ensure_index(data)
        data = self._ensure_source(data)
        return MonsterDefinition.model_validate(data)

    def _parse_feat(self, data: dict) -> FeatDefinition:
        """Parse a feat definition."""
        data = self._ensure_index(data)
        data = self._ensure_source(data)
        return FeatDefinition.model_validate(data)

    def _parse_background(self, data: dict) -> BackgroundDefinition:
        """Parse a background definition."""
        data = self._ensure_index(data)
        data = self._ensure_source(data)
        return BackgroundDefinition.model_validate(data)

    def _parse_item(self, data: dict) -> ItemDefinition:
        """Parse an item definition."""
        data = self._ensure_index(data)
        data = self._ensure_source(data)
        return ItemDefinition.model_validate(data)

    # =========================================================================
    # Query Methods
    # =========================================================================

    def get_class(self, index: str) -> ClassDefinition | None:
        return self._classes.get(index.lower())

    def get_subclass(self, index: str) -> SubclassDefinition | None:
        return self._subclasses.get(index.lower())

    def get_race(self, index: str) -> RaceDefinition | None:
        return self._races.get(index.lower())

    def get_subrace(self, index: str) -> SubraceDefinition | None:
        return self._subraces.get(index.lower())

    def get_spell(self, index: str) -> SpellDefinition | None:
        return self._spells.get(index.lower())

    def get_monster(self, index: str) -> MonsterDefinition | None:
        return self._monsters.get(index.lower())

    def get_feat(self, index: str) -> FeatDefinition | None:
        return self._feats.get(index.lower())

    def get_background(self, index: str) -> BackgroundDefinition | None:
        return self._backgrounds.get(index.lower())

    def get_item(self, index: str) -> ItemDefinition | None:
        return self._items.get(index.lower())

    def search(
        self,
        query: str,
        categories: list[str] | None = None,
        limit: int = 20,
        class_filter: str | None = None,
    ) -> Iterator[SearchResult]:
        """Search across all content.

        Args:
            query: Search term (case-insensitive, partial match)
            categories: Filter to specific categories
            limit: Maximum number of results
            class_filter: Filter spells by class (e.g., "ranger", "wizard")
        """
        query_lower = query.lower()
        class_filter_lower = class_filter.lower() if class_filter else None
        count = 0

        # Define category -> (storage, category_name) mapping
        category_map = {
            "class": (self._classes, "class"),
            "subclass": (self._subclasses, "subclass"),
            "race": (self._races, "race"),
            "subrace": (self._subraces, "subrace"),
            "spell": (self._spells, "spell"),
            "monster": (self._monsters, "monster"),
            "feat": (self._feats, "feat"),
            "background": (self._backgrounds, "background"),
            "item": (self._items, "item"),
        }

        # Filter to requested categories
        if categories:
            search_categories = [(k, v) for k, v in category_map.items() if k in categories]
        else:
            search_categories = list(category_map.items())

        for category_name, (storage, cat) in search_categories:
            for index, item in storage.items():
                if count >= limit:
                    return

                # Apply class filter for spells
                if class_filter_lower and cat == "spell":
                    spell_classes = getattr(item, "classes", [])
                    if class_filter_lower not in [c.lower() for c in spell_classes]:
                        continue

                # Match by query (or return all if query is empty and class_filter is set)
                if query_lower:
                    if query_lower not in index and query_lower not in item.name.lower():
                        continue
                elif not class_filter_lower:
                    # If no query and no class_filter, skip (need at least one filter)
                    continue

                yield SearchResult(
                    index=item.index,
                    name=item.name,
                    category=cat,  # type: ignore
                    source=self.source_id,
                    summary=getattr(item, "desc", [None])[0] if hasattr(item, "desc") else None,
                )
                count += 1

    def content_counts(self) -> ContentCounts:
        """Get counts of all content types."""
        return ContentCounts(
            classes=len(self._classes),
            subclasses=len(self._subclasses),
            races=len(self._races),
            subraces=len(self._subraces),
            spells=len(self._spells),
            monsters=len(self._monsters),
            feats=len(self._feats),
            backgrounds=len(self._backgrounds),
            items=len(self._items),
        )


__all__ = [
    "CustomSource",
    "CustomSourceError",
]
