"""
D&D MCP Server - A comprehensive campaign management tool for D&D built with FastMCP 2.8.0+.
"""

from .models import *
from .storage import DnDStorage

try:
    from importlib.metadata import version as _get_version
    __version__ = _get_version("dm20-protocol")
except Exception:
    __version__ = "0.4.0"  # Fallback if metadata unavailable
__all__ = ["mcp", "DnDStorage"]


def __getattr__(name: str):
    # Importing main starts the whole server (rulebook loads, logging), so defer
    # it until `mcp` is actually requested. Lightweight entry points such as
    # dm20_protocol.checkpoint can then import the package cheaply.
    if name == "mcp":
        from .main import mcp
        return mcp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
