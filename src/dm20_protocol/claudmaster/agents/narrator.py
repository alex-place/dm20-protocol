"""
Narrator Agent for the Claudmaster multi-agent system.

The Narrator is responsible for generating evocative scene descriptions,
NPC dialogue stubs, and atmospheric text. It uses an LLM (Claude API)
for text generation and supports configurable narrative styles.

Implements the ReAct pattern: reason about what description is needed,
generate it via the LLM, then observe/validate the output quality.
"""

import logging
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from ..base import Agent, AgentRole
from ..llm_client import call_llm, supports_extended_api

logger = logging.getLogger("dm20-protocol")


# ------------------------------------------------------------------
# Terminology style hint formatting
# ------------------------------------------------------------------

def format_style_hint(preferences: dict[str, str]) -> str:
    """Format language preferences into a natural-language hint for the LLM.

    Converts the StyleTracker preferences summary into a readable prompt
    fragment that instructs the LLM to mirror the player's language style.

    Args:
        preferences: Dict mapping category to language ("en" or "it")
            Example: {"spell": "en", "skill": "it", "combat": "it"}

    Returns:
        A formatted hint string suitable for injection into the narrator prompt.
        Returns empty string if preferences dict is empty.

    Example:
        >>> format_style_hint({"spell": "en", "skill": "it"})
        "Player language preferences (mirror their style):\n- Spells: English (e.g., 'Fireball' not 'Palla di Fuoco')\n- Skills: Italian (e.g., 'Furtività' not 'Stealth')"
    """
    if not preferences:
        return ""

    # Category name mapping for better readability
    category_names = {
        "spell": "Spells",
        "skill": "Skills",
        "ability": "Abilities",
        "condition": "Conditions",
        "combat": "Combat terms",
        "item": "Items",
        "class": "Classes",
        "race": "Races",
        "general": "General terms",
    }

    # Language name mapping
    lang_names = {"en": "English", "it": "Italian"}

    # Build lines
    lines = ["Player language preferences (mirror their style):"]
    for category, lang in sorted(preferences.items()):
        category_display = category_names.get(category, category.capitalize())
        lang_display = lang_names.get(lang, lang.upper())

        # Add examples for common categories
        examples = {
            ("spell", "en"): "(e.g., 'Fireball' not 'Palla di Fuoco')",
            ("spell", "it"): "(e.g., 'Palla di Fuoco' not 'Fireball')",
            ("skill", "en"): "(e.g., 'Stealth' not 'Furtività')",
            ("skill", "it"): "(e.g., 'Furtività' not 'Stealth')",
            ("combat", "en"): "(e.g., 'Initiative' not 'Iniziativa')",
            ("combat", "it"): "(e.g., 'Iniziativa' not 'Initiative')",
        }
        example = examples.get((category, lang), "")

        lines.append(f"- {category_display}: {lang_display} {example}".strip())

    return "\n".join(lines)


# ------------------------------------------------------------------
# Narrative styles
# ------------------------------------------------------------------

class NarrativeStyle(str, Enum):
    """Supported narrative styles for scene descriptions."""
    DESCRIPTIVE = "descriptive"  # Rich, detailed prose
    TERSE = "terse"              # Brief, action-focused
    DRAMATIC = "dramatic"        # Theatrical, tension-building
    MYSTERIOUS = "mysterious"    # Hints and atmosphere


# ------------------------------------------------------------------
# Style-specific narrative guidance
# ------------------------------------------------------------------
# Rationale: Instead of just passing the style name to the LLM,
# each style gets concrete instructions on tone, sentence structure,
# sensory focus, and pacing. This ensures the LLM produces
# noticeably different output per style, not just "more or less words."

