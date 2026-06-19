#!/usr/bin/env bash
# slack-backfill-channel-ids.sh — ONE-TIME, READ-ONLY backfill of channelId/userId
# into existing slack_threads.json items that lack routable Slack identifiers.
#
# PURPOSE: existing queue items store only human-readable "DM with <name>" as the
# channel field, which is not routable by post_message. This script resolves each
# item's actual Slack channelId (D.../C.../G...) and userId (U.../W...) by calling
# READ-ONLY Slack MCP tools (list_dms, lookup_user) via a one-shot claude --print
# subprocess. It NEVER sends, posts, or calls any write tool.
#
# SAFETY BOUNDARY: the subprocess's --allowedTools is restricted to ONLY read tools
# (get_unreads, list_dms, lookup_user, get_thread, get_messages, search,
# batch_get_threads, batch_get_messages). post_message is NEVER granted. The
# subprocess literally cannot send even if the prompt were malicious.
#
# USAGE: the human operator runs this manually after verifying the output.
#   bash scripts/slack-backfill-channel-ids.sh [--dry-run]
#
# OPTIONS:
#   --dry-run   Print the resolution plan without writing back to the file.
#
# ATOMICITY: writes via .tmp-then-rename (os.replace) so slack_threads.json is
# never left in a partial/corrupt state. Preserves ALL existing fields on every
# item; only ADDS channelId/userId to items that were successfully resolved.
# Items that cannot be unambiguously resolved are left with channelId absent/empty
# (the approve_item handler will surface a clear error for those).
#
# This script uses the same --mcp-config pattern as _drive_agent in slack.py:
# it reads the existing slack-mcp entry from the user's claude config, writes a
# minimal scoped config to a tempfile, and passes --mcp-config + --strict-mcp-config
# so only slack-mcp (~6 tools) loads.

set -euo pipefail

PROJ="$HOME/workspace/projects/claude-web"
SLACK_FILE="$PROJ/.claude/slack_threads.json"

# Read-only Slack MCP tools — same as _SLACK_READ_TOOLS in server/routes/slack.py.
# post_message is NEVER included. This is the safety boundary.
ALLOWED_TOOLS="mcp__slack-mcp__get_unreads,mcp__slack-mcp__list_dms,mcp__slack-mcp__get_thread,mcp__slack-mcp__get_messages,mcp__slack-mcp__search,mcp__slack-mcp__lookup_user,mcp__slack-mcp__batch_get_threads,mcp__slack-mcp__batch_get_messages"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
fi

# --- Preflight checks ---

if ! command -v claude >/dev/null 2>&1; then
  echo "ERROR: claude not on PATH" >&2
  exit 1
fi

if [[ ! -f "$SLACK_FILE" ]]; then
  echo "ERROR: $SLACK_FILE not found — nothing to backfill" >&2
  exit 1
fi

# --- Build scoped MCP config (same pattern as _drive_agent / _slack_mcp_config) ---

MCP_CONFIG_FILE=""
cleanup() {
  if [[ -n "$MCP_CONFIG_FILE" && -f "$MCP_CONFIG_FILE" ]]; then
    rm -f "$MCP_CONFIG_FILE"
  fi
}
trap cleanup EXIT

# Find and extract the slack-mcp entry from the user's claude config, build a
# minimal --mcp-config doc. Falls back to the documented AIM/SAFE_MODE spec.
MCP_CONFIG_FILE="$(mktemp /tmp/slack-backfill-mcp-XXXXXX.json)"
python3 -c "
import json, os, sys
from pathlib import Path

# Same search order as _find_slack_mcp_entry in slack.py
configs = [
    Path('$PROJ') / '.mcp.json',
    Path.home() / '.claude.json',
    Path.home() / '.claude' / 'mcp.json',
    Path.home() / '.config' / 'claude' / 'mcp.json',
]

fallback = {'command': 'aim', 'args': ['mcp', 'start-server', 'slack-mcp'], 'env': {'SAFE_MODE': 'true'}}
entry = None

