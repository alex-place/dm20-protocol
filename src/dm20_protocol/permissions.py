"""
Role-based permission system for multi-player campaigns.

This module implements an opt-in permission layer for MCP tool calls.
When no player_id is provided, the system operates in single-player DM mode
with full access and zero overhead. When player_id is present, permissions
are checked against the caller's role and entity ownership.

Key components:
- PlayerRole: Enum for DM, PLAYER, OBSERVER roles
- PermissionLevel: Enum for ALLOWED, DENIED, CONDITIONAL access
- PERMISSION_MATRIX: Dict mapping (role, tool_name) to permission levels
- PermissionResolver: Validates MCP tool calls against caller role and ownership
"""

import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("dm20-protocol")


class PlayerRole(str, Enum):
    """
    Player roles with increasing privilege levels.

    DM has full access to all tools. PLAYER can modify their own
    characters and read shared state. OBSERVER is read-only.
    """
    OBSERVER = "observer"
    PLAYER = "player"
    DM = "dm"


class PermissionLevel(str, Enum):
    """
    Permission level for a (role, tool) combination.

    ALLOWED: Unconditionally permitted.
    DENIED: Unconditionally blocked.
    CONDITIONAL: Permitted only if ownership check passes
                 (e.g., PLAYER modifying their own character).
    """
    ALLOWED = "allowed"
    DENIED = "denied"
    CONDITIONAL = "conditional"


class TemporaryPermission(BaseModel):
    """
    A time-limited permission grant from the DM.

    Allows temporary elevation of access for specific players
    and tool operations (e.g., letting a player update game state
    during a session).

    Attributes:
        player_id: The player receiving the permission
        tool_name: The tool this permission applies to
        granted_at: When the permission was granted
        expires_at: When the permission expires (None = session-scoped)
        granted_by: Who granted it (always the DM)
    """
    player_id: str
    tool_name: str
    granted_at: datetime = Field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None
    granted_by: str = "DM"


class PlayerRoleAssignment(BaseModel):
    """
    Maps a player_id to a PlayerRole, stored in campaign game state.

    Attributes:
        player_id: Unique identifier for the player
        role: The assigned role
        assigned_at: When the role was assigned
    """
    player_id: str
    role: PlayerRole = PlayerRole.PLAYER
    assigned_at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Permission Matrix
# ---------------------------------------------------------------------------
# Maps tool names to their default permission for each role.
# Tools not listed here default to ALLOWED for DM, DENIED for others.
#
# Categories:
#   READ tools: get_*, list_*, search_*, roll_dice, etc.
#   WRITE_OWN tools: character modification (conditional on ownership)
#   WRITE_SHARED tools: campaign state, NPCs, locations, quests
#   DM_ONLY tools: campaign management, combat control, NPC creation

# Read-only tools — accessible to all roles
_READ_TOOLS: set[str] = {
    "get_campaign_info",
    "list_campaigns",
    "get_character",
    "list_characters",
    "get_npc",
    "list_npcs",
    "get_location",
    "list_locations",
    "list_quests",
    "get_game_state",
    "get_sessions",
    "get_events",
    "list_rulebooks",
    "search_rules",
    "get_class_info",
    "get_race_info",
    "get_spell_info",
    "get_monster_info",
    "roll_dice",
    "calculate_experience",
    "show_map",
    "list_library",
    "get_library_toc",
    "search_library",
    "ask_books",
    "list_enabled_library",
    "list_packs",
    "validate_pack",
    "check_sheet_changes",
    "get_claudmaster_session_state",
    "discover_adventures",
}

# Character-modification tools — PLAYER gets CONDITIONAL (own character only)
_CHARACTER_TOOLS: set[str] = {
    "update_character",
    "add_item_to_character",
    "equip_item",
    "unequip_item",
    "remove_item",
    "use_spell_slot",
    "use_resource",
    "add_spell",
    "remove_spell",
    "long_rest",
    "short_rest",
    "add_death_save",
    "level_up_character",
    "export_character_sheet",
}

# DM-only tools — campaign management, world building, combat control
_DM_ONLY_TOOLS: set[str] = {
    "create_campaign",
    "load_campaign",
    "delete_campaign",
    "create_character",
    "delete_character",
    "bulk_update_characters",
    "create_npc",
    "update_npc",
    "create_location",
    "create_quest",
    "update_quest",
    "update_game_state",
    "start_combat",
    "end_combat",
    "next_turn",
    "combat_action",
    "update_combatant",
    "build_encounter_tool",
    "apply_effect",
    "remove_effect",
    "add_session_note",
    "summarize_session",
    "add_event",
    "load_rulebook",
    "unload_rulebook",
    "validate_character_rules",
    "open_library_folder",
    "scan_library",
    "extract_content",
    "enable_library_source",
    "disable_library_source",
    "configure_claudmaster",
    "start_claudmaster_session",
    "end_claudmaster_session",
    "player_action",
    "load_adventure",
    "sync_all_sheets",
    "approve_sheet_change",
    "export_pack",
    "import_pack",
    "send_private_message",
}