STYLE_GUIDES: dict[str, str] = {
    NarrativeStyle.DESCRIPTIVE: (
        "Write in rich, layered prose. Engage at least three senses per scene — sight, sound, "
        "smell, touch, or taste. Build depth through specific, concrete details: the particular "
        "shade of moss on a wall, the exact timbre of a merchant's voice, the weight of humid "
        "air. Use varied sentence lengths — long flowing descriptions punctuated by short, "
        "punchy observations. You have space; use it to paint a world that rewards curiosity."
    ),
    NarrativeStyle.TERSE: (
        "Be sharp, direct, and economical. Every word must earn its place. Favor short "
        "declarative sentences and active voice. Focus on what matters right now: threats, "
        "exits, objects of interest. No adjective chains, no poetic flourishes. Think field "
        "report, not novel. One strong sensory detail per scene is enough. Leave whitespace "
        "for the player's imagination."
    ),
    NarrativeStyle.DRAMATIC: (
        "Write with theatrical intensity. Build tension through contrast — silence before "
        "thunder, stillness before violence, beauty beside decay. Use rhetorical devices: "
        "foreshadowing, dramatic irony, callback to earlier events. Lean into emotional "
        "extremes — dread, exhilaration, awe, grief. Pace your reveals; the most important "
        "detail comes last. Make the player feel the weight of the moment."
    ),
    NarrativeStyle.MYSTERIOUS: (
        "Suggest more than you reveal. Describe what is absent or wrong — the silence where "
        "there should be sound, the shadow that moved against the light, the door that was "
        "open yesterday but stands locked today. Use questions and half-answers. Favor "
        "ambiguity and implication over certainty. The player should finish each description "
        "with more questions than answers. Let unease seep through the cracks."
    ),
}


# ------------------------------------------------------------------
# LLM Client protocol
# ------------------------------------------------------------------

class LLMClient(Protocol):
    """Protocol for LLM interaction, enabling easy mocking in tests."""

    async def generate(self, prompt: str, max_tokens: int = 1024) -> str:
        """Generate text from a prompt.

        Args:
            prompt: The full prompt to send to the LLM.
            max_tokens: Maximum tokens in the response.

        Returns:
            The generated text.
        """
        ...


# ------------------------------------------------------------------
# Prompt templates
# ------------------------------------------------------------------

_NARRATOR_GUIDANCE = """\
When describing a new place, character, or situation for the first time, give the players multiple \
threads to pull on: curious details, half-noticed oddities, things that beg questions. On follow-up \
requests for more detail, narrow your focus precisely to what was asked, and calibrate the depth of \
revealed information to the difficulty of any check involved — not every secret is freely given.

IMPORTANT: If discovery context is provided above, you MUST respect it. Only describe features the \
party has actually discovered. For GLIMPSED features, use vague and uncertain language. For EXPLORED \
features, describe fully. For FULLY MAPPED features, include mechanical and tactical detail. \
For hidden features, weave the sensory hints naturally into the scene without revealing the feature \
itself. Never explicitly mention features the party hasn't discovered.

Occasionally, without forcing it, weave in fragments of history or culture — a faded crest on a \
wall, a local superstition muttered by a passerby, the architectural echo of a fallen empire — so \
the world feels lived-in and layered beyond the immediate scene.

Adapt your emotional register to the situation. Match the tone to what the scene demands — mystery \
when the unknown stretches ahead, dread in dark threatening places, triumph when heroes prevail.

Never follow a predictable pattern. Vary your sentence structure, your openings, your rhythm. \
Sometimes begin mid-action, sometimes with a single sensory detail, sometimes with dialogue or \
silence. The players should never feel they are reading output from a template — they should feel \
a living voice telling their story.
"""

SCENE_DESCRIPTION_TEMPLATE = """\
You are the Narrator of a D&D campaign. Your task: {reasoning}

{style_guide}

{style_hint}

{discovery_context}

""" + _NARRATOR_GUIDANCE

# Frozen system prompt for clients that support system/context separation.
# It depends only on the narrative style, so it stays byte-identical for the
# whole session and is served from the prompt cache.
NARRATOR_SYSTEM_TEMPLATE = """\
You are the Narrator of a D&D campaign.

{style_guide}

""" + _NARRATOR_GUIDANCE

# Volatile per-turn input, sent last (after any cached campaign state).
NARRATOR_TURN_TEMPLATE = """\
Your task: {reasoning}

{style_hint}

{discovery_context}
"""

