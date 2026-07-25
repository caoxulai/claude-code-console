#!/usr/bin/env bash
#
# check.sh — one-command repo check gate (D-076 item 8).
#
# Runs the same gates CI cares about, in order, and exits non-zero on the first
# FATAL failure so a red tree can never masquerade as green:
#
#   (a) backend tests   .venv/bin/python -m pytest tests/ -q      [FATAL]
#   (b) frontend tests  cd frontend && npm test                   [FATAL]
#   (c) frontend build  cd frontend && npm run build              [FATAL]
#   (d) lint            npx eslint .                               [non-fatal]
#
# `set -euo pipefail` means any un-guarded non-zero exit aborts the whole
# script — that is the structural guarantee against a false green, so never
# swallow a fatal step behind a pipe or subshell.
set -euo pipefail

# Resolve the repo root from THIS script's own location so the gate works from
# any caller cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

step() { printf '\n=== %s ===\n' "$1"; }

# (a) Backend tests — FATAL.
step "backend tests: pytest tests/ -q"
"${REPO_ROOT}/.venv/bin/python" -m pytest tests/ -q

# (b) Frontend tests — FATAL.
step "frontend tests: npm test"
( cd "${REPO_ROOT}/frontend" && npm test )

# (c) Frontend build — FATAL.
step "frontend build: npm run build"
( cd "${REPO_ROOT}/frontend" && npm run build )

# (d) Lint — NON-FATAL (reported only).
#
# The repo currently carries ~50 pre-existing lint errors that are scheduled for
# a later clean-up batch, so a lint failure here is REPORTED but does not fail
# the gate. `|| eslint_rc=$?` captures the exit code without `set -e` aborting.
#
# >>> FLIP-TO-FATAL POINT <<<
# Once the lint-cleanup batch lands and `npx eslint .` is clean, make lint fatal
# by deleting the `|| eslint_rc=$?` guard below (let `set -e` abort on failure)
# and removing this comment block.
step "lint: npx eslint . (non-fatal)"
eslint_rc=0
( cd "${REPO_ROOT}/frontend" && npx eslint . ) || eslint_rc=$?
if [ "${eslint_rc}" -eq 0 ]; then
  echo "eslint: clean"
else
  echo "eslint: reported ${eslint_rc} (NON-FATAL — pre-existing lint debt, scheduled for a later batch; not failing the gate)"
fi

step "check.sh: all fatal gates passed"
