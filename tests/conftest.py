"""
Pytest configuration and fixtures for dm20-protocol tests.
"""

import sys
from pathlib import Path
import pytest

# Add src directory to Python path to allow importing dm20_protocol
src_path = Path(__file__).parent.parent / "src"
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))


# Configure anyio to only use asyncio (not trio)
@pytest.fixture
def anyio_backend():
    return "asyncio"


# Keep every test away from the repo's real `.claude/agents/*.md` files.
# `update_agent_files()` (called by profile switches in configure_claudmaster)
# resolves DM20_AGENTS_DIR first, so pointing it at a per-test copy prevents
# the suite from rewriting checked-in agent frontmatter.
@pytest.fixture(autouse=True)
def _isolated_agents_dir(tmp_path_factory, monkeypatch):
    import shutil

    agents_dir = tmp_path_factory.mktemp("claude_agents")
    repo_agents = Path(__file__).parent.parent / ".claude" / "agents"
    if repo_agents.is_dir():
        for md in repo_agents.glob("*.md"):
            shutil.copy2(md, agents_dir / md.name)
    monkeypatch.setenv("DM20_AGENTS_DIR", str(agents_dir))
    yield agents_dir
