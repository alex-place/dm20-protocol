"""
Party Mode tools: private messaging, server lifecycle, action queue and tokens.

Split out of ``dm20_protocol.main``; every name defined here is re-exported
from ``dm20_protocol.main``. Shared server state (``storage``, managers, ...)
is read through ``_m`` (the main module) at call time so that tests and
callers that monkeypatch ``dm20_protocol.main.storage`` keep working.
"""

import logging
from typing import Annotated
from pydantic import Field

from .. import main as _m
from .voice import _party_tts_speak

logger = logging.getLogger("dm20-protocol")
mcp = _m.mcp


# --------------------------------------------------------------------------
# Output Filtering and Multi-User Session Coordination (Issue #147)
# --------------------------------------------------------------------------

@mcp.tool
def send_private_message(
    player_id: Annotated[str, Field(description="Recipient player ID")],
    content: Annotated[str, Field(description="Message content to send privately")],
    sender_id: Annotated[str, Field(description="Sender player ID (typically the DM)")] = "DM",
) -> str:
    """DM can send private messages to individual players via this tool.

    Messages are stored in the session coordinator and can be retrieved
    by the recipient player. Only visible to the specified recipient.
    """
    try:
        message = _m.session_coordinator.send_private_message(
            sender_id=sender_id,
            recipient_id=player_id,
            content=content,
        )
        return f"Private message sent to '{player_id}': {content[:50]}{'...' if len(content) > 50 else ''}"
    except ValueError as e:
        return f"Error: {e}"


# --------------------------------------------------------------------------
# Party Mode Server Management Tools
# --------------------------------------------------------------------------


