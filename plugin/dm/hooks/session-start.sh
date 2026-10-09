#!/usr/bin/env bash
# SessionStart (matcher: compact): if a checkpoint recorded the campaign, tell Claude
# to resume it. The file is consumed so a later, unrelated compaction doesn't resume.

project="${CLAUDE_PROJECT_DIR:-$(pwd)}"
marker="$project/.claude/last-campaign.txt"

if [ -f "$marker" ]; then
    campaign="$(cat "$marker")"
    rm -f "$marker"
    echo "🎲 Session checkpoint detected for campaign: $campaign. Auto-resuming — invoke /dm:start $campaign now."
fi
exit 0
