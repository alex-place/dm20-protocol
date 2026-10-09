#!/usr/bin/env bash
# PreCompact: write a deterministic session checkpoint (no LLM) before Claude Code
# compacts the conversation, so the SessionStart hook can resume the campaign.
# Never blocks compaction: always exits 0.

project="${CLAUDE_PROJECT_DIR:-$(pwd)}"

if command -v dm20-checkpoint >/dev/null 2>&1; then
    dm20-checkpoint --campaign-dir "$project" || true
elif command -v uv >/dev/null 2>&1 && [ -f "$project/pyproject.toml" ] \
        && grep -q 'name = "dm20-protocol"' "$project/pyproject.toml" 2>/dev/null; then
    # Developer install: run from the repository's own environment.
    uv run --project "$project" python -m dm20_protocol.checkpoint --campaign-dir "$project" || true
else
    echo "dm20: dm20-checkpoint not found, so no automatic checkpoint was written. Run /dm:save to keep a session note."
fi
exit 0
