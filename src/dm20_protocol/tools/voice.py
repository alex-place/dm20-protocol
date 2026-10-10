"""
Text-to-speech helpers used by Party Mode narration.

Split out of ``dm20_protocol.main``; every name defined here is re-exported
from ``dm20_protocol.main``. Shared server state (``storage``, managers, ...)
is read through ``_m`` (the main module) at call time so that tests and
callers that monkeypatch ``dm20_protocol.main.storage`` keep working.
"""

import logging

from .. import main as _m

logger = logging.getLogger("dm20-protocol")


# --- TTS helper for Party Mode narration (macOS local audio) ----------

# Maximum characters sent to TTS to avoid excessively long audio
_TTS_MAX_CHARS = 3000

# Singleton router (lazy-initialized)
_tts_router_instance = None


def _strip_markdown_for_tts(text: str) -> str:
    """Remove Markdown formatting so TTS does not read symbols aloud."""
    import re

    # Headers: ## Title → Title
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Bold+italic: ***text*** / ___text___
    text = re.sub(r"\*{3}(.+?)\*{3}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"_{3}(.+?)_{3}", r"\1", text, flags=re.DOTALL)
    # Bold: **text** / __text__
    text = re.sub(r"\*{2}(.+?)\*{2}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"_{2}(.+?)_{2}", r"\1", text, flags=re.DOTALL)
    # Italic: *text* / _text_
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    # Inline code: `code`
    text = re.sub(r"`(.+?)`", r"\1", text)
    # Links: [text](url) → text
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    # Horizontal rules
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    # Blockquotes
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    # Unordered list markers
    text = re.sub(r"^[-*+]\s+", "", text, flags=re.MULTILINE)
    # Ordered list markers: 1. 2. etc.
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)
    # Collapse excess blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _party_tts_speak(narrative: str, server) -> None:
    """Synthesize narrative text via TTS and play on the DM's Mac speakers.

    Only runs when interaction_mode is 'narrated' or 'immersive'.
    Plays audio non-blocking via macOS ``afplay`` so the MCP response
    is not delayed.  All errors are silently logged — TTS failure must
    never break the Party Mode flow.
    """
    import asyncio
    import logging
    import platform
    import subprocess
    import tempfile

    logger = logging.getLogger("dm20-protocol.party.tts")

    # 1. Check interaction mode
    if _m.storage.interaction_mode not in ("narrated", "immersive"):
        logger.info(
            "TTS skipped — interaction_mode is '%s'. Use /dm:profile to enable voice.",
            _m.storage.interaction_mode,
        )
        return

    # 2. macOS-only guard
    if platform.system() != "Darwin":
        logger.debug("TTS playback skipped: not macOS (platform=%s)", platform.system())
        return

    # 3. Import voice subsystem (optional dependency)
    try:
        from ..voice import TTSRouter, VoiceConfig, VoiceRegistry
    except ImportError:
        logger.debug("TTS skipped: voice dependencies not installed")
        return

    # 4. Truncate long narratives
    text = _strip_markdown_for_tts(narrative)
    if not text:
        return
    if len(text) > _TTS_MAX_CHARS:
        # Cut at the last sentence boundary within the limit
        truncated = text[:_TTS_MAX_CHARS]
        last_period = truncated.rfind(".")
        if last_period > _TTS_MAX_CHARS // 2:
            text = truncated[: last_period + 1]
        else:
            text = truncated.rstrip() + "…"

    # 5. Use pre-initialized router from server if available, else lazy-init singleton
    if getattr(server, "tts_router", None) is not None:
        router = server.tts_router
    else:
        global _tts_router_instance
        if _tts_router_instance is None:
            _tts_router_instance = TTSRouter()
        router = _tts_router_instance

    # 6. Run async synthesis on the server event loop
    async def _synth_and_play():
        try:
            # Ensure the router is initialized (lazy singleton path)
            if not router._initialized:
                await router.initialize()
                logger.info("Lazy TTSRouter initialized: %s", router.get_status())
            _vr = getattr(server, "voice_registry", None)
            _vc = _vr.get_dm_voice() if _vr is not None else VoiceConfig(language="it")
            result = await router.synthesize(text, context="narration", voice_config=_vc)
            # Write WAV to a temp file and play with afplay (non-blocking)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(result.audio_data)
                tmp_path = tmp.name
            # afplay in background — don't wait for playback to finish
            subprocess.Popen(
                ["afplay", tmp_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(
                "TTS playing narrative (%d chars, engine=%s, %.0fms)",
                len(text), result.engine_name, result.duration_ms,
            )

            # Broadcast audio to connected players via WebSocket
            try:
                import base64 as _b64
                audio_b64 = _b64.b64encode(result.audio_data).decode("ascii")
                audio_msg = {
                    "type": "audio",
                    "format": result.format.value,
                    "data": audio_b64,
                }
                await server.connection_manager.broadcast(audio_msg)
            except Exception as ws_exc:
                logger.warning("Audio WebSocket broadcast failed: %s", ws_exc)

        except Exception as exc:
            logger.warning("TTS synthesis/playback failed: %s", exc)

    # Schedule on the server's event loop (same pattern as party_kick_player)
    if server._loop and not server._loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(_synth_and_play(), server._loop)
        except Exception as exc:
            logger.warning("Could not schedule TTS on server loop: %s", exc)
