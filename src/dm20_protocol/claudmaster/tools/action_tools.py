"""
Action processing tools for Claudmaster AI DM system.

This module provides the player_action MCP tool for processing player input
through the multi-agent orchestrator pipeline, returning structured responses
with narrative, state changes, dice rolls, and NPC interactions.
"""

import logging
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..orchestrator import (
    Orchestrator,
    IntentType,
    PlayerIntent,
    OrchestratorResponse,
    AgentTimeoutError,
    AgentExecutionError,
    IntentClassificationError,
)
from ..session import ClaudmasterSession
from ..recovery.error_messages import ErrorMessageFormatter
from .session_tools import _session_manager, mock_mode_warning

logger = logging.getLogger("dm20-protocol")


# ============================================================================
# Response Models
# ============================================================================

class DiceRoll(BaseModel):
    """Represents a dice roll that occurred during action processing."""
    dice: str = Field(description="Dice notation (e.g., '1d20+5', '2d6')")
    result: int = Field(description="The numeric result of the dice roll")
    purpose: str = Field(description="Why this roll was made (e.g., 'attack roll', 'perception check')")


class NPCResponse(BaseModel):
    """Represents an NPC's reaction or response to player action."""
    npc_name: str = Field(description="Name of the NPC responding")
    dialogue: Optional[str] = Field(default=None, description="NPC dialogue, if any")
    reaction: Optional[str] = Field(default=None, description="NPC's reaction (e.g., 'surprised', 'hostile')")
    attitude_change: Optional[str] = Field(
        default=None,
        description="Change in NPC attitude (e.g., 'friendly -> neutral')"
    )


class StateChange(BaseModel):
    """Represents a change to game state resulting from player action."""
    entity: str = Field(description="What changed (character name, location, quest, etc.)")
    field: str = Field(description="What field changed (e.g., 'hp', 'location', 'inventory')")
    old_value: Optional[str] = Field(default=None, description="Previous value (if applicable)")
    new_value: str = Field(description="New value after the change")


class ActionType(str, Enum):
    """Classification of player action type."""
    COMBAT = "combat"
    ROLEPLAY = "roleplay"
    EXPLORATION = "exploration"
    PUZZLE = "puzzle"
    SKILL_CHECK = "skill_check"
    REST = "rest"
    INVENTORY = "inventory"
    MIXED = "mixed"


class ActionResponse(BaseModel):
    """Complete response from processing a player action."""
    narrative: str = Field(description="The DM's narrative response to the action")
    action_type: ActionType = Field(description="Type of action that was processed")
    state_changes: list[StateChange] = Field(
        default_factory=list,
        description="List of game state changes resulting from the action"
    )
    dice_rolls: list[DiceRoll] = Field(
        default_factory=list,
        description="List of dice rolls made during action resolution"
    )
    npc_responses: list[NPCResponse] = Field(
        default_factory=list,
        description="List of NPC reactions or responses"
    )
    follow_up_options: Optional[list[str]] = Field(
        default=None,
        description="Suggested follow-up actions or options for the player"
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="List of warnings or error messages"
    )
    character_name: Optional[str] = Field(
        default=None,
        description="Name of the character who performed the action"
    )
    turn_number: int = Field(
        default=0,
        description="Turn number when this action was processed"
    )
    mock_mode: bool = Field(
        default=False,
        description="True when no live Claude model is connected (placeholder output)"
    )


# ============================================================================
# Action Processor
# ============================================================================