def _build_permission_matrix() -> dict[str, dict[PlayerRole, PermissionLevel]]:
    """
    Build the full permission matrix from the categorized tool sets.

    Returns:
        Dict mapping tool_name -> {role: permission_level}
    """
    matrix: dict[str, dict[PlayerRole, PermissionLevel]] = {}

    for tool in _READ_TOOLS:
        matrix[tool] = {
            PlayerRole.DM: PermissionLevel.ALLOWED,
            PlayerRole.PLAYER: PermissionLevel.ALLOWED,
            PlayerRole.OBSERVER: PermissionLevel.ALLOWED,
        }

    for tool in _CHARACTER_TOOLS:
        matrix[tool] = {
            PlayerRole.DM: PermissionLevel.ALLOWED,
            PlayerRole.PLAYER: PermissionLevel.CONDITIONAL,
            PlayerRole.OBSERVER: PermissionLevel.DENIED,
        }

    for tool in _DM_ONLY_TOOLS:
        matrix[tool] = {
            PlayerRole.DM: PermissionLevel.ALLOWED,
            PlayerRole.PLAYER: PermissionLevel.DENIED,
            PlayerRole.OBSERVER: PermissionLevel.DENIED,
        }

    return matrix


PERMISSION_MATRIX: dict[str, dict[PlayerRole, PermissionLevel]] = _build_permission_matrix()


