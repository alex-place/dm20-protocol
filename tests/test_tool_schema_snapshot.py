"""
Snapshot test for the registered MCP tool surface.

Guards refactors of ``dm20_protocol.main`` (e.g. splitting tools into
``dm20_protocol.tools.*`` modules): every tool must keep its exact name,
description (docstring), input schema, output schema and annotations.

Regenerate the fixture deliberately with:

    DM20_UPDATE_TOOL_SNAPSHOT=1 uv run pytest tests/test_tool_schema_snapshot.py
"""

import asyncio
import json
import os
from pathlib import Path

from fastmcp import Client

from dm20_protocol.main import mcp

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "tool_schema_snapshot.json"


def _collect_tool_surface() -> dict:
    async def _list():
        async with Client(mcp) as client:
            return await client.list_tools()

    tools = asyncio.run(_list())
    surface = {}
    for tool in tools:
        surface[tool.name] = {
            "description": tool.description,
            "inputSchema": tool.inputSchema,
            "outputSchema": getattr(tool, "outputSchema", None),
            "annotations": tool.annotations.model_dump(mode="json") if tool.annotations else None,
        }
    return dict(sorted(surface.items()))


def test_tool_surface_matches_snapshot():
    surface = _collect_tool_surface()

    if os.getenv("DM20_UPDATE_TOOL_SNAPSHOT"):
        SNAPSHOT_PATH.write_text(
            json.dumps(surface, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    assert SNAPSHOT_PATH.exists(), f"Missing snapshot fixture {SNAPSHOT_PATH}"
    expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    # Round-trip through JSON so tuples/lists etc. compare consistently.
    actual = json.loads(json.dumps(surface, ensure_ascii=False, sort_keys=True))

    assert sorted(actual) == sorted(expected), (
        f"Tool set changed. Missing: {sorted(set(expected) - set(actual))}; "
        f"unexpected: {sorted(set(actual) - set(expected))}"
    )
    for name in expected:
        assert actual[name] == expected[name], f"Tool surface changed for '{name}'"