DIALOGUE_TEMPLATE = """\
You are generating NPC dialogue for a D&D campaign.

NPC: {npc_name}
Description: {description}
Occupation: {occupation}
Attitude: {attitude}
Voice Profile:
  - Speech pattern: {speech_pattern}
  - Vocabulary level: {vocabulary_level}
  - Accent/Regional hints: {accent_hints}
  - Quirks: {quirks}
  - Emotional baseline: {emotional_baseline}

Scene Context:
  - Location: {location}
  - Topic: {topic}
  - Mood: {mood}
  - Listeners: {listeners}
  - Recent events: {recent_events}

Current tone: {tone}

Generate a single line of dialogue for {npc_name}. CRITICAL voice differentiation rules:

1. **Speech pattern is law.** A terse guard speaks in clipped fragments. A scholarly wizard uses \
subordinate clauses and precise terminology. A crude bandit drops articles and swears. The pattern \
must be audible in every line — if you remove the speaker name, the reader should still know who \
is talking.

2. **Vocabulary defines social class.** "Simple" speakers use short, common words and concrete \
metaphors ("strong as an ox"). "Educated" speakers use abstractions and formal structures. \
"Scholarly" speakers cite, qualify, and hedge. "Street" speakers use slang and abbreviations.

3. **Quirks are mandatory.** If the NPC has quirks listed, at least one must appear in every line. \
A character who "stammers when nervous" MUST stammer. A character who "quotes proverbs" MUST \
quote one. These are the hooks that make NPCs memorable.

4. **Body language is character.** The stage direction reveals personality: a nervous character \
fidgets; a confident one takes up space; a deceptive one avoids eye contact. Always include a \
stage direction that reinforces who this person IS.

5. **Subtext over text.** What the NPC means may differ from what they say. A "hostile" NPC can \
be coldly polite. A "friendly" NPC can be overwhelmingly pushy. Attitude colors the HOW, not \
the WHAT.

Format your response as:
{npc_name}: "The dialogue text here" [stage direction showing body language/action]
"""

CONVERSATION_TEMPLATE = """\
You are generating a multi-party conversation for a D&D campaign.

Participants:
{participants_info}

Scene:
  - Location: {location}
  - Topic: {topic}
  - Number of exchanges: {num_exchanges}

Generate a natural conversation with {num_exchanges} exchanges (each participant speaks once per exchange). \
Each line should match the speaker's voice profile and personality.

Format your response as:
NPC_NAME: "dialogue text" [optional stage direction]
NPC_NAME: "dialogue text" [optional stage direction]
...

Keep the conversation natural, with characters reacting to each other's statements.
"""

RECAP_TEMPLATE = """\
You are the Narrator of a D&D campaign. A player is resuming their session. Generate a \
"Previously on..." recap using ONLY the verified facts below — do not invent or embellish \
beyond what is stated.

Current Location: {location}
Active Quests: {active_quests}
Recent Events:
{recent_events}
Party Status: {party_status}

Generate a concise, atmospheric recap (2-3 short paragraphs) that:
1. Reminds the player where they are and what they were doing
2. Highlights the most significant recent events
3. Ends with a hook suggesting what to do next
4. Is written in second person ("You...")

Keep it under 200 words. Be vivid but factual.
"""


# ------------------------------------------------------------------
# Dialogue models
# ------------------------------------------------------------------

class VoiceProfile(BaseModel):
    """Voice characteristics for an NPC."""
    speech_pattern: str = "casual"  # formal, casual, archaic, crude, etc.
    vocabulary_level: str = "common"  # simple, common, educated, scholarly
    accent_hints: str = ""  # regional speech patterns
    quirks: list[str] = Field(default_factory=list)  # catchphrases, verbal tics
    emotional_baseline: str = "calm"  # calm, excitable, melancholic, etc.


class DialogueLine(BaseModel):
    """A single line of NPC dialogue."""
    speaker_name: str
    text: str
    tone: str = "neutral"  # friendly, hostile, nervous, etc.
    stage_direction: str = ""  # optional action/emotion note


class DialogueContext(BaseModel):
    """Context for dialogue generation."""
    speaker_name: str
    speaker_description: str = ""
    speaker_attitude: str = "neutral"
    speaker_occupation: str = ""
    listeners: list[str] = Field(default_factory=list)
    location: str = ""
    topic: str = ""
    mood: str = "neutral"
    recent_events: list[str] = Field(default_factory=list)


class Conversation(BaseModel):
    """A multi-line conversation between characters."""
    participants: list[str]
    lines: list[DialogueLine] = Field(default_factory=list)
    scene_description: str = ""


# ------------------------------------------------------------------
# NarratorAgent
# ------------------------------------------------------------------