for cfg in configs:
    try:
        doc = json.loads(cfg.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        continue
    if not isinstance(doc, dict):
        continue
    # Check top-level mcpServers
    servers = doc.get('mcpServers')
    if isinstance(servers, dict):
        for name, e in servers.items():
            if 'slack' in str(name).lower() and isinstance(e, dict):
                entry = e
                break
    if entry:
        break
    # Check projects.*.mcpServers
    projects = doc.get('projects')
    if isinstance(projects, dict):
        for proj in projects.values():
            if isinstance(proj, dict):
                srv = proj.get('mcpServers')
                if isinstance(srv, dict):
                    for name, e in srv.items():
                        if 'slack' in str(name).lower() and isinstance(e, dict):
                            entry = e
                            break
                if entry:
                    break
    if entry:
        break

if entry is None:
    entry = dict(fallback)

# Scrub any credential-looking material (same as slack.py)
secret_markers = ('~/.midway', '.midway', 'cookie', 'mwinit', 'aws_secret', 'authorization:')
def looks_secret(s):
    return any(m in s.lower() for m in secret_markers)

clean = {}
allowed_keys = ('command', 'args', 'env', 'type', 'url', 'headers')
for key in allowed_keys:
    if key not in entry:
        continue
    val = entry[key]
    if isinstance(val, str):
        if not looks_secret(val):
            clean[key] = val
    elif isinstance(val, list):
        clean[key] = [v for v in val if not (isinstance(v, str) and looks_secret(v))]
    elif isinstance(val, dict):
        clean[key] = {k: v for k, v in val.items() if not (looks_secret(str(k)) or (isinstance(v, str) and looks_secret(v)))}
    else:
        clean[key] = val

json.dump({'mcpServers': {'slack-mcp': clean}}, sys.stdout)
" > "$MCP_CONFIG_FILE"

# --- Build the backfill prompt ---

if [[ "$DRY_RUN" == "true" ]]; then
  WRITE_INSTRUCTION="Do NOT write anything back. Instead, include a 'dryRun': true field in your JSON output and list what you WOULD have written for each item."
else
  WRITE_INSTRUCTION="Write the updated items back to $SLACK_FILE atomically: write to a .tmp file in the same directory, then rename it over the original (os.replace pattern). Preserve ALL existing fields on every item; only ADD channelId and/or userId to items you resolved."
fi

PROMPT="You are a ONE-TIME backfill agent. Your job is to resolve Slack channelId and userId for existing queue items that lack them. You are READ-ONLY: you MUST NEVER send, post, reply, or call any Slack write tool.

TASK:
1. Read the file $SLACK_FILE. It contains {\"items\": [...]}, where each item has fields like id, sender, channel (e.g. \"DM with Huan Wang\"), channelType, etc. Some items may already have channelId/userId — skip those.

2. For each item that has NO channelId (or channelId is empty/absent):
   a. Call mcp__slack-mcp__list_dms to get the list of DM conversations. Each DM in the result has a channelId (the Slack conversation id: D... for 1:1, C.../G... for group DMs) and a userId (the U.../W... id of the other participant).
   b. Match the item's 'channel' field (e.g. \"DM with Huan Wang\") to the correct DM conversation from list_dms. Match by the display name in the DM metadata. For channelType \"mention\", use the channel name to find the channel id.
   c. AMBIGUITY RULE: if multiple DM conversations could match (e.g. multiple users named \"Huan Wang\"), do NOT guess. Leave channelId empty for that item. Log it in the unresolved list.
   d. On a successful unambiguous match, record: channelId = the conversation's channel id (D.../C.../G...), userId = the other participant's user id (U.../W...).

3. $WRITE_INSTRUCTION

4. Return ONLY a STRICT JSON object (no prose, no markdown fences):
{\"resolved\": <number of items resolved>, \"unresolved\": <number left without channelId>, \"details\": [{\"id\": \"<item id>\", \"sender\": \"<name>\", \"status\": \"resolved\"|\"unresolved\", \"reason\": \"<why unresolved, if applicable>\"}]}

CONSTRAINTS:
- NEVER call post_message or any send/write tool. You are READ-ONLY.
- NEVER fabricate or guess a channelId/userId. Only use ids that come directly from list_dms or lookup_user results.
- Preserve ALL existing fields on every item. Only ADD channelId/userId.
- If list_dms doesn't return enough info to resolve an item, leave it unresolved.
- Handle the get_unreads/list_dms datamark format: if text contains U+E000 sentinels, replace them with spaces."

# --- Run the backfill subprocess ---

echo "=== Slack channel-id backfill ==="
echo "File: $SLACK_FILE"
echo "Dry run: $DRY_RUN"
echo "Allowed tools: READ-ONLY (no post_message)"
echo ""
echo "Spawning claude --print subprocess..."
echo ""

RESULT=$(claude --print \
  --allowedTools "$ALLOWED_TOOLS" \
  --mcp-config "$MCP_CONFIG_FILE" \
  --strict-mcp-config \
  "$PROMPT" 2>/dev/null) || {
  echo "ERROR: claude subprocess failed (exit $?)" >&2
  exit 1
}

# --- Output the summary ---

echo "=== Backfill result ==="
echo "$RESULT"