@mcp.tool
def start_party_mode(
    port: Annotated[int, Field(description="Server port number", ge=1024, le=65535)] = 8080,
) -> str:
    """Start the Party Mode web server for multi-player sessions.

    Launches a background HTTP server that allows multiple players to connect
    via their phones or browsers. Automatically generates authentication tokens
    and QR codes for each player character in the current campaign.

    Returns connection URLs and QR code file paths for each player.
    """
    from ..party.server import start_party_server, get_server_instance
    from ..party.auth import QRCodeGenerator
    from ..party.firewall import ensure_firewall_allows_python, format_firewall_status
    from ..claudmaster.pc_tracking import PCRegistry, MultiPlayerConfig

    # Check macOS firewall authorization for incoming connections
    firewall_status = ensure_firewall_allows_python()
    firewall_msg = format_firewall_status(firewall_status)

    # Check if already running
    existing = get_server_instance()
    if existing is not None:
        return (
            f"Party Mode is already running at http://{existing.host_ip}:{existing.port}\n"
            "Use `stop_party_mode` to shut it down first, or `get_party_status` to see current state."
        )

    # Check campaign loaded
    campaign = _m.storage.get_current_campaign()
    if not campaign:
        return "Error: No campaign loaded. Use `/dm:start` to load a campaign first."

    # Resolve campaign directory
    campaign_dir = _m.storage._split_backend._get_campaign_dir(campaign.name)

    # Get characters (need full Character objects, not just names)
    characters = _m.storage.list_characters_detailed()
    if not characters:
        return "Error: No player characters in the current campaign. Create characters before starting Party Mode."

    # Build PCRegistry
    config = MultiPlayerConfig()
    registry = PCRegistry(config)
    for char in characters:
        registry.register_pc(char.id, char.player_name or char.name)

    # Start the server in a background thread
    try:
        server = start_party_server(
            pc_registry=registry,
            permission_resolver=_m.permission_resolver,
            storage=_m.storage,
            campaign_dir=campaign_dir,
            port=port,
        )
    except Exception as e:
        return f"Error starting Party Mode server: {e}"

    # --- TTS Router: background async init ---
    import asyncio as _asyncio

    async def _init_tts(srv):
        try:
            from ..voice import TTSRouter, VoiceRegistry
            srv.tts_router = TTSRouter()
            await srv.tts_router.initialize()
            logger.info("TTSRouter ready: %s", srv.tts_router.get_status())
            srv.voice_registry = VoiceRegistry(srv.campaign_dir)
            srv.setup_audio(srv.tts_router, srv.voice_registry)
        except Exception as exc:
            logger.warning("TTSRouter init failed, TTS disabled: %s", exc)
            srv.tts_router = None

    if server._loop and not server._loop.is_closed():
        _asyncio.run_coroutine_threadsafe(_init_tts(server), server._loop)

    # --- PrefetchEngine init ---
    try:
        from ..prefetch import PrefetchEngine
        from ..claudmaster.llm_client import AnthropicLLMClient
        from ..claudmaster.model_ids import HAIKU
        _haiku = AnthropicLLMClient(model=HAIKU, effort="low", default_max_tokens=2048)
        server.prefetch_engine = PrefetchEngine(
            main_model=_haiku,
            refinement_model=_haiku,
            intensity="conservative",
        )
        logger.info("PrefetchEngine ready (intensity=conservative)")
    except Exception as exc:
        logger.warning("PrefetchEngine init failed, prefetch disabled: %s", exc)
        server.prefetch_engine = None

    # Generate tokens and QR codes
    host_ip = server.host_ip
    lines = []
    lines.append("# Party Mode Active\n")
    lines.append(f"**Server:** http://{host_ip}:{port}")
    lines.append(f"**Players:** {len(characters)} PCs + 1 Observer\n")
    lines.append("## Player Connections\n")

    # Collect QR terminal art for printing to stderr after response
    qr_terminal_lines: list[str] = []

    for char in characters:
        token = server.token_manager.generate_token(char.id)
        url = f"http://{host_ip}:{port}/play?token={token}"

        # Render QR as terminal ASCII art (for DM's terminal)
        qr_terminal_lines.append(
            QRCodeGenerator.render_qr_terminal(url, char.name)
        )

        try:
            qr_path = QRCodeGenerator.generate_player_qr(
                char.id, token, host_ip, port, campaign_dir,
                player_name=char.player_name or "",
                character_name=char.name,
            )
            lines.append(f"### {char.name}")
            lines.append(f"- **URL:** {url}")
            lines.append(f"- **QR Code:** {qr_path}\n")
        except Exception:
            lines.append(f"### {char.name}")
            lines.append(f"- **URL:** {url}")
            lines.append("- **QR Code:** (generation failed, use URL instead)\n")

    # Observer token
    observer_token = server.token_manager.generate_token("OBSERVER")
    observer_url = f"http://{host_ip}:{port}/play?token={observer_token}"

    # Render observer QR as terminal ASCII art
    qr_terminal_lines.append(
        QRCodeGenerator.render_qr_terminal(observer_url, "OBSERVER (read-only)")
    )

    try:
        observer_qr = QRCodeGenerator.generate_player_qr(
            "OBSERVER", observer_token, host_ip, port, campaign_dir
        )
        lines.append("### OBSERVER (read-only)")
        lines.append(f"- **URL:** {observer_url}")
        lines.append(f"- **QR Code:** {observer_qr}\n")
    except Exception:
        lines.append("### OBSERVER (read-only)")
        lines.append(f"- **URL:** {observer_url}")
        lines.append("- **QR Code:** (generation failed, use URL instead)\n")

    # Print QR codes as ASCII art to terminal (stderr, visible to DM)
    # This allows the DM to see scannable QR codes directly in the terminal
    # without needing to open the saved PNG files.
    import sys
    try:
        print("\n\n=== Party Mode QR Codes (scan with phone) ===", file=sys.stderr)
        for qr_art in qr_terminal_lines:
            print(qr_art, file=sys.stderr)
        print("=== End QR Codes ===\n", file=sys.stderr)
    except Exception:
        pass  # Graceful fallback: terminal rendering failure is non-fatal

    # Firewall status (only shown if relevant)
    if firewall_msg:
        lines.append(f"\n{firewall_msg}\n")

    lines.append("---")
    lines.append("Players can scan QR codes or open URLs on their phones/tablets to join.")
    lines.append(f"DM dashboard: http://{host_ip}:{port}/ (verify server is reachable)")
    lines.append("")
    lines.append("**Troubleshooting — players can't connect?**")
    lines.append("- Ensure players are on the **same Wi-Fi network** as this machine")
    lines.append("- Check **macOS Firewall**: System Settings → Network → Firewall — allow incoming connections for Python")
    lines.append("- If you just added the firewall rule, **restart Party Mode** (`stop_party_mode` then `start_party_mode`) — macOS applies rules only when the process opens the socket")
    lines.append(f"- Test from this machine: open http://localhost:{port}/ in a browser")
    lines.append("")
    lines.append("Use `get_party_status` to monitor connections.")
    lines.append("Use `stop_party_mode` to end Party Mode.")

    return "\n".join(lines)