class NarratorAgent(Agent):
    """Agent responsible for narrative scene descriptions.

    Uses an LLM to generate atmospheric text based on the current game
    context, location, recent events, and the configured narrative style.

    Args:
        llm: An object implementing the LLMClient protocol.
        style: The narrative style to use for descriptions.
        max_tokens: Maximum tokens for LLM responses.
    """

    def __init__(
        self,
        llm: LLMClient,
        style: NarrativeStyle = NarrativeStyle.DESCRIPTIVE,
        max_tokens: int = 1024,
        multi_turn: bool = False,
    ) -> None:
        super().__init__(name="narrator", role=AgentRole.NARRATOR)
        self.llm = llm
        self.style = style
        self.max_tokens = max_tokens
        self.multi_turn = multi_turn
        self.conversation: Any = None  # ConversationSession, created lazily
        self._voice_profiles: dict[str, VoiceProfile] = {}  # cache for consistency
        self._current_style_preferences: dict[str, str] = {}  # populated during reason() phase
        self._current_discovery_context: str = ""  # populated during reason() phase
        self._current_scene: str | None = None  # populated during reason() phase
        self._current_turn_state: str | None = None  # populated during reason() phase

    async def reason(self, context: dict[str, Any]) -> str:
        """Analyze context to determine what kind of description is needed.

        Examines the player action, current location, and recent events
        to decide on the appropriate narrative response type.

        Args:
            context: Game context dict with keys like 'player_action',
                'location', 'recent_events', 'setting', 'style_preferences',
                'discovery_context', etc.

        Returns:
            A reasoning string describing the intended narrative approach.
        """
        player_action = context.get("player_action", "")
        location = context.get("location", {})
        location_name = location.get("name", "unknown location") if isinstance(location, dict) else str(location)

        # Store style preferences for use in act() phase
        self._current_style_preferences = context.get("style_preferences", {})

        # Store discovery context for use in act() phase
        self._current_discovery_context = context.get("discovery_context", "")

        # Scene type (drives per-turn effort) and per-turn state for multi-turn mode
        game_state = context.get("game_state") or {}
        in_combat = bool(game_state.get("in_combat")) if isinstance(game_state, dict) else False
        self._current_scene = self._classify_scene(player_action, in_combat)
        self._current_turn_state = self._format_turn_state(location_name, game_state)

        # Determine the narrative task
        if not player_action:
            return f"No player action provided. Generating ambient description for {location_name}."

        # Classify what kind of narrative response is appropriate
        action_words = set(player_action.lower().split())
        if action_words & {"look", "examine", "inspect", "observe", "search"}:
            return f"Player is observing. Generate detailed description of {location_name}."
        elif action_words & {"enter", "go", "move", "walk", "travel"}:
            return f"Player is moving. Generate transition scene to/within {location_name}."
        elif action_words & {"talk", "speak", "ask", "greet", "converse", "chat", "question"}:
            return f"Player initiating dialogue. Generate NPC dialogue for {location_name}."
        else:
            return f"Player action: '{player_action}'. Narrate the result at {location_name}."

    async def act(self, reasoning: str) -> Any:
        """Generate the narrative description using the LLM.

        Builds a prompt from the reasoning and context, then calls the
        LLM to generate atmospheric text.

        Args:
            reasoning: Output from the reason() phase.

        Returns:
            The generated narrative text string.
        """
        if not supports_extended_api(self.llm):
            prompt = self._build_prompt(reasoning)
            response = await self.llm.generate(prompt, max_tokens=self.max_tokens)
            return response.strip()

        turn_input = self._build_turn_input(reasoning)
        if self.multi_turn and hasattr(self.llm, "start_conversation"):
            if self.conversation is None:
                self.conversation = self.llm.start_conversation(
                    system=self.build_system_prompt(), max_tokens=self.max_tokens,
                )
            result = await self.conversation.send(
                turn_input, turn_state=self._current_turn_state, scene=self._current_scene,
            )
            return result.text.strip()

        response = await call_llm(
            self.llm, turn_input, self.max_tokens, system=self.build_system_prompt(),
        )
        return response.strip()

    # -- system / turn prompt split (cache-friendly path) ------------------

    def build_system_prompt(self) -> str:
        """Frozen system prompt: instructions + style guide (no per-turn data)."""
        style_guide = STYLE_GUIDES.get(self.style, STYLE_GUIDES[NarrativeStyle.DESCRIPTIVE])
        return NARRATOR_SYSTEM_TEMPLATE.format(style_guide=style_guide)

    def _build_turn_input(self, reasoning: str) -> str:
        style_hint = format_style_hint(self._current_style_preferences)
        return NARRATOR_TURN_TEMPLATE.format(
            reasoning=reasoning,
            style_hint=style_hint,
            discovery_context=self._current_discovery_context or "",
        ).strip()

    @staticmethod
    def _classify_scene(player_action: str, in_combat: bool) -> str:
        if in_combat:
            return "combat"
        words = set(player_action.lower().split())
        if words & {"buy", "sell", "shop", "trade", "haggle"}:
            return "shopping"
        if words & {"travel", "journey", "ride", "walk", "go", "enter", "move"}:
            return "travel"
        if words & {"talk", "speak", "ask", "greet", "persuade", "chat"}:
            return "social"
        return "exploration"

    @staticmethod
    def _format_turn_state(location_name: str, game_state: Any) -> str | None:
        if not isinstance(game_state, dict) or not game_state:
            return None
        parts = [f"Location: {location_name}"]
        if game_state.get("in_combat"):
            parts.append("In combat: yes")
            if game_state.get("current_turn"):
                parts.append(f"Current turn: {game_state['current_turn']}")
        for key in ("current_date_in_game", "party_level"):
            if game_state.get(key):
                parts.append(f"{key.replace('_', ' ').capitalize()}: {game_state[key]}")
        return "Current game state — " + "; ".join(parts)

    async def observe(self, result: Any) -> dict[str, Any]:
        """Validate and annotate the generated narrative.

        Checks basic quality metrics and returns observations about
        the generated text.

        Args:
            result: The narrative text from act().

        Returns:
            Dict with observations: word_count, style, has_dialogue, etc.
        """
        text = str(result)
        return {
            "word_count": len(text.split()),
            "style": self.style.value,
            "has_dialogue": '"' in text or "\u201c" in text,
            "empty": len(text.strip()) == 0,
        }

    def _build_prompt(self, reasoning: str) -> str:
        """Build the full LLM prompt from reasoning and the template.

        Args:
            reasoning: The reasoning output describing what to narrate.

        Returns:
            Complete prompt string ready for the LLM.
        """
        # Format style hint from current preferences
        style_hint = format_style_hint(self._current_style_preferences)
        if style_hint:
            style_hint = f"\n{style_hint}\n"  # Add spacing

        # Get style-specific guidance (falls back to descriptive)
        style_guide = STYLE_GUIDES.get(self.style, STYLE_GUIDES[NarrativeStyle.DESCRIPTIVE])

        # Format discovery context
        discovery_context = self._current_discovery_context or ""

        return SCENE_DESCRIPTION_TEMPLATE.format(
            reasoning=reasoning,
            style_guide=style_guide,
            style_hint=style_hint,
            discovery_context=discovery_context,
        )

    def build_voice_profile(
        self,
        npc_name: str,
        description: str | None = None,
        occupation: str | None = None,
        attitude: str | None = None,
        bio: str | None = None,
    ) -> VoiceProfile:
        """Build voice profile from NPC attributes. Cache for consistency.

        Derives voice characteristics from NPC traits. For example:
        - Merchants: formal speech, sales-oriented
        - Guards: terse, authoritative
        - Scholars: educated vocabulary, formal
        - Thieves: casual, slang-heavy

        Args:
            npc_name: The NPC's name (used as cache key).
            description: Physical or personality description.
            occupation: The NPC's job or role.
            attitude: Current attitude (friendly, hostile, etc.).
            bio: Backstory and motivations.

        Returns:
            A VoiceProfile with derived speech characteristics.
        """
        # Return cached profile if available
        if npc_name in self._voice_profiles:
            return self._voice_profiles[npc_name]

        # Derive voice characteristics from occupation and attitude
        speech_pattern = "casual"
        vocabulary_level = "common"
        emotional_baseline = "calm"
        quirks: list[str] = []

        occupation_lower = (occupation or "").lower()
        attitude_lower = (attitude or "neutral").lower()

        # Derive from occupation — extended archetypes for rich NPC differentiation
        if any(word in occupation_lower for word in ["merchant", "trader", "shopkeeper"]):
            speech_pattern = "formal"
            vocabulary_level = "common"
            quirks = ["steers every topic toward commerce", "names exact prices"]
        elif any(word in occupation_lower for word in ["guard", "soldier", "captain"]):
            speech_pattern = "terse"
            vocabulary_level = "common"
            quirks = ["clips sentences short", "gives orders not requests"]
        elif any(word in occupation_lower for word in ["scholar", "wizard", "sage", "librarian"]):
            speech_pattern = "formal"
            vocabulary_level = "scholarly"
            quirks = ["corrects others' terminology", "cites obscure references"]
        elif any(word in occupation_lower for word in ["thief", "rogue", "pickpocket", "smuggler"]):
            speech_pattern = "casual"
            vocabulary_level = "common"
            quirks = ["speaks in coded euphemisms", "deflects direct questions"]
        elif any(word in occupation_lower for word in ["noble", "lord", "lady", "baron", "duke", "count"]):
            speech_pattern = "formal"
            vocabulary_level = "educated"
            quirks = ["refers to others by full title", "never asks—implies or commands"]
        elif any(word in occupation_lower for word in ["innkeeper", "bartender", "tavern"]):
            speech_pattern = "casual"
            vocabulary_level = "common"
            quirks = ["calls everyone 'friend' or a nickname", "shares unsolicited local gossip"]
        elif any(word in occupation_lower for word in ["priest", "cleric", "monk", "acolyte"]):
            speech_pattern = "formal"
            vocabulary_level = "educated"
            quirks = ["weaves deity references into conversation", "blesses or invokes divine will"]
        elif any(word in occupation_lower for word in ["peasant", "farmer", "laborer", "miner"]):
            speech_pattern = "casual"
            vocabulary_level = "simple"
            quirks = ["uses weather and harvest metaphors", "distrusts anything complicated"]
        elif any(word in occupation_lower for word in ["bard", "minstrel", "performer", "entertainer"]):
            speech_pattern = "theatrical"
            vocabulary_level = "educated"
            quirks = ["quotes songs and poems mid-conversation", "gestures dramatically"]
        elif any(word in occupation_lower for word in ["blacksmith", "smith", "armorer", "forge"]):
            speech_pattern = "blunt"
            vocabulary_level = "common"
            quirks = ["judges everything by craftsmanship", "speaks with quiet pride"]
        elif any(word in occupation_lower for word in ["healer", "herbalist", "apothecary", "doctor"]):
            speech_pattern = "measured"
            vocabulary_level = "educated"
            quirks = ["diagnoses everything including moods", "prescribes remedies unprompted"]
        elif any(word in occupation_lower for word in ["beggar", "orphan", "urchin", "homeless"]):
            speech_pattern = "pleading"
            vocabulary_level = "simple"
            quirks = ["speaks in half-finished sentences", "flinches at sudden movements"]
        elif any(word in occupation_lower for word in ["pirate", "sailor", "captain", "corsair"]):
            speech_pattern = "boisterous"
            vocabulary_level = "common"
            quirks = ["uses nautical metaphors for everything", "laughs too loud"]
        elif any(word in occupation_lower for word in ["assassin", "spy", "agent", "informant"]):
            speech_pattern = "controlled"
            vocabulary_level = "educated"
            quirks = ["never reveals more than necessary", "watches the room while talking"]
        elif any(word in occupation_lower for word in ["druid", "ranger", "woodsman", "hunter"]):
            speech_pattern = "sparse"
            vocabulary_level = "common"
            quirks = ["uses animal and nature comparisons", "uncomfortable with crowds"]
        elif any(word in occupation_lower for word in ["child", "kid", "boy", "girl", "youth"]):
            speech_pattern = "excitable"
            vocabulary_level = "simple"
            quirks = ["asks too many questions", "changes subject abruptly"]
        elif any(word in occupation_lower for word in ["elder", "crone", "ancient", "old"]):
            speech_pattern = "deliberate"
            vocabulary_level = "educated"
            quirks = ["trails off into memories", "speaks in proverbs and warnings"]

        # Adjust for attitude
        if "hostile" in attitude_lower or "aggressive" in attitude_lower:
            emotional_baseline = "aggressive"
            quirks.append("threatening")
        elif "friendly" in attitude_lower or "helpful" in attitude_lower:
            emotional_baseline = "warm"
            quirks.append("welcoming")
        elif "nervous" in attitude_lower or "fearful" in attitude_lower:
            emotional_baseline = "anxious"
            quirks.append("stammers")
        elif "suspicious" in attitude_lower or "wary" in attitude_lower:
            emotional_baseline = "guarded"
            quirks.append("evasive")

        profile = VoiceProfile(
            speech_pattern=speech_pattern,
            vocabulary_level=vocabulary_level,
            accent_hints="",
            quirks=quirks,
            emotional_baseline=emotional_baseline,
        )

        # Cache the profile
        self._voice_profiles[npc_name] = profile
        return profile

    async def generate_recap(
        self,
        location: str,
        active_quests: str,
        recent_events: str,
        party_status: str,
    ) -> str:
        """Generate an atmospheric session recap using the LLM.

        Formats verified session facts into a narrative "Previously on..."
        recap suitable for session resumption.

        Args:
            location: Current party location.
            active_quests: Summary of active quests/objectives.
            recent_events: Formatted string of recent significant events.
            party_status: Brief party condition summary (HP, conditions).

        Returns:
            Atmospheric recap text (2-3 paragraphs).
        """
        prompt = RECAP_TEMPLATE.format(
            location=location or "Unknown",
            active_quests=active_quests or "None active",
            recent_events=recent_events or "- No recent events recorded",
            party_status=party_status or "Unknown",
        )
        response = await self.llm.generate(prompt, max_tokens=self.max_tokens)
        return response.strip()

    async def generate_dialogue(
        self,
        dialogue_context: DialogueContext,
        tone_override: str | None = None,
    ) -> DialogueLine:
        """Generate a single line of NPC dialogue using LLM.

        Args:
            dialogue_context: Context information for dialogue generation.
            tone_override: Optional tone to override the context mood.

        Returns:
            A DialogueLine with the generated speech.
        """
        # Build or retrieve voice profile
        voice = self.build_voice_profile(
            npc_name=dialogue_context.speaker_name,
            description=dialogue_context.speaker_description,
            occupation=dialogue_context.speaker_occupation,
            attitude=dialogue_context.speaker_attitude,
        )

        # Build prompt
        prompt = self._build_dialogue_prompt(dialogue_context, voice, tone_override)

        # Generate dialogue
        response = await self.llm.generate(prompt, max_tokens=self.max_tokens)

        # Parse response
        return self._parse_dialogue_line(response, dialogue_context.speaker_name, tone_override)

    async def generate_conversation(
        self,
        participants: list[DialogueContext],
        topic: str,
        num_exchanges: int = 3,
    ) -> Conversation:
        """Generate a multi-party conversation.

        Args:
            participants: List of DialogueContext for each participant.
            topic: The conversation topic or prompt.
            num_exchanges: Number of back-and-forth exchanges (each participant speaks once per exchange).

        Returns:
            A Conversation with multiple DialogueLines.
        """
        # Build voice profiles for all participants
        voices = {
            ctx.speaker_name: self.build_voice_profile(
                npc_name=ctx.speaker_name,
                description=ctx.speaker_description,
                occupation=ctx.speaker_occupation,
                attitude=ctx.speaker_attitude,
            )
            for ctx in participants
        }

        # Build conversation prompt
        prompt = self._build_conversation_prompt(participants, voices, topic, num_exchanges)

        # Generate conversation
        response = await self.llm.generate(prompt, max_tokens=self.max_tokens * 2)

        # Parse response
        lines = self._parse_conversation(response)

        return Conversation(
            participants=[ctx.speaker_name for ctx in participants],
            lines=lines,
            scene_description="",
        )

    def _build_dialogue_prompt(
        self,
        context: DialogueContext,
        voice: VoiceProfile,
        tone_override: str | None = None,
    ) -> str:
        """Build LLM prompt for dialogue generation.

        Args:
            context: The dialogue context.
            voice: The NPC's voice profile.
            tone_override: Optional tone override.

        Returns:
            Formatted prompt string.
        """
        tone = tone_override or context.mood
        recent_events_str = "; ".join(context.recent_events) if context.recent_events else "None"
        listeners_str = ", ".join(context.listeners) if context.listeners else "None"
        quirks_str = ", ".join(voice.quirks) if voice.quirks else "None"

        return DIALOGUE_TEMPLATE.format(
            npc_name=context.speaker_name,
            description=context.speaker_description or "Unknown",
            occupation=context.speaker_occupation or "Unknown",
            attitude=context.speaker_attitude or "neutral",
            speech_pattern=voice.speech_pattern,
            vocabulary_level=voice.vocabulary_level,
            accent_hints=voice.accent_hints or "None",
            quirks=quirks_str,
            emotional_baseline=voice.emotional_baseline,
            location=context.location or "Unknown location",
            topic=context.topic or "General conversation",
            mood=context.mood,
            tone=tone,
            listeners=listeners_str,
            recent_events=recent_events_str,
        )

    def _build_conversation_prompt(
        self,
        participants: list[DialogueContext],
        voices: dict[str, VoiceProfile],
        topic: str,
        num_exchanges: int,
    ) -> str:
        """Build LLM prompt for multi-party conversation.

        Args:
            participants: List of participant contexts.
            voices: Voice profiles by name.
            topic: Conversation topic.
            num_exchanges: Number of exchanges.

        Returns:
            Formatted prompt string.
        """
        # Build participant info
        participants_info_lines = []
        for ctx in participants:
            voice = voices[ctx.speaker_name]
            quirks_str = ", ".join(voice.quirks) if voice.quirks else "None"
            participants_info_lines.append(
                f"- {ctx.speaker_name} ({ctx.speaker_occupation or 'Unknown'})\n"
                f"  Attitude: {ctx.speaker_attitude or 'neutral'}\n"
                f"  Speech: {voice.speech_pattern}, {voice.vocabulary_level} vocabulary\n"
                f"  Baseline: {voice.emotional_baseline}\n"
                f"  Quirks: {quirks_str}"
            )

        participants_info = "\n\n".join(participants_info_lines)
        location = participants[0].location if participants else "Unknown location"

        return CONVERSATION_TEMPLATE.format(
            participants_info=participants_info,
            location=location,
            topic=topic,
            num_exchanges=num_exchanges,
        )

    def _parse_dialogue_line(
        self,
        response: str,
        speaker_name: str,
        tone: str | None = None,
    ) -> DialogueLine:
        """Parse LLM response into a DialogueLine.

        Expected format:
        SPEAKER_NAME: "dialogue text" [stage direction]

        Args:
            response: Raw LLM response.
            speaker_name: Expected speaker name.
            tone: Optional tone.

        Returns:
            Parsed DialogueLine.
        """
        response = response.strip()

        # Try to extract dialogue and stage direction
        text = ""
        stage_direction = ""

        # Look for pattern: NAME: "text" [direction]
        if ":" in response:
            parts = response.split(":", 1)
            if len(parts) == 2:
                dialogue_part = parts[1].strip()

                # Extract quoted text
                if '"' in dialogue_part:
                    quote_start = dialogue_part.index('"')
                    quote_end = dialogue_part.rfind('"')
                    if quote_start < quote_end:
                        text = dialogue_part[quote_start + 1 : quote_end]

                        # Extract stage direction if present
                        after_quote = dialogue_part[quote_end + 1 :].strip()
                        if after_quote.startswith("[") and after_quote.endswith("]"):
                            stage_direction = after_quote[1:-1].strip()

        # Fallback: use entire response if parsing failed
        if not text:
            text = response

        return DialogueLine(
            speaker_name=speaker_name,
            text=text,
            tone=tone or "neutral",
            stage_direction=stage_direction,
        )

    def _parse_conversation(self, response: str) -> list[DialogueLine]:
        """Parse LLM response into multiple DialogueLines.

        Expected format:
        NAME: "text" [direction]
        NAME: "text" [direction]
        ...

        Args:
            response: Raw LLM response.

        Returns:
            List of parsed DialogueLines.
        """
        lines: list[DialogueLine] = []
        response_lines = response.strip().split("\n")

        for line in response_lines:
            line = line.strip()
            if not line or not ":" in line:
                continue

            # Extract speaker name
            parts = line.split(":", 1)
            speaker_name = parts[0].strip()

            # Parse the rest as a dialogue line
            dialogue_line = self._parse_dialogue_line(line, speaker_name)
            lines.append(dialogue_line)

        return lines


__all__ = [
    "NarratorAgent",
    "NarrativeStyle",
    "LLMClient",
    "SCENE_DESCRIPTION_TEMPLATE",
    "STYLE_GUIDES",
    "DIALOGUE_TEMPLATE",
    "CONVERSATION_TEMPLATE",
    "VoiceProfile",
    "DialogueLine",
    "DialogueContext",
    "Conversation",
    "format_style_hint",
]