class PermissionResolver:
    """
    Validates MCP tool calls against caller role and entity ownership.

    The resolver is designed for zero overhead in single-player mode:
    when player_id is None, check_permission() returns True immediately
    without any dict lookups or logic.

    In multi-player mode, the resolver checks:
    1. The caller's role (DM/PLAYER/OBSERVER)
    2. The tool's permission level for that role
    3. For CONDITIONAL tools, whether the caller owns the target entity

    Attributes:
        _role_assignments: Maps player_id -> PlayerRole
        _temp_permissions: List of active temporary permission grants
        _character_ownership: Maps character entity ID -> player_id
    """

    def __init__(self) -> None:
        """Initialize an empty PermissionResolver."""
        self._role_assignments: dict[str, PlayerRole] = {}
        self._temp_permissions: list[TemporaryPermission] = []
        self._character_ownership: dict[str, str] = {}

    def set_player_role(self, player_id: str, role: PlayerRole) -> None:
        """
        Assign a role to a player.

        Args:
            player_id: Unique player identifier
            role: The role to assign
        """
        self._role_assignments[player_id] = role
        logger.debug(f"Role assigned: {player_id} -> {role.value}")

    def get_player_role(self, player_id: str) -> PlayerRole:
        """
        Get the role for a player.

        Returns PLAYER as default if no explicit assignment exists.

        Args:
            player_id: Unique player identifier

        Returns:
            The player's assigned role, or PLAYER if unassigned
        """
        return self._role_assignments.get(player_id, PlayerRole.PLAYER)

    def remove_player_role(self, player_id: str) -> bool:
        """
        Remove a player's role assignment.

        Args:
            player_id: Unique player identifier

        Returns:
            True if the role was removed, False if player had no assignment
        """
        if player_id in self._role_assignments:
            del self._role_assignments[player_id]
            return True
        return False

    def register_character_ownership(
        self, character_id: str, player_id: str
    ) -> None:
        """
        Register that a character is owned by a specific player.

        This is used for CONDITIONAL permission checks: a PLAYER role
        can only modify characters they own.

        Args:
            character_id: The character's unique ID
            player_id: The owning player's ID
        """
        self._character_ownership[character_id] = player_id

    def unregister_character_ownership(self, character_id: str) -> None:
        """
        Remove ownership registration for a character.

        Args:
            character_id: The character's unique ID
        """
        self._character_ownership.pop(character_id, None)

    def is_owner(self, player_id: str, character_id: str) -> bool:
        """
        Check if a player owns a specific character.

        Args:
            player_id: The player to check
            character_id: The character to check ownership of

        Returns:
            True if the player owns the character
        """
        return self._character_ownership.get(character_id) == player_id

    def grant_permission(
        self,
        player_id: str,
        tool_name: str,
        duration_minutes: Optional[int] = None,
    ) -> TemporaryPermission:
        """
        Grant a temporary permission to a player for a specific tool.

        The DM can use this to temporarily allow a player access to
        tools they wouldn't normally have (e.g., update_game_state).

        Args:
            player_id: The player receiving the permission
            tool_name: The tool to grant access to
            duration_minutes: How long the grant lasts (None = session-scoped)

        Returns:
            The created TemporaryPermission
        """
        expires = None
        if duration_minutes is not None:
            expires = datetime.now() + timedelta(minutes=duration_minutes)

        perm = TemporaryPermission(
            player_id=player_id,
            tool_name=tool_name,
            expires_at=expires,
        )
        self._temp_permissions.append(perm)
        logger.debug(
            f"Temporary permission granted: {player_id} -> {tool_name} "
            f"(expires: {expires or 'session'})"
        )
        return perm

    def revoke_permission(self, player_id: str, tool_name: str) -> int:
        """
        Revoke all temporary permissions for a player on a specific tool.

        Args:
            player_id: The player to revoke from
            tool_name: The tool to revoke access to

        Returns:
            Number of permissions revoked
        """
        before = len(self._temp_permissions)
        self._temp_permissions = [
            p for p in self._temp_permissions
            if not (p.player_id == player_id and p.tool_name == tool_name)
        ]
        revoked = before - len(self._temp_permissions)
        if revoked:
            logger.debug(
                f"Revoked {revoked} temporary permission(s): "
                f"{player_id} -> {tool_name}"
            )
        return revoked

    def _has_temp_permission(self, player_id: str, tool_name: str) -> bool:
        """
        Check if a player has a valid (non-expired) temporary permission.

        Also cleans up expired permissions as a side effect.

        Args:
            player_id: The player to check
            tool_name: The tool to check

        Returns:
            True if a valid temporary permission exists
        """
        now = datetime.now()
        valid = []
        found = False

        for perm in self._temp_permissions:
            # Skip expired
            if perm.expires_at is not None and perm.expires_at < now:
                continue
            valid.append(perm)
            if perm.player_id == player_id and perm.tool_name == tool_name:
                found = True

        # Clean expired
        self._temp_permissions = valid
        return found

    def check_permission(
        self,
        player_id: Optional[str],
        tool_name: str,
        target_entity_id: Optional[str] = None,
    ) -> bool:
        """
        Check if a player is allowed to call a specific tool.

        This is the main entry point for permission checks. When player_id
        is None (single-player DM mode), returns True immediately with
        zero overhead — no dict lookups, no logic.

        Args:
            player_id: The calling player's ID (None = single-player bypass)
            tool_name: The MCP tool being called
            target_entity_id: The target entity ID (for CONDITIONAL checks)

        Returns:
            True if the call is permitted, False otherwise
        """
        # Single-player bypass — zero overhead
        if player_id is None:
            return True

        role = self.get_player_role(player_id)

        # DM always has full access
        if role == PlayerRole.DM:
            return True

        # Check temporary permissions first
        if self._has_temp_permission(player_id, tool_name):
            return True

        # Look up permission matrix
        tool_perms = PERMISSION_MATRIX.get(tool_name)
        if tool_perms is None:
            # Unknown tool: default to DM-only
            logger.warning(
                f"Tool '{tool_name}' not in permission matrix, "
                f"defaulting to DM-only"
            )
            return False

        perm_level = tool_perms.get(role, PermissionLevel.DENIED)

        if perm_level == PermissionLevel.ALLOWED:
            return True
        elif perm_level == PermissionLevel.DENIED:
            return False
        elif perm_level == PermissionLevel.CONDITIONAL:
            # Ownership check required
            if target_entity_id is None:
                # No entity specified — deny to be safe
                logger.debug(
                    f"CONDITIONAL permission for {player_id}/{tool_name} "
                    f"denied: no target_entity_id provided"
                )
                return False
            return self.is_owner(player_id, target_entity_id)

        return False

    def cleanup_expired(self) -> int:
        """
        Remove all expired temporary permissions.

        Returns:
            Number of expired permissions removed
        """
        now = datetime.now()
        before = len(self._temp_permissions)
        self._temp_permissions = [
            p for p in self._temp_permissions
            if p.expires_at is None or p.expires_at >= now
        ]
        removed = before - len(self._temp_permissions)
        if removed:
            logger.debug(f"Cleaned up {removed} expired temporary permission(s)")
        return removed

    @property
    def role_count(self) -> int:
        """Get the number of role assignments."""
        return len(self._role_assignments)

    @property
    def temp_permission_count(self) -> int:
        """Get the number of active temporary permissions."""
        return len(self._temp_permissions)

    def get_all_role_assignments(self) -> dict[str, PlayerRole]:
        """
        Get a copy of all current role assignments.

        Returns:
            Dict mapping player_id -> PlayerRole
        """
        return dict(self._role_assignments)


__all__ = [
    "PlayerRole",
    "PermissionLevel",
    "TemporaryPermission",
    "PlayerRoleAssignment",
    "PermissionResolver",
    "PERMISSION_MATRIX",
]
