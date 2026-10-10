# 5etools Data: Schema Map and Loader Notes

dm20-protocol can load rules content from the
[5etools-mirror-3/5etools-src](https://github.com/5etools-mirror-3/5etools-src) data files
(`load_rulebook(source="5etools")`, also used by the global rulebook manager).
This document maps the 5etools JSON schema, says which parts the loader
(`src/dm20_protocol/rulebooks/sources/fivetools.py`) uses, which it ignores, and why.

> **Content and licensing.** Most 5etools data is non-SRD Wizards of the Coast text.
> dm20-protocol never ships it. Files are downloaded at runtime from the mirror into the
> user's cache directory (`rulebook_cache/5etools/`). Tests use small hand-written fixtures
> that only mirror the schema shape. Load the books you own; use `sources=[...]` to restrict.

Surveyed against mirror commit `8c026b8` (2026-10-08).

---

## 1. Files the loader downloads

| Category (merged cache file) | 5etools file(s) | Top-level keys used |
|---|---|---|
| `class` | `class/index.json` → `class/class-*.json` (15 files) | `class`, `subclass`, `classFeature`, `subclassFeature` |
| `spells` | `spells/index.json` → `spells/spells-*.json` (18 files) | `spell` |
| `spell-sources` | `spells/sources.json` | whole file: `{SRC: {Spell Name: {class: [...], classVariant: [...]}}}` |
| `bestiary` | `bestiary/index.json` → `bestiary/bestiary-*.json` (111 files) | `monster` |
| `legendarygroups` | `bestiary/legendarygroups.json` | `legendaryGroup` |
| `races` | `races.json` | `race`, `subrace` |
| `backgrounds` | `backgrounds.json` | `background` |
| `feats` | `feats.json` | `feat` |
| `optionalfeatures` | `optionalfeatures.json` | `optionalfeature` |
| `items` | `items.json` | `item` |
| `items-base` | `items-base.json` | `baseitem`, `itemProperty`, `itemMastery` |
| `magicvariants` | `magicvariants.json` | `magicvariant` |
| `conditionsdiseases` | `conditionsdiseases.json` | `condition`, `disease`, `status` |
| `actions` | `actions.json` | `action` |
| `variantrules` | `variantrules.json` | `variantrule` |

### Caching

```
{cache_dir}/raw/...         files exactly as published (same relative paths)
{cache_dir}/merged/*.json   one file per category (data key + extra keys + count)
{cache_dir}/metadata.json   downloaded_at, checked_at, schema_version, per-file ETags
```

* A fresh cache (younger than `cache_max_age_days`, default 14) loads with **no network**.
* A stale cache, or `refresh=True`, revalidates every file with `If-None-Match`; `304`
  keeps the cached copy. Failures fall back to the cached copy.
* A cache written by an older loader (`schema_version` < 2) is upgraded in place: raw
  files are reused, only missing files are fetched, merged files are rebuilt.
* A full load of the parsed models takes roughly 8 s from a warm cache.

### Options

```python
FiveToolsSource(cache_dir, sources=["XPHB", "XMM", "EGW"],   # allowlist (case-insensitive)
                exclude_sources=["UA"],                     # denylist
                edition="2024",                             # or "2014"
                refresh=False, cache_max_age_days=14,
                expand_magic_variants=True)
```

`load_rulebook(source="5etools", sources=..., exclude_sources=..., edition=..., refresh=...)`
exposes the same options. They are saved in the campaign's rulebook manifest.

---

## 2. Cross-cutting conventions

### Identity, `source`, `edition`, `reprintedAs`

Every entry has `name` + `source` (a book abbreviation: `PHB`, `XPHB`, `EGW`, `MM`, `XMM`, ...).
2024 ("one") content lives in `XPHB` / `XDMG` / `XMM` and carries `"edition": "one"`.
Older entries carry `"edition": "classic"` (or nothing) and often
`"reprintedAs": ["Name|XPHB"]`.

**Loader dedupe (reprint-aware).** For each category:

1. Apply the source allow/deny list.
2. With `edition="2024"`, an entry whose `reprintedAs` target is also loaded is moved to a
   *secondary* set. With `edition="2014"`, the reprint target is moved instead.
3. Remaining index collisions are ranked: preferred edition, then specific over generated
   items, then core books (PHB/DMG/MM/XPHB/XDMG/XMM), then SRD flag, then source name.
4. Primary entries own the plain index (`fireball`). Every entry is also reachable as
   `{index}-{source}` (`fireball-phb`, `fighter-xphb`). A superseded entry keeps its own
   index when nothing else claims it, so `get_monster("goblin")` still finds the 2014
   Goblin under a 2024 preference (whose reprint is named "Goblin Warrior").

The model fields `source_book` (`"EGW"`) and `edition` (`"2014"`/`"2024"`) record where
an entry came from. `source` stays `"5etools"` (the loader's id) for compatibility.

### `_copy` / `_mod` inheritance

About 1,150 monsters, 124 subclasses, 75 subclass features, 67 items, 26 backgrounds and
16 races are defined as copies:

```json
{"name": "Chukka", "source": "BGDIA",
 "_copy": {"name": "Kenku", "source": "MM",
           "_mod": {"*": {"mode": "replaceTxt", "replace": "the kenku", "with": "Chukka", "flags": "i"}},
           "_preserve": {"page": true}}}
```

`fivetools_copy.py` re-implements the 5etools `copyApplier`:

* The base is matched on every identity key in `_copy` (for example `className`,
  `classSource` or `shortName` for subclasses). A blank `classSource` defaults to PHB.
  Copies of copies resolve recursively, with a cycle guard.
* Some properties are only inherited when `_preserve` lists them (or `"*"`). These are
  `page`, `srd`, `reprintedAs`, `otherSources`, `hasFluff*`, `legendaryGroup`,
  `environment`, `variant`, `lootTables`, `tier` and a few others.
* Mods apply in this order: `"*"` (all of `action`, `bonus`, `reaction`, `trait`,
  `legendary`, `mythic`, `variant`, `spellcasting` and the header props), then `"_"`
  (root), then named or dotted props (`"inherits.entries"`).
* The supported modes cover every mode the data uses: `replaceTxt` (tag-aware),
  `replaceName`, `prependArr`, `appendArr`, `appendIfNotExistsArr`, `replaceArr`,
  `replaceOrAppendArr`, `insertArr`, `removeArr`, `renameArr`, `setProp`, `appendStr`,
  `prefixSuffixStringProp`, `scalarAddProp`, `scalarMultProp`,
  `addSkills`/`addSaves`/`addAll*`, `addSenses`, `addSpells`/`replaceSpells`/`removeSpells`,
  `scalarAddHit`, `scalarAddDc`, `maxSize`, `scalarMultXp` and the string mode `"remove"`.
* `<$name$>`, `<$short_name$>`, `<$title_short_name$>`, `<$spell_dc__cha$>`,
  `<$to_hit__str$>` and `<$damage_mod__str$>` variables are resolved from the copied entity.

`_versions` (85 monsters plus some items and races) declare alternate stat blocks. Each
version becomes a copy of its parent; `_abstract` + `_implementations` templates are
expanded with their `{{variable}}` values. Versions are registered as separate monsters,
for example `test-wyrm-young`.

**Not applied:** `_templates` / `_trait` (monster templates from `bestiary/template.json`).
They affect 187 copies. The copy is still produced, only without the template's mods.

### `{@tag ...}` inline markup

`fivetools_utils.strip_tags()` mirrors the `_getStripped` behaviour of 5etools'
`Renderer.tag` classes. It resolves nested tags innermost first and keeps the
**display text**:

| Tag form | Result |
|---|---|
| `{@spell fireball\|XPHB}`, `{@item longsword\|phb\|a long blade}` | name, or 3rd part if present (`a long blade`) |
| `{@creature commoner\|\|commoners}` | `commoners` |
| `{@class Fighter\|PHB\|fighters}` / `{@deity N\|P\|S\|disp}` / `{@subclass ...}` / `{@classFeature ...}` / `{@subclassFeature ...}` / `{@quickref ...}` | display at the tag's own index (3/4/5/6/8/5) |
| `{@filter text\|page\|...}`, `{@book text\|ID}`, `{@adventure ...}`, `{@link ...}`, `{@5etools ...}` | first part |
| `{@atk mw,rw}` / `{@atkr m}` | `Melee or Ranged Weapon Attack:` / `Melee Attack Roll:` |
| `{@h}` `{@m}` `{@hom}` | `Hit: ` `Miss: ` `Hit or Miss: ` |
| `{@hit 4}` `{@d20 -1}` | `+4` `-1` |
| `{@dc 15}` | `DC 15` |
| `{@damage 2d6 + 3}` `{@dice 1d20+5\|+5}` | `2d6 + 3` `+5` |
| `{@scaledamage 8d6\|3-9\|1d6}` | `1d6` |
| `{@recharge 5}` `{@recharge}` | `(Recharge 5–6)` `(Recharge 6)` |
| `{@actSave wis}` `{@actSaveFail}` `{@actSaveFail 2}` `{@actSaveSuccess}` `{@actTrigger}` `{@actResponse}` | `Wisdom Saving Throw:` `Failure:` `Second Failure:` `Success:` `Trigger:` `Response:` |
| `{@chance 25}` `{@ability str 20}` `{@skillCheck animal_handling 5}` | `25 percent` `20 (+5)` `Animal Handling +5` |
| `{@b ...}` `{@i ...}` `{@note ...}` and other style tags | inner text |

The legacy `convert_5etools_markup()` (used by the adventure parser) is now a thin wrapper
around `strip_tags`. The only difference is that it renders `{@h}`/`{@m}`/`{@hom}` as empty
strings, which keeps the adventure tests unchanged.

`render_entries()` flattens the recursive `entries` structure. It handles `entries`,
`section`, `inset`, `variant*`, `quote`, `list` and `item`, `table` (caption, labels and
rows, with `cell` rolls), `options`, `abilityDc`, `abilityAttackMod`, `inline*`, `dice`,
`bonus` and `spellcasting`. `refClassFeature`/`refSubclassFeature`/`refOptionalfeature` go
through an optional resolver. Images and galleries are dropped.

---

## 3. Classes — `data/class/class-*.json`

Each file holds `class` (both editions, 30 total), `subclass` (330), `classFeature` (677)
and `subclassFeature` (1,499).

### `class`

| Field | Meaning | Loader |
|---|---|---|
| `hd.faces` | hit die | `hit_die` |
| `proficiency` | saving throws (`["str","con"]`) | `saving_throws` |
| `startingProficiencies` | `armor`/`weapons`/`tools` strings (may contain tags), `skills: [{choose: {from, count}}]` | `proficiencies`, `proficiency_choices.skills` |
| `startingEquipment` | 2014: `default` (strings); 2024: `entries` / `defaultData` (A/B/C packs) | `starting_equipment` (text lines); `defaultData` ignored |
| `multiclassing` | `requirements` (`{"or": [{...}]}`), `proficienciesGained` | `multi_classing` |
| `classFeatures` | refs `Name\|Class\|ClassSource\|Level[\|Source]` or `{classFeature, gainSubclassFeature: true}` | features by level; the lowest `gainSubclassFeature` level becomes `subclass_level` |
| `subclassTitle` | "Martial Archetype", "Fighter Subclass" | `subclass_title` |
| `casterProgression` | `full`, `1/2`, `1/3`, `pact`, `artificer` (half, rounding up, slots from level 1; used by the artificer and the 2024 paladin/ranger) | `SpellcastingInfo.caster_type` (`artificer` → `half`) |
| `spellcastingAbility` | `int`/`wis`/`cha` | `spellcasting_ability` |
| `cantripProgression` | 20 numbers | `cantrips_known` |
| `spellsKnownProgression` / `preparedSpellsProgression` | 20 numbers (2014 known casters / 2024 prepared) | `spells_known` (known, else prepared), `prepared_spells` |
| `classTableGroups[]` | `{colLabels, rows}` or `{title, colLabels, rowsSpellProgression}` | see below |
| `optionalfeatureProgression` | `[{name, featureType: ["EI"], progression: {"2": 2, ...} or [..20]}]` | `optional_feature_progression` |
| `edition`, `reprintedAs` | 2014 class → `["Fighter\|XPHB"]` | edition dedupe |
| `featProgression`, `primaryAbility`, `preparedSpellsChange`, `spellsKnownProgressionFixed*`, `additionalSpells` (class-level) | 2024 extras / wizard spellbook rules | ignored |

**Spell slots.** `rowsSpellProgression` holds 20 rows of slots per spell level and becomes
`SpellcastingInfo.spell_slots[level] = [1st, 2nd, ...]` with trailing zeros trimmed.
Pact Magic has no `rowsSpellProgression`. The loader finds the `Spell Slots` and
`Slot Level` columns in a regular `rows` group. Slot level is a tag such as
`{@filter 3rd|spells|...}`. A warlock at level 5 becomes `[0, 0, 2]` with
`pact_magic=True`. Without either table, the standard progression for `casterProgression`
is used (`rulebooks/spell_progression.py`: full, half, artificer-style half, third, pact).
`SpellcastingInfo.slots_at(level)` returns `{slot_level: count}`.

The other table columns (Rage Damage, Sneak Attack, Invocations Known, ...) become
`ClassLevelInfo.class_specific` with snake_case keys. Cells can be numbers, strings or
`{type: dice|bonus|bonusSpeed}`.

### `classFeature` / `subclassFeature`

Each one has `name`, `source`, `className`, `classSource`, `level` and `entries`. Subclass
features also have `subclassShortName` and `subclassSource`. A feature's entries can embed
`{"type": "refClassFeature" | "refSubclassFeature"}`. Echo Knight's level-3 entry, for
example, references *Manifest Echo* and *Unleash Incarnation*. The loader hoists each
reference into its own feature at the referenced level. `refOptionalfeature` inside
`options` renders as a list of choices. Features flagged `isClassFeatureVariant` (TCE
optional class features) are not auto-granted. They are listed under
`class_specific["optional_features"]`.

Feature lookup key: `(name, className, classSource, level, source)`. Blank `classSource`
means PHB. A blank source means the class (or subclass) source.

### `subclass`

| Field | Loader |
|---|---|
| `name`, `shortName` (`"College of Lore"` / `"Lore"`) | `name`, `short_name`; index = slug(`shortName`) |
| `className`, `classSource` | attaches to the class with that name **and** source |
| `subclassFeatures` refs `Name\|Class\|ClassSource\|Short\|SubSource\|Level[\|Source]` | `subclass_levels[level].features` / `.feature_details`. The first feature, named like the subclass, is the intro and becomes `desc` |
| `casterProgression` + `subclassTableGroups` (Eldritch Knight, Arcane Trickster: `1/3`) | `SubclassDefinition.spellcasting` with its own slots |
| `additionalSpells[0].prepared/expanded/known` | `class_specific["always_prepared_spells"]`, `["expanded_spells"]`, `["bonus_known_spells"]` |
| `optionalfeatureProgression` (Battle Master maneuvers) | ignored (also present on the class progression) |
| `reprintedAs` (`"Champion\|Fighter\|XPHB\|XPHB"` = name\|class\|classSource\|source) | edition dedupe |

5etools re-hosts every legacy subclass on the 2024 class through `_copy` with
`classSource: "XPHB"`. That is how Echo Knight (EGW) appears under the 2024 Fighter. The
subclasses of the preferred-edition class are mapped. `ClassDefinition.subclasses` holds
their short-name slugs (`"echo-knight"`), which is what `level_up_character` validates
against. Subclass lookup accepts `echo-knight`, `fighter-echo-knight`,
`way-of-shadow`-style full names and `{class}-{slug}-{source}`. Short names collide across
classes (Monk *Shadow* vs Sorcerer *Shadow*). The second one is stored as
`sorcerer-shadow`.

---

## 4. Other categories

| File / key | Used fields | Ignored (why) |
|---|---|---|
| `spells/*.json` `spell` | `level`, `school` (letter), `time`, `range`, `components` (`m` string/object), `duration` (timed/concentration/permanent), `meta.ritual`, `entries`, `entriesHigherLevel`, `damageInflict`, `savingThrow` | `scalingLevelDice`, `areaTags`, `miscTags`, `conditionInflict`, `affectsCreatureType` (filter metadata) |
| `spells/sources.json` | `class` + `classVariant` lists → `SpellDefinition.classes` (needed by `search_rules(class_filter=...)`) | — |
| `generated/gendata-spell-source-lookup.json` | — | duplicates `sources.json` and adds subclass/feat/item grants; `subclasses` stays empty |
| `bestiary/*.json` `monster` | `size`, `type` (string / `{type, tags, swarmSize}` / `{choose}`), `alignment` (codes, `{alignment, chance}`, `{special}`), `ac` (ints / `{ac, from, condition}`), `hp` (`average`/`formula` / `special`), `speed`, ability scores, `save`, `skill`, `senses`, `passive`, `languages`, `immune`/`resist`/`vulnerable`/`conditionImmune` (strings or nested `{immune: [...], note}`), `cr` (string or `{cr, lair, xp, xpLair}`), `trait`, `action`, `bonus`, `reaction`, `legendary`, `legendaryHeader`, `legendaryActions`, `mythic`, `spellcasting` (`headerEntries`, `will`, `daily`, `spells{lvl: {slots, spells}}`), `legendaryGroup`, `environment` | `variant` insets, `dragonCastingColor`, token/fluff/sound fields, `*Tags` filter metadata, `gear`, `treasure`, `initiative` |
| — structured attacks | `{@hit N}` → `MonsterAction.attack_bonus`; each `{@damage XdY+Z}` plus the following damage word → `damage: [{damage_dice, damage_type}]` (SRD shape); `{@dc N}` with `{@actSave abl}` or "Dexterity saving throw" → `dc: {dc_value, dc_type}` | rider conditions (prose only) |
| `bestiary/legendarygroups.json` | `lairActions` → `lair_actions`, `regionalEffects` → `regional_effects` | `mythicEncounter` |
| `bestiary/template.json` | — | monster templates (`_templates`), see §2 |
| `fluff-*.json` | — | lore and images only, not mechanics; large |
| `races.json` `race` | `size` (list), `speed`, `ability` (fixed + `choose` → `ability_bonus_options`), `age`, `languageProficiencies`, `skillProficiencies`, named `entries` → traits | `darkvision`/`resist`/`traitTags` (already in trait text), `additionalSpells` (2024 lineages), `_versions` variants (e.g. dragonborn colours) |
| `races.json` `subrace` | `name` + `raceName`/`raceSource` → index `high-elf`, ability, traits | nameless "base" subraces (they only modify the parent) |
| `backgrounds.json` | skill/tool proficiencies; 2014 `Feature: X` entry → `feature`; 2024 `ability.choose.weighted.from` → `ability_scores`, `feats` (`"magic initiate; cleric\|xphb"` → `Magic Initiate (Cleric)`) | `startingEquipment` A/B packs (the text is in `desc`), personality tables |
| `feats.json` | `category` (O/G/FS/EB/D/DG), `prerequisite` (level, ability, race, spellcasting, feature, proficiency, other → `prerequisites` + `prerequisite_text`), `ability` → increases/choice, `repeatable` | `additionalSpells`, `optionalfeatureProgression`, `resist`, `expertise` (in text) |
| `optionalfeatures.json` | `featureType` (EI invocation, MV:B maneuver, FS:F/B/P/R fighting style, MM metamagic, PB pact boon, AI infusion, AS arcane shot, ED elemental discipline, RN rune, ...) → `OptionalFeatureDefinition`, `prerequisite` → text | `consumes`, `additionalSpells`. 2024 fighting styles are feats (`category: FS`) |
| `items-base.json` | `baseitem` (type `M\|XPHB` style codes, `weaponCategory`, `dmg1`/`dmg2`/`dmgType`, `property`, `range`, `ac`, `strength`, `stealth`, `value` in cp, `weight`), `itemProperty` (names) | `itemMastery` text, `itemType`/`itemEntry` (renderer helpers) |
| `items.json` | magic and mundane items; `baseItem` merges weapon/armor stats from the base; `rarity`, `reqAttune` (bool or text), `bonusWeapon`/`bonusAc`/`bonusSpellAttack`..., `charges` | `itemGroup` (grouping only), `attachedSpells`, `lootTables` |
| `magicvariants.json` | generic variants (`+1 Weapon`, `Armor of Resistance`): `requires` (OR list of property matches, e.g. `{"type": "HA\|XPHB"}`, `{"sword": true}`), `excludes`, `inherits` (namePrefix/Suffix, rarity, bonuses, entries with `{=bonusWeapon}`, `{=baseName/l}`, `{=dmgType}` templates) → about 6,000 generated items such as `+1 Longsword`; the generic entry is kept too | `valueExpression`/`weightExpression`, `propertyRemove`, `linkedLootTables`; disable with `expand_magic_variants=False` |
| `conditionsdiseases.json` | `condition`, `disease`, `status` → `RuleDefinition(category=...)` | — |
| `actions.json`, `variantrules.json` | → `RuleDefinition(category="action" / "variantrule")` | `seeAlsoAction`, `ruleType` |
| `deities.json` | — | 563 pantheon entries (flavour, not rules) |
| `adventures.json`, `adventure/*.json`, `books.json` | — | handled by `dm20_protocol.adventures` (index + chapter parser), not the rulebook loader |
| `bastions.json`, `vehicles.json`, `objects.json`, `trapshazards.json`, `psionics.json`, `cultsboons.json`, `rewards.json`, `recipes.json`, `decks.json`, `tables.json`, `languages.json`, `charcreationoptions.json` | — | out of scope for the rules layer today; candidates for later |
| `foundry*.json`, `makebrew-*`, `converter.json`, `renderdemo.json`, `changelog.json` | — | tooling data |

### What callers can rely on

* `ClassDefinition.subclasses`: short-name slugs (`echo-knight`).
  `RulebookManager.get_subclasses_for(cls)` returns the definitions, merged across sources.
* `ClassDefinition.class_levels[1..20]`: features, feature text and table columns.
* `SpellcastingInfo.spell_slots`: `{class_level: [1st, 2nd, ...]}`, including Pact Magic.
  The manager fills an empty table from `caster_type` for sources that lack one.
* `MonsterDefinition`: `xp`, `challenge_rating`, `hit_points`, `armor_class[0].value`,
  structured `actions[].attack_bonus/damage/dc`, `legendary_actions`, `lair_actions`.

---

## 5. User content packs (`load_rulebook(source="custom", path=...)`)

Homebrew or owned non-SRD content goes in a JSON/YAML pack. Classes and subclasses accept a
friendly format on top of the model shape:

```json
{
  "$schema": "dm20-protocol/rulebook-v1",
  "name": "My Owned Content",
  "content": {
    "subclasses": [
      {
        "name": "Echo Knight",
        "class": "fighter",
        "features": {
          "3": [{"name": "Manifest Echo", "desc": "Your text here."},
                {"name": "Unleash Incarnation", "desc": ["Paragraph 1", "Paragraph 2"]}],
          "7": [{"name": "Echo Avatar", "desc": "..."}]
        }
      },
      {
        "name": "Arcane Knight",
        "parent_class": "fighter",
        "features": [{"name": "Weapon Bond", "level": 3, "desc": "..."}],
        "spellcasting": {"ability": "INT", "progression": "third"}
      }
    ],
    "classes": [
      {
        "name": "Spellsword", "hit_die": 8, "saving_throws": ["STR", "INT"],
        "features": {"1": ["Arcane Strike"], "2": [{"name": "Channel", "desc": "..."}]},
        "spellcasting": {
          "ability": "INT",
          "progression": "half",
          "spell_slots": {"2": {"1": 2}, "3": [3]}
        }
      }
    ]
  }
}
```

* `class` / `parent_class`: the parent class index or name.
* `features`: either `{level: [names or {name, desc}]}` or `[{name, level, desc}]`.
  These become `subclass_levels` / `class_levels`, with the proficiency bonus filled in.
* `spellcasting.progression`: one of `full`, `half` (`1/2`), `third` (`1/3`), `pact` or
  `artificer`. It sets `caster_type` and the standard slot table. An explicit
  `spell_slots` table wins. It can be `{level: [1st, 2nd, ...]}` or
  `{level: {slot_level: count}}`.
* A pack subclass attaches to a class from **any** loaded source.
  `RulebookManager.get_class("fighter").subclasses` then includes `echo-knight` on top of
  the SRD Fighter, so `level_up_character(..., subclass="Echo Knight")` validates.
