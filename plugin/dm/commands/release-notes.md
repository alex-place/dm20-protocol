---
description: Show what's new in dm20-protocol — latest changes and release notes.
allowed-tools: mcp__dm20-protocol__check_for_updates, mcp__dm20-protocol__get_release_notes
---

# Release Notes

Show the latest changes and version information for dm20-protocol.

## Usage
```
/dm:release-notes
```

## Instructions

### Step 1 — Version Check

Call `check_for_updates` to get the current and latest version info.

### Step 2 — Fetch Release Notes

Call `get_release_notes` to get the changelog content.

### Step 3 — Display

Present the information clearly:

```
╔══════════════════════════════════════════════════╗
║  dm20-protocol — Release Notes                   ║
╠══════════════════════════════════════════════════╣
║  Installed: v{current}                           ║
║  Latest:    v{latest}                            ║
╚══════════════════════════════════════════════════╝
```

**If an update is available**, add:
```
⚡ Update available! Run:
bash <(curl -fsSL https://raw.githubusercontent.com/Polloinfilzato/dm20-protocol/main/install.sh) --upgrade
```

Then display the changelog content from `get_release_notes`, formatted as readable markdown. Show the **[Unreleased]** section (upcoming changes) and the **most recent released version** section.

### Error Handling

- **Network error on version check:** Skip version comparison, just show release notes.
- **Network error on release notes:** Show version info and suggest checking GitHub directly.