class ActionProcessor:
    """
    Processes player actions through the orchestrator pipeline.

    This class handles the translation of player input into structured action
    responses, coordinating intent classification, agent execution, and response
    aggregation.
    """

    def __init__(self, session_manager) -> None:
        """
        Initialize the ActionProcessor.

        Args:
            session_manager: The SessionManager instance managing active sessions
        """
        self.session_manager = session_manager
        self.error_formatter = ErrorMessageFormatter()

    def _get_active_session(self, session_id: str) -> tuple[Orchestrator, ClaudmasterSession]:
        """
        Get orchestrator and session for active session_id.

        Args:
            session_id: The session ID to retrieve

        Returns:
            Tuple of (Orchestrator, ClaudmasterSession)

        Raises:
            ValueError: If session_id is not found in active sessions
        """
        if session_id not in self.session_manager._active_sessions:
            raise ValueError(f"Session {session_id} not found in active sessions")

        return self.session_manager._active_sessions[session_id]

    def _map_intent_to_action_type(self, intent: PlayerIntent) -> ActionType:
        """
        Map IntentType to ActionType enum.

        Args:
            intent: The classified player intent

        Returns:
            Corresponding ActionType
        """
        mapping = {
            IntentType.COMBAT: ActionType.COMBAT,
            IntentType.ROLEPLAY: ActionType.ROLEPLAY,
            IntentType.EXPLORATION: ActionType.EXPLORATION,
            IntentType.QUESTION: ActionType.SKILL_CHECK,
            IntentType.SYSTEM: ActionType.INVENTORY,
            IntentType.ACTION: ActionType.MIXED,
        }
        return mapping.get(intent.intent_type, ActionType.MIXED)

    def _extract_npc_responses(self, orchestrator_response: OrchestratorResponse) -> list[NPCResponse]:
        """
        Extract NPC interactions from agent responses metadata.

        Args:
            orchestrator_response: The orchestrator's aggregated response

        Returns:
            List of NPCResponse objects extracted from agent metadata
        """
        npc_responses: list[NPCResponse] = []

        for agent_response in orchestrator_response.agent_responses:
            # Check if agent has NPC interaction data in observations
            if "npc_interactions" in agent_response.observations:
                interactions = agent_response.observations["npc_interactions"]
                if isinstance(interactions, list):
                    for interaction in interactions:
                        if isinstance(interaction, dict):
                            npc_responses.append(NPCResponse(**interaction))

            # Also check metadata for legacy format
            if "npc_responses" in agent_response.metadata:
                responses = agent_response.metadata["npc_responses"]
                if isinstance(responses, list):
                    for response in responses:
                        if isinstance(response, dict):
                            npc_responses.append(NPCResponse(**response))

        return npc_responses

    def _extract_dice_rolls(self, orchestrator_response: OrchestratorResponse) -> list[DiceRoll]:
        """
        Extract dice rolls from agent responses metadata.

        Args:
            orchestrator_response: The orchestrator's aggregated response

        Returns:
            List of DiceRoll objects extracted from agent metadata
        """
        dice_rolls: list[DiceRoll] = []

        for agent_response in orchestrator_response.agent_responses:
            # Check if agent has dice roll data in observations
            if "dice_rolls" in agent_response.observations:
                rolls = agent_response.observations["dice_rolls"]
                if isinstance(rolls, list):
                    for roll in rolls:
                        if isinstance(roll, dict):
                            dice_rolls.append(DiceRoll(**roll))

            # Also check metadata for legacy format
            if "dice_rolls" in agent_response.metadata:
                rolls = agent_response.metadata["dice_rolls"]
                if isinstance(rolls, list):
                    for roll in rolls:
                        if isinstance(roll, dict):
                            dice_rolls.append(DiceRoll(**roll))

        return dice_rolls

    def _extract_state_changes(self, orchestrator_response: OrchestratorResponse) -> list[StateChange]:
        """
        Convert raw state_changes dicts to StateChange models.

        Args:
            orchestrator_response: The orchestrator's aggregated response

        Returns:
            List of StateChange objects
        """
        state_changes: list[StateChange] = []

        for change_dict in orchestrator_response.state_changes:
            if isinstance(change_dict, dict):
                try:
                    state_changes.append(StateChange(**change_dict))
                except Exception as e:
                    logger.warning(f"Failed to parse state change {change_dict}: {e}")

        return state_changes

    async def process_action(
        self,
        session_id: str,
        action: str,
        character_name: Optional[str] = None,
        context: Optional[str] = None
    ) -> ActionResponse:
        """
        Main action processing method.

        This method orchestrates the complete player action workflow:
        1. Validate session is active
        2. Get orchestrator and session
        3. Scan player input for known terms and observe language preferences (terminology system)
        4. Classify intent via orchestrator (pure Python, zero tokens)
        5. If character_name provided, set context for PC identification
        6. Process through orchestrator pipeline
        7. Build ActionResponse from OrchestratorResponse with intent metadata
        8. Increment turn counter
        9. Return structured response

        Args:
            session_id: The active session ID
            action: The player's action text
            character_name: Optional name of character performing the action
            context: Optional additional context for the action

        Returns:
            ActionResponse with narrative, state changes, dice rolls, etc.
        """
        try:
            # Check for empty/whitespace input BEFORE processing
            if not action or not action.strip():
                return ActionResponse(
                    narrative=self.error_formatter.format_empty_input(),
                    action_type=ActionType.MIXED,
                    warnings=[],
                    turn_number=0
                )

            # Step 1 & 2: Validate and get session
            orchestrator, session = self._get_active_session(session_id)

            # Step 3: Scan player input for known terms and track language preferences
            if session_id in self.session_manager._term_resolvers:
                try:
                    term_resolver = self.session_manager._term_resolvers[session_id]
                    style_tracker = self.session_manager._style_trackers[session_id]

                    # Resolve terms in player input
                    matches = term_resolver.resolve_in_text(action)

                    # Observe language preferences
                    for original_text, term_entry in matches:
                        style_tracker.observe(term_entry, original_text)

                    if matches:
                        logger.debug(
                            f"[Terminology] Detected {len(matches)} terms in player input: "
                            f"{[term.canonical for _, term in matches]}"
                        )
                except Exception as e:
                    # Graceful degradation: terminology system failure doesn't break player_action
                    logger.warning(f"[Terminology] Error during term resolution: {e}")

            # Step 4: Classify intent BEFORE processing (deterministic, zero tokens)
            try:
                intent = orchestrator.classify_intent(action)

                logger.info(
                    "[Hybrid Python] Intent classified: %s (confidence: %.2f)",
                    intent.intent_type.value,
                    intent.confidence
                )
            except IntentClassificationError as e:
                # Ambiguous input - request clarification
                return ActionResponse(
                    narrative=self.error_formatter.format_ambiguous_input(action),
                    action_type=ActionType.MIXED,
                    warnings=[],
                    turn_number=session.turn_count
                )

            # Step 5: Build context if provided
            if character_name or context:
                # Store in session metadata for PC identification
                if character_name:
                    session.metadata["acting_character"] = character_name
                if context:
                    session.metadata["action_context"] = context

            # Step 6: Inject style preferences into session metadata for narrator context
            if session_id in self.session_manager._style_trackers:
                style_tracker = self.session_manager._style_trackers[session_id]
                style_prefs = style_tracker.preferences_summary()
                if style_prefs:
                    session.metadata["style_preferences"] = style_prefs
                    logger.debug(f"[Terminology] Injected style preferences into session metadata: {style_prefs}")

            # Step 7: Process through orchestrator
            try:
                orchestrator_response = await orchestrator.process_player_input(action)
            except AgentTimeoutError as e:
                # Agent timeout - provide degraded response using partial results
                logger.warning(f"Agent timeout during processing: {e}")
                partial_narrative = getattr(e, 'partial_narrative', None)
                return ActionResponse(
                    narrative=self.error_formatter.format_timeout_fallback(partial_narrative),
                    action_type=self._map_intent_to_action_type(intent),
                    warnings=[],
                    turn_number=session.turn_count
                )

            # Step 8: Build ActionResponse
            action_type = self._map_intent_to_action_type(intent)
            state_changes = self._extract_state_changes(orchestrator_response)
            dice_rolls = self._extract_dice_rolls(orchestrator_response)
            npc_responses = self._extract_npc_responses(orchestrator_response)

            # Extract follow-up options from metadata if available
            follow_up_options = orchestrator_response.metadata.get("follow_up_options")

            # Step 9: Turn counter already incremented by process_player_input
            turn_number = session.turn_count

            # Clean up temporary metadata
            session.metadata.pop("acting_character", None)
            session.metadata.pop("action_context", None)
            session.metadata.pop("style_preferences", None)

            # Surface mock mode loudly instead of passing off placeholder text
            mock_mode = bool(session.metadata.get("mock_mode"))
            warnings = (
                [mock_mode_warning(session.metadata.get("mock_reason"))] if mock_mode else []
            )

            # Create response with intent classification metadata
            response = ActionResponse(
                narrative=orchestrator_response.narrative,
                action_type=action_type,
                state_changes=state_changes,
                dice_rolls=dice_rolls,
                npc_responses=npc_responses,
                follow_up_options=follow_up_options,
                character_name=character_name,
                turn_number=turn_number,
                warnings=warnings,
                mock_mode=mock_mode,
            )

            # Return response as dict with added intent metadata (internal use)
            return response

        except ValueError as e:
            # Session not found or validation error
            logger.warning(f"Validation error in process_action: {e}")
            return ActionResponse(
                narrative=self.error_formatter.format_session_not_found(session_id),
                action_type=ActionType.MIXED,
                warnings=[],
                turn_number=0
            )

        except Exception as e:
            # All other unexpected errors - use formatter
            logger.error(f"Error processing action in session {session_id}: {e}", exc_info=True)
            return ActionResponse(
                narrative=self.error_formatter.format_error(e),
                action_type=ActionType.MIXED,
                warnings=[],
                turn_number=0
            )