@mcp.tool
def stop_party_mode() -> str:
    """Stop the Party Mode web server and disconnect all players.

    Gracefully shuts down the server and closes all WebSocket connections.
    """
    from ..party.server import stop_party_server, get_server_instance

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running."

    try:
        stop_party_server()
        return "Party Mode server stopped. All players have been disconnected."
    except Exception as e:
        return f"Error stopping Party Mode: {e}"


@mcp.tool
def get_party_status() -> str:
    """Get the current status of the Party Mode server.

    Shows server info, connected players, and action queue stats.
    """
    from ..party.server import get_server_instance
    from datetime import datetime

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running. Use `start_party_mode` to start it."

    uptime = (datetime.now() - server.start_time).total_seconds()
    hours, remainder = divmod(int(uptime), 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s" if hours else f"{minutes}m {seconds}s"

    lines = []
    lines.append("# Party Mode Status\n")
    lines.append(f"**Server:** http://{server.host_ip}:{server.port}")
    lines.append(f"**Uptime:** {uptime_str}")

    # Connected players
    connected = server.connection_manager.get_connected_players()
    lines.append(f"**Connected Players:** {len(connected)}\n")

    if connected:
        for player_id in connected:
            lines.append(f"- {player_id}")
    else:
        lines.append("_(No players currently connected)_")

    # Action queue stats
    lines.append(f"\n**Actions in Queue:** {server.action_queue.get_pending_count()}")

    # Stale connections
    stale = server.connection_manager.get_stale_players()
    if stale:
        lines.append(f"\n**Stale Connections:** {', '.join(stale)}")

    return "\n".join(lines)


@mcp.tool
def party_pop_action() -> str:
    """Pop the next pending player action from the Party Mode queue.

    Returns the action details (player_id, action_id, text, timestamp) and
    remaining queue count, or reports that the queue is empty.
    """
    import json as _json
    from ..party.server import get_server_instance

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running. Use `start_party_mode` to start it."

    action = server.action_queue.pop()
    if action is None:
        return _json.dumps({"empty": True, "pending": 0})

    remaining = server.action_queue.get_pending_count()
    return _json.dumps({
        "empty": False,
        "action": action,
        "remaining": remaining,
    })


@mcp.tool
def party_resolve_action(
    action_id: Annotated[str, Field(description="The action_id returned by party_pop_action")],
    narrative: Annotated[str, Field(description="The DM's narrative response to the player's action")],
    private_messages: Annotated[str | None, Field(description="JSON object of player-specific private messages, e.g. {\"player\": \"secret\"}")] = None,
    dm_notes: Annotated[str | None, Field(description="DM-only notes (not sent to players)")] = None,
) -> str:
    """Resolve a player action and broadcast the response to connected players.

    After processing a player action (rolling dice, narrating outcome, updating state),
    call this tool to push the response to the WebSocket broadcast queue.
    """
    import json as _json
    from ..party.server import get_server_instance

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running."

    private = {}
    if private_messages:
        try:
            private = _json.loads(private_messages)
        except _json.JSONDecodeError:
            pass

    response_data = {
        "action_id": action_id,
        "narrative": narrative,
        "private": private,
        "dm_only": dm_notes or "",
    }

    response_id = server.response_queue.push(response_data)
    server.action_queue.resolve(action_id, response_data)

    # --- TTS narration (macOS local audio) ---
    _party_tts_speak(narrative, server)

    # --- Prefetch: feed updated game state ---
    if getattr(server, "prefetch_engine", None):
        try:
            _gs = _m.storage.get_game_state()
            if _gs:
                server.prefetch_engine.on_state_change(_gs.model_dump())
        except Exception as exc:
            logger.debug("Prefetch state update failed: %s", exc)

    return f"Response broadcast to connected players (response_id: {response_id})."


@mcp.tool
def party_thinking(
    message: Annotated[str | None, Field(description="Short message shown to players, e.g. 'The Dungeon Master consults the ancient scrolls…'")] = None,
) -> str:
    """Signal to players that the DM is preparing the next narrative.

    Call this immediately after party_pop_action to give players instant
    visual feedback (animated dots + message) while you think and generate
    the response. The indicator disappears automatically when you call
    party_resolve_action.
    """
    import asyncio as _asyncio
    from ..party.server import get_server_instance

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running."

    thinking_msg = {
        "type": "thinking",
        "message": message or "The Dungeon Master is consulting the dice\u2026",
    }

    if server._loop and not server._loop.is_closed():
        try:
            _asyncio.run_coroutine_threadsafe(
                server.connection_manager.broadcast(thinking_msg),
                server._loop,
            )
        except Exception as exc:
            logger.debug("party_thinking broadcast failed: %s", exc)

    return "Thinking indicator shown to players."


@mcp.tool
def party_get_prefetch(
    turn_id: Annotated[str, Field(description="Turn identifier — use the same format as the observer: 'round_{N}_{character_name}', e.g. 'round_3_Aria'")],
    outcome: Annotated[str, Field(description="Actual combat outcome: 'hit', 'miss', or 'critical'")],
    roll: Annotated[int | None, Field(description="The actual attack roll value")] = None,
    damage: Annotated[int | None, Field(description="Damage dealt (for hit/critical)")] = None,
    target_hp: Annotated[int | None, Field(description="Target's remaining HP after damage")] = None,
) -> str:
    """Retrieve a pre-generated narrative variant for a combat turn.

    If the prefetch engine has a cached variant for this turn, returns a
    refined narrative instantly (no main-model call needed). On cache miss,
    falls back to full generation with the main model.

    Call this right after party_thinking, before writing your own narrative.
    If 'cached' is true in the response, use 'narrative' as your starting
    point and adjust only the details that differ from actual game state.
    """
    import asyncio as _asyncio
    import json as _json
    from ..party.server import get_server_instance

    server = get_server_instance()
    if server is None:
        return _json.dumps({"cached": False, "narrative": "", "reason": "Party Mode not running"})

    prefetch_engine = getattr(server, "prefetch_engine", None)
    if prefetch_engine is None:
        return _json.dumps({"cached": False, "narrative": "", "reason": "Prefetch engine not initialized"})

    actual_result = {
        "outcome": outcome,
        "roll": roll,
        "damage": damage,
        "target_hp": target_hp,
    }

    if not (server._loop and not server._loop.is_closed()):
        return _json.dumps({"cached": False, "narrative": "", "reason": "Server loop unavailable"})

    hits_before = prefetch_engine.token_usage.cache_hits
    try:
        future = _asyncio.run_coroutine_threadsafe(
            prefetch_engine.resolve_with_actual(turn_id, actual_result),
            server._loop,
        )
        narrative = future.result(timeout=8.0)
        was_cached = prefetch_engine.token_usage.cache_hits > hits_before
        return _json.dumps({"cached": was_cached, "narrative": narrative})
    except Exception as exc:
        logger.debug("party_get_prefetch failed: %s", exc)
        return _json.dumps({"cached": False, "narrative": "", "reason": str(exc)})


@mcp.tool
def party_kick_player(
    player_name: Annotated[str, Field(description="Player name or character ID to kick")],
) -> str:
    """Kick a player from the Party Mode session.

    Disconnects their WebSocket, revokes their token, and deactivates
    them in the PC registry. They will need a new token to rejoin.
    """
    import json as _json
    import asyncio
    from ..party.server import get_server_instance

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running."

    player_id = player_name.strip()

    # Check if player exists
    tokens = server.token_manager.get_all_tokens()
    if player_id not in tokens:
        available = list(tokens.keys())
        return f"Player '{player_id}' not found. Active players: {', '.join(available)}"

    results = []

    # Close WebSocket connections
    closed_count = 0
    if server._loop and not server._loop.is_closed():
        async def _kick():
            connections = server.connection_manager._connections.get(player_id, set()).copy()
            for ws in connections:
                try:
                    await ws.close(code=1008, reason="Kicked by host")
                except Exception:
                    pass
            server.connection_manager.disconnect(player_id, None)
            return len(connections)

        try:
            future = asyncio.run_coroutine_threadsafe(_kick(), server._loop)
            closed_count = future.result(timeout=5.0)
        except Exception:
            pass

    results.append(f"Connections closed: {closed_count}")

    # Revoke token
    revoked = server.token_manager.revoke_token(player_id)
    results.append(f"Token revoked: {revoked}")

    # Deactivate in registry
    try:
        server.pc_registry.leave_session(player_id)
        results.append("Registry: deactivated")
    except Exception:
        results.append("Registry: was not active")

    # Broadcast notification
    if server._loop and not server._loop.is_closed():
        from datetime import datetime as _dt
        async def _broadcast():
            msg = {
                "type": "system",
                "content": f"{player_id} was removed from the session by the DM.",
                "timestamp": _dt.now().isoformat(),
            }
            return await server.connection_manager.broadcast(msg)
        try:
            asyncio.run_coroutine_threadsafe(_broadcast(), server._loop).result(timeout=5.0)
        except Exception:
            pass

    return f"Player '{player_id}' kicked.\n" + "\n".join(results)


@mcp.tool
def party_refresh_token(
    player_name: Annotated[str, Field(description="Player name or character ID to refresh token for")],
) -> str:
    """Generate a new token and QR code for a player, invalidating their old token.

    Use when a player needs a new connection link (lost QR code, security concern,
    or after being kicked and readmitted).
    """
    from ..party.server import get_server_instance
    from ..party.auth import QRCodeGenerator

    server = get_server_instance()
    if server is None:
        return "Party Mode is not running. Use `start_party_mode` to start it."

    player_id = player_name.strip()

    # Generate new token (invalidates old one)
    new_token = server.token_manager.refresh_token(player_id)
    url = f"http://{server.host_ip}:{server.port}/play?token={new_token}"

    lines = [f"# Token Refreshed: {player_id}\n"]
    lines.append(f"**New URL:** {url}")

    # Generate QR code
    try:
        qr_path = QRCodeGenerator.generate_player_qr(
            player_id, new_token, server.host_ip, server.port, server.campaign_dir
        )
        lines.append(f"**QR Code:** {qr_path}")
    except Exception:
        lines.append("**QR Code:** (generation failed — use URL instead)")

    lines.append("\nThe old token is now invalid. The player must use the new URL or scan the new QR code.")

    # Print refreshed QR code as ASCII art to terminal
    import sys
    try:
        qr_art = QRCodeGenerator.render_qr_terminal(url, player_id)
        print(qr_art, file=sys.stderr)
    except Exception:
        pass  # Graceful fallback: terminal rendering failure is non-fatal

    # Reactivate in registry if needed
    try:
        from ..permissions import PlayerRole
        role = PlayerRole.OBSERVER if player_id == "OBSERVER" else PlayerRole.PLAYER
        server.pc_registry.join_session(player_id, player_id, role=role)
    except Exception:
        pass

    return "\n".join(lines)
