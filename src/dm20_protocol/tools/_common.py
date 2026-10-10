"""
Small helpers shared by several tool modules (no tools are registered here).

Split out of ``dm20_protocol.main``; every name defined here is re-exported
from ``dm20_protocol.main``. Shared server state (``storage``, managers, ...)
is read through ``_m`` (the main module) at call time so that tests and
callers that monkeypatch ``dm20_protocol.main.storage`` keep working.
"""

import json
import logging

from ..models import Combatant

from .. import main as _m

logger = logging.getLogger("dm20-protocol")


def _parse_json_list(value: str) -> list[str]:
    """Parse a JSON list string, or fall back to comma-separated values."""
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return [str(parsed)]
    except json.JSONDecodeError:
        return [item.strip() for item in value.split(",") if item.strip()]


def _prefetch_state_update() -> None:
    """Feed current game state to PrefetchEngine if active."""
    from ..party.server import get_server_instance
    server = get_server_instance()
    if server is None:
        return
    if getattr(server, "prefetch_engine", None) is None:
        return
    try:
        _gs = _m.storage.get_game_state()
        if _gs:
            server.prefetch_engine.on_state_change(_gs.model_dump())
    except Exception as exc:
        logger.debug("Prefetch state update failed: %s", exc)


def _hp_text(c: Combatant) -> str:
    temp = f" (+{c.temp_hp} temp)" if c.temp_hp else ""
    status = " DOWN" if c.is_down else ""
    return f"{c.hp_current}/{c.hp_max} HP{temp}{status}"
