#!/usr/bin/env bash
# test_slack_warmup_retire.sh — verify scripts/slack-warmup-session.sh `ensure` is
# SELF-RETIRING: it removes ONLY its own managed crontab block (never unrelated
# entries), is idempotent (a second run is a clean no-op), and prints the manual
# removal commands. It must NOT touch the real crontab or a real tmux server.
#
# HOW IT WORKS: we put fake `crontab` and `tmux` executables on PATH (in a temp
# dir prepended to PATH) so the script's `crontab -l` / `crontab -` and all tmux
# calls hit our stubs, which read/write a fixture file under $TMPDIR. The real
# crontab/tmux on this host are never invoked. No bash-test harness exists in this
# repo, so this is a self-contained script: it exits non-zero on the first failed
# assertion and prints "ALL PASS" + exit 0 when every assertion holds.
#
# RUN:  bash tests/test_slack_warmup_retire.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/slack-warmup-session.sh"
[ -f "$SCRIPT" ] || { echo "FAIL: $SCRIPT not found"; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

CRONFILE="$WORK/crontab.txt"   # the fake crontab store our stubs read/write
BIN="$WORK/bin"                # fake executables prepended to PATH
mkdir -p "$BIN"

# --- fake `crontab`: -l prints the store (exit 3 if absent, like real crontab);
#     `crontab -` (or `crontab <file>`) installs stdin/file into the store. -----
cat > "$BIN/crontab" <<CRONEOF
#!/usr/bin/env bash
store="$CRONFILE"
if [ "\${1:-}" = "-l" ]; then
  if [ -s "\$store" ]; then cat "\$store"; exit 0; else echo "no crontab for user" >&2; exit 1; fi
elif [ "\${1:-}" = "-" ] || [ -z "\${1:-}" ]; then
  cat > "\$store"          # install from stdin
else
  cat "\$1" > "\$store"     # install from a named file
fi
CRONEOF
chmod +x "$BIN/crontab"

# --- fake `tmux`: never has a session, no-ops everything. Records nothing real. -
cat > "$BIN/tmux" <<'TMUXEOF'
#!/usr/bin/env bash
case "$*" in
  *has-session*) exit 1 ;;          # never running
  *list-sessions*) exit 0 ;;        # no sessions
  *) exit 0 ;;                      # kill-session / anything else: no-op success
esac
TMUXEOF
chmod +x "$BIN/tmux"

export PATH="$BIN:$PATH"

fail() { echo "FAIL: $*"; exit 1; }

# --- Fixture: a crontab with BOTH the managed slack-warmup block AND an unrelated
#     pipeline-stats-dashboard entry (which must survive byte-for-byte). The
#     pipeline-stats block has its OWN CRON_TZ line, which must NOT be collateral. -
PIPELINE_LINE_TZ='CRON_TZ=America/Los_Angeles'
PIPELINE_LINE_CMD='17 * * * * cd /home/me/proj && bash scripts/pipeline-stats-dashboard.sh refresh >> /tmp/ps.log 2>&1'
cat > "$CRONFILE" <<FIXEOF
# === pipeline-stats-dashboard (managed) ===
$PIPELINE_LINE_TZ
$PIPELINE_LINE_CMD
# === claude-web slack-warmup (managed) ===
# claude-web Slack warm-up — keep the tmux host (idle Claude REPL) alive.
CRON_TZ=America/Los_Angeles
@reboot bash scripts/slack-warmup-session.sh ensure >> /home/testuser/projects/claude-web/.claude/slack-warmup-cron.log 2>&1
41 * * * * cd /home/testuser/projects/claude-web && bash scripts/slack-warmup-session.sh ensure >> /home/testuser/projects/claude-web/.claude/slack-warmup-cron.log 2>&1
FIXEOF

# ============================================================================
# (a) FIRST `ensure`: removes ALL slack-warmup lines, preserves pipeline-stats
#     block byte-for-byte, and prints the manual removal commands.
# ============================================================================
OUT1="$(bash "$SCRIPT" ensure 2>&1)" || fail "ensure exited non-zero (run 1)"

# Zero slack-warmup-session.sh lines remain in the resulting crontab.
if grep -q 'slack-warmup-session.sh' "$CRONFILE"; then
  echo "----- resulting crontab -----"; cat "$CRONFILE"
  fail "slack-warmup-session.sh lines still present after ensure"
fi
# The managed marker comment is gone too.
if grep -qF '# === claude-web slack-warmup (managed) ===' "$CRONFILE"; then
  fail "slack-warmup managed marker still present after ensure"
fi
# The unrelated pipeline-stats entries survive BYTE-FOR-BYTE.
grep -qF "$PIPELINE_LINE_CMD" "$CRONFILE" || fail "pipeline-stats command line was lost"
grep -qF '# === pipeline-stats-dashboard (managed) ===' "$CRONFILE" \
  || fail "pipeline-stats marker comment was lost"
# Its CRON_TZ must survive — exactly one CRON_TZ line should remain (the
# pipeline-stats one), proving the slack-warmup CRON_TZ was removed but not this.
tz_count="$(grep -c '^CRON_TZ=' "$CRONFILE" || true)"
[ "$tz_count" = "1" ] || fail "expected exactly 1 surviving CRON_TZ line, got $tz_count"
# The run summary prints the manual removal commands.
echo "$OUT1" | grep -qF "crontab -l 2>/dev/null | grep -v 'slack-warmup-session.sh'" \
  || fail "run summary did not print the manual crontab-removal command"
echo "$OUT1" | grep -qF 'tmux -L claude-web kill-session -t claude-web-slack' \
  || fail "run summary did not print the manual tmux-stop command"

# Snapshot the cleaned crontab for the idempotence check.
CLEANED="$(cat "$CRONFILE")"

# ============================================================================
# (b) SECOND `ensure` on the already-clean crontab: clean no-op (exit 0,
#     crontab unchanged).
# ============================================================================
OUT2="$(bash "$SCRIPT" ensure 2>&1)" || fail "ensure exited non-zero (run 2 / idempotence)"
if [ "$(cat "$CRONFILE")" != "$CLEANED" ]; then
  echo "----- before -----"; printf '%s\n' "$CLEANED"
  echo "----- after  -----"; cat "$CRONFILE"
  fail "second ensure mutated the already-clean crontab (not idempotent)"
fi
# Second run still says there is nothing to remove (no-op messaging).
echo "$OUT2" | grep -qiE 'nothing to remove|no slack-warmup crontab block' \
  || fail "second ensure did not report a no-op"
# (c) Manual removal commands are printed on the no-op run too.
echo "$OUT2" | grep -qF 'Manual removal' \
  || fail "second ensure did not print the manual removal commands"

# ============================================================================
# (d) No-crontab case: a totally empty/absent crontab is a clean no-op, exit 0.
# ============================================================================
: > "$CRONFILE"   # empty store => fake `crontab -l` reports "no crontab"
OUT3="$(bash "$SCRIPT" ensure 2>&1)" || fail "ensure exited non-zero on empty crontab"
echo "$OUT3" | grep -qiE 'no crontab present|nothing to remove' \
  || fail "empty-crontab ensure did not report a clean no-op"

echo "ALL PASS"
