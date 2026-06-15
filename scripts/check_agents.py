#!/usr/bin/env python3
"""Report which role agents Claude Code would actually load in a given directory.

Claude Code discovers subagents from two places at startup:
  - ~/.claude/agents/*.md          (global / "universal" — every session)
  - <cwd>/.claude/agents/*.md      (project — only when launched in that dir)

The #1 reason a project's agents "don't trigger" is a cwd mismatch: a session
started in the home directory sees ONLY the global agents, never the project
team. This script makes that visible BEFORE you start work (or before a
/dev-team run), and it doubles as the source of the role list dev-team passes
as args.roleAgents.

Usage:
    python scripts/check_agents.py [PROJECT_DIR]      # human-readable report
    python scripts/check_agents.py [PROJECT_DIR] --json   # machine-readable

PROJECT_DIR defaults to the current working directory. Exit code is non-zero
if a project .md is malformed or (in --strict) if the dir has no project agents.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

GLOBAL_AGENTS_DIR = Path.home() / ".claude" / "agents"
# Roles that write code and therefore need the Write tool to CREATE new files
# (Edit alone can't). Used only to surface a warning, not to fail.
_IMPLEMENTER_HINT = re.compile(r"dev$|coder|implement", re.IGNORECASE)


def _parse_frontmatter(text: str) -> dict | None:
    """Return the YAML-ish frontmatter as a flat dict, or None if absent/broken.

    Deliberately tiny — matches the subset Claude Code subagents use
    (name/description/tools/model as `key: value` lines). Not a full YAML parser.
    """
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not m:
        return None
    fields: dict = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    return fields


def _scan_dir(d: Path, scope: str) -> tuple[list[dict], list[str]]:
    """Parse every *.md in d into a role record. Returns (records, problems)."""
    records: list[dict] = []
    problems: list[str] = []
    if not d.is_dir():
        return records, problems
    for f in sorted(d.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            problems.append(f"{f}: unreadable ({e})")
            continue
        fm = _parse_frontmatter(text)
        body = text[text.index("---", 3) + 3:].strip() if fm else text.strip()
        if fm is None:
            problems.append(f"{f}: no YAML frontmatter — Claude will ignore this file")
            continue
        if not fm.get("name"):
            problems.append(f"{f}: frontmatter missing `name:`")
        if not fm.get("description"):
            problems.append(f"{f}: missing `description:` — auto-delegation can't trigger it")
        if not body:
            problems.append(f"{f}: empty body (no system prompt)")
        tools = fm.get("tools", "")
        tool_list = [t.strip() for t in tools.split(",") if t.strip()] if tools else []
        # An implementer with no Write tool can't create new files.
        looks_impl = bool(_IMPLEMENTER_HINT.search(f.stem) or _IMPLEMENTER_HINT.search(fm.get("description", "")))
        if tool_list and looks_impl and "Write" not in tool_list:
            problems.append(
                f"{f.stem}: looks like an implementer but has no `Write` tool — "
                f"it can Edit existing files but cannot CREATE new ones"
            )
        records.append({
            "slug": f.stem,
            "name": fm.get("name", f.stem),
            "scope": scope,
            "description": fm.get("description", ""),
            "tools": tool_list or "(inherits all)",
            "model": fm.get("model", ""),
            "path": str(f),
        })
    return records, problems


def collect(project_dir: Path) -> dict:
    """Build the full picture of what would load in `project_dir`."""
    proj_records, proj_problems = _scan_dir(project_dir / ".claude" / "agents", "project")
    glob_records, glob_problems = _scan_dir(GLOBAL_AGENTS_DIR, "global")

    # Project agents shadow same-named globals (Claude Code precedence).
    proj_slugs = {r["slug"] for r in proj_records}
    visible_global = [r for r in glob_records if r["slug"] not in proj_slugs]
    shadowed = [r["slug"] for r in glob_records if r["slug"] in proj_slugs]

    home = Path.home().resolve()
    is_home = project_dir.resolve() == home

    return {
        "projectDir": str(project_dir.resolve()),
        "isHomeDir": is_home,
        "projectAgents": proj_records,
        "globalAgents": visible_global,
        "shadowedGlobals": shadowed,
        # The list dev-team should pass as args.roleAgents = everything that
        # would actually resolve via agentType in a session rooted here.
        "availableRoles": sorted(
            {r["slug"] for r in proj_records} | {r["slug"] for r in visible_global}
        ),
        # Project problems are yours to fix and drive the exit code. Global
        # problems (e.g. an AIM-managed agent's non-standard frontmatter) are
        # informational — out of this project's control — so they never fail
        # the check.
        "problems": proj_problems,
        "globalProblems": glob_problems,
    }


def _print_report(info: dict) -> None:
    print(f"Agents Claude would load in: {info['projectDir']}\n")

    if info["isHomeDir"]:
        print("⚠️  This IS the home directory. A session here loads ONLY global")
        print("    agents — no project team. Start work in the project's own")
        print("    directory (or set the claude-web chat cwd to it) to load its")
        print("    role agents.\n")

    proj = info["projectAgents"]
    if proj:
        print(f"PROJECT agents ({len(proj)}) — .claude/agents/:")
        for r in proj:
            tools = r["tools"] if isinstance(r["tools"], str) else ", ".join(r["tools"])
            print(f"  • {r['slug']:<18} [{tools}]")
            print(f"      {r['description'][:96]}")
    else:
        print("PROJECT agents: NONE. This project has no .claude/agents/*.md —")
        print("  a session here gets only the global agents below. /dev-team will")
        print("  fall back to generic coders (no project context).")
    print()

    glob = info["globalAgents"]
    print(f"GLOBAL agents ({len(glob)}) — ~/.claude/agents/:")
    for r in glob:
        print(f"  • {r['slug']}")
    if info["shadowedGlobals"]:
        print(f"  (shadowed by same-named project agents: {', '.join(info['shadowedGlobals'])})")
    print()

    print(f"Roles available to agentType here: {info['availableRoles'] or '(none)'}")
    print()

    if info["problems"]:
        print(f"⚠️  {len(info['problems'])} project-agent problem(s) — fix these:")
        for p in info["problems"]:
            print(f"  - {p}")
    else:
        print("✓ No project-agent problems — the project team is well-formed.")

    if info["globalProblems"]:
        print(f"\nℹ️  {len(info['globalProblems'])} global-agent note(s) (informational, "
              f"out of this project's control):")
        for p in info["globalProblems"]:
            print(f"  - {p}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project_dir", nargs="?", default=".", help="Project directory (default: cwd)")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    ap.add_argument("--strict", action="store_true", help="Exit non-zero if the dir has no project agents")
    args = ap.parse_args(argv)

    project_dir = Path(args.project_dir)
    if not project_dir.is_dir():
        print(f"error: not a directory: {project_dir}", file=sys.stderr)
        return 2

    info = collect(project_dir)
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        _print_report(info)

    if info["problems"]:
        return 1
    if args.strict and not info["projectAgents"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
