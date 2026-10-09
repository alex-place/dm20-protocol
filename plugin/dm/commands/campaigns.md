---
description: Interactive campaign management - list, load, create, and delete campaigns.
argument-hint: [list|load|create|delete]
allowed-tools: AskUserQuestion, mcp__dm20-protocol__list_campaigns, mcp__dm20-protocol__load_campaign, mcp__dm20-protocol__create_campaign, mcp__dm20-protocol__delete_campaign, mcp__dm20-protocol__get_campaign_info
---

# Campaign Management

Interactive campaign management with menus.

## Instructions

### Determine Action

**If `$ARGUMENTS` matches one of: list, load, create, delete** → jump directly to that section below.

**If `$ARGUMENTS` is empty:**

1. Call `list_campaigns()` and `get_campaign_info()` (if a campaign is active) in parallel
2. Show the current active campaign (if any) and the full list
3. Use `AskUserQuestion` to present options:

```
Question: "What would you like to do?"
Header: "Action"
Options:
  - "List campaigns" → Show all campaigns with details
  - "Load a campaign" → Switch to a different campaign
  - "Create new campaign" → Start a fresh campaign
  - "Delete a campaign" → Permanently remove a campaign
```

### Action: List

1. Call `list_campaigns()`
2. For each campaign, show its name with a marker for the currently active one
3. Show total count

### Action: Load

1. Call `list_campaigns()`
2. If only one campaign exists, confirm loading it
3. If multiple campaigns exist, use `AskUserQuestion`:
   - Question: "Which campaign would you like to load?"
   - Header: "Campaign"
   - Options: list each campaign name (mark current with "(current)")
4. Call `load_campaign(name=chosen_campaign)`
5. Call `get_campaign_info()` and display a summary

### Action: Create

1. Ask the user conversationally for:
   - Campaign name (required)
   - Brief description (required)
   - DM name (optional)
   - Setting description (optional)
2. Call `create_campaign()` with the provided details
3. Confirm creation and show the new campaign info

### Action: Delete

1. Call `list_campaigns()`
2. If no campaigns exist, inform the user and stop
3. Use `AskUserQuestion`:
   - Question: "Which campaign do you want to delete? This CANNOT be undone."
   - Header: "Delete"
   - Options: list each campaign name
4. After selection, use a SECOND `AskUserQuestion` to confirm:
   - Question: "Are you sure you want to permanently delete '[campaign_name]'?"
   - Header: "Confirm"
   - Options: "Yes, delete it" / "No, cancel"
5. If confirmed, call `delete_campaign(name=chosen_campaign)`
6. Show confirmation message

## Error Handling

- **No campaigns for load/delete:** "No campaigns found. Use 'create' to make one!"
- **Campaign not found:** Show available campaigns
- **Delete active campaign:** Warn the user that the active campaign will be unloaded
