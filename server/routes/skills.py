"""CRUD /api/skills — manage ~/.claude/skills/ and ~/.claude/commands/."""
from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp import web

from server.routes import read_json_body

from server import filestore


SKILLS_DIR = Path.home() / ".claude" / "skills"
COMMANDS_DIR = Path.home() / ".claude" / "commands"


def register(app: web.Application):
    app.router.add_get("/api/skills", list_skills)
    app.router.add_get("/api/skills/{name}", get_skill)
    app.router.add_put("/api/skills/{name}", put_skill)
    app.router.add_post("/api/skills", create_skill)
    app.router.add_delete("/api/skills/{name}", delete_skill)


def _parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter fields."""
    meta = {}
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            for line in content[3:end].splitlines():
                if ":" in line:
                    key, val = line.split(":", 1)
                    meta[key.strip()] = val.strip()
    return meta


def _list_local_skills() -> list[dict]:
    skills = []
    if SKILLS_DIR.is_dir():
        for d in sorted(SKILLS_DIR.iterdir()):
            skill_file = d / "SKILL.md" if d.is_dir() else None
            if skill_file and skill_file.exists():
                # A single unreadable/non-UTF-8 SKILL.md must not 500 the whole
                # inventory — skip it.
                try:
                    content = skill_file.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                meta = _parse_frontmatter(content)
                skills.append({
                    "name": d.name,
                    "description": meta.get("description", ""),
                    "tags": meta.get("tags", ""),
                    "source": "local",
                    "path": str(skill_file),
                })
    if COMMANDS_DIR.is_dir():
        for f in sorted(COMMANDS_DIR.glob("*.md")):
            try:
                content = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            meta = _parse_frontmatter(content)
            skills.append({
                "name": f.stem,
                "description": meta.get("description", ""),
                "tags": "",
                "source": "command",
                "path": str(f),
            })
    return skills


async def list_skills(request: web.Request) -> web.Response:
    # Offload the directory traversal + per-file read_text() to a worker thread
    # so a large ~/.claude/skills + ~/.claude/commands inventory never blocks the
    # event loop (B7). Pure offload — same response shape, no caching.
    skills = await asyncio.to_thread(_list_local_skills)
    return web.json_response(skills)


async def get_skill(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    # Check skills dir first, then commands
    skill_file = SKILLS_DIR / name / "SKILL.md"
    if not skill_file.exists():
        skill_file = COMMANDS_DIR / f"{name}.md"
    if not skill_file.exists():
        raise web.HTTPNotFound(reason=f"skill {name} not found")
    content, etag = filestore.read_text(skill_file)
    return web.json_response({"name": name, "content": content, "etag": etag, "path": str(skill_file)})


async def put_skill(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    body = await read_json_body(request)
    content = body.get("content", "")
    expected_etag = body.get("etag")

    skill_file = SKILLS_DIR / name / "SKILL.md"
    if not skill_file.exists():
        skill_file = COMMANDS_DIR / f"{name}.md"
    if not skill_file.exists():
        raise web.HTTPNotFound(reason=f"skill {name} not found")

    try:
        new_etag = filestore.write_text(skill_file, content, expected_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_text(skill_file)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )
    ws = request.app["ws_manager"]
    await ws.broadcast("skill_changed", {"name": name})
    return web.json_response({"name": name, "etag": new_etag})


async def create_skill(request: web.Request) -> web.Response:
    body = await read_json_body(request)
    name = body.get("name", "").strip()
    content = body.get("content", "")
    if not name or "/" in name or ".." in name:
        raise web.HTTPBadRequest(reason="invalid skill name")

    skill_dir = SKILLS_DIR / name
    if skill_dir.exists():
        raise web.HTTPConflict(reason=f"skill {name} already exists")

    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    new_etag = filestore.write_text(skill_file, content)
    ws = request.app["ws_manager"]
    await ws.broadcast("skill_changed", {"name": name})
    return web.json_response({"name": name, "etag": new_etag}, status=201)


async def delete_skill(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    skill_dir = SKILLS_DIR / name
    skill_file = COMMANDS_DIR / f"{name}.md"

    if skill_dir.is_dir():
        import shutil
        shutil.rmtree(skill_dir)
    elif skill_file.exists():
        skill_file.unlink()
    else:
        raise web.HTTPNotFound(reason=f"skill {name} not found")

    ws = request.app["ws_manager"]
    await ws.broadcast("skill_deleted", {"name": name})
    return web.json_response({"deleted": name})