# ============================================================================
# MCP Tool Function
# ============================================================================

async def player_action(
    session_id: str,
    action: str,
    character_name: Optional[str] = None,
    context: Optional[str] = None
) -> dict:
    """
    Process a player action in the current Claudmaster session.

    This is the main MCP tool function for player interaction with the AI DM.
    It processes the player's action through the multi-agent orchestrator and
    returns a structured response with narrative, state changes, dice rolls,
    and NPC interactions.

    **Hybrid Python Integration:** This tool uses Python's `Orchestrator.classify_intent()`
    to classify player input locally (zero LLM tokens consumed for classification).
    The intent classification result is included in the response metadata.

    Args:
        session_id: The active session ID to process the action in
        action: The player's action as natural language text
            Examples: "I attack the goblin with my sword"
                     "I try to persuade the guard to let us through"
                     "I search the room for traps"
        character_name: Optional name of the character performing the action
            Used for multi-PC parties to track which character acted
        context: Optional additional context about the action
            Useful for clarifying ambiguous actions or providing extra details

    Returns:
        Dictionary representation of ActionResponse with the following keys:
            - narrative: The DM's narrative response to the action
            - action_type: Type of action (combat, roleplay, exploration, etc.)
            - state_changes: List of game state changes (HP, location, inventory, etc.)
            - dice_rolls: List of dice rolls made during the action
            - npc_responses: List of NPC reactions or responses
            - follow_up_options: Suggested follow-up actions (if any)
            - warnings: Any warnings or errors encountered
            - character_name: Name of the acting character (if provided)
            - turn_number: Turn number when this action was processed
            - _intent_classification: Intent classification metadata (internal)
                - intent_type: Classified intent (combat, roleplay, exploration, etc.)
                - confidence: Classification confidence (0.0-1.0)
                - matched_patterns: List of patterns that matched
                - ambiguous: Whether classification was ambiguous
                - python_classified: Always True (indicates local Python classification)

    Examples:
        Basic action:
        >>> result = await player_action(
        ...     session_id="abc123",
        ...     action="I attack the orc with my longsword"
        ... )
        >>> # result["_intent_classification"]["intent_type"] == "combat"
        >>> # result["_intent_classification"]["python_classified"] == True

        Action with character name (multi-PC party):
        >>> result = await player_action(
        ...     session_id="abc123",
        ...     action="I cast fireball at the group of goblins",
        ...     character_name="Gandalf"
        ... )

        Action with context:
        >>> result = await player_action(
        ...     session_id="abc123",
        ...     action="I try to sneak past",
        ...     context="Using the shadows for cover"
        ... )
    """
    # Wrap entire function in error formatter for safety
    try:
        # Create processor instance
        processor = ActionProcessor(_session_manager)

        # Get orchestrator to classify intent BEFORE processing
        try:
            orchestrator, _ = processor._get_active_session(session_id)
            intent = orchestrator.classify_intent(action)

            # Build intent metadata for response
            intent_metadata = {
                "intent_type": intent.intent_type.value,
                "confidence": intent.confidence,
                "matched_patterns": intent.metadata.get("matched_patterns", []),
                "ambiguous": intent.metadata.get("ambiguous", False),
                "python_classified": True,  # Flag: intent was classified locally in Python
            }

            # Add alternative intent if ambiguous
            if intent.metadata.get("ambiguous"):
                intent_metadata["alternative_intent"] = intent.metadata.get("alternative_intent")
                intent_metadata["score_gap"] = intent.metadata.get("score_gap")

        except Exception as e:
            logger.warning(f"Failed to pre-classify intent: {e}")
            intent_metadata = {"python_classified": False, "error": str(e)}

        # Process the action
        response = await processor.process_action(
            session_id=session_id,
            action=action,
            character_name=character_name,
            context=context
        )

        # Return as dictionary with intent metadata
        response_dict = response.model_dump()
        response_dict["_intent_classification"] = intent_metadata

        return response_dict

    except Exception as e:
        # Ultimate safety net - format any unhandled exception
        logger.error(f"Unhandled error in player_action: {e}", exc_info=True)
        error_formatter = ErrorMessageFormatter()
        return {
            "narrative": error_formatter.format_error(e),
            "action_type": "mixed",
            "state_changes": [],
            "dice_rolls": [],
            "npc_responses": [],
            "follow_up_options": None,
            "warnings": [],
            "character_name": character_name,
            "turn_number": 0,
            "_intent_classification": {"python_classified": False, "error": str(e)}
        }


__all__ = [
    "DiceRoll",
    "NPCResponse",
    "StateChange",
    "ActionType",
    "ActionResponse",
    "ActionProcessor",
    "player_action",
]
