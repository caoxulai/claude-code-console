"""CRUD /api/memory — manage ~/.claude/projects/.../memory/ files."""
from __future__ import annotations

import re
from pathlib import Path

from aiohttp import web

from server import filestore


MEMORY_DIR = Path.home() / ".claude" / "projects" / "-local-home-xulaicao" / "memory"


def register(app: web.Application):
    app.router.add_get("/api/memory/files", list_files)
    app.router.add_get("/api/memory/files/{name}", get_file)
    app.router.add_put("/api/memory/files/{name}", put_file)
    app.router.add_post("/api/memory/files", create_file)
    app.router.add_delete("/api/memory/files/{name}", delete_file)


def _safe_name(name: str) -> str:
    """Sanitize filename to prevent directory traversal."""
    name = name.strip()
    if not name.endswith(".md"):
        name += ".md"
    if "/" in name or "\\" in name or ".." in name:
        raise web.HTTPBadRequest(reason="invalid filename")
    return name


def _file_meta(path: Path) -> dict:
    """Extract metadata from a memory file."""
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    meta_type = "unknown"
    description = ""
    # Parse YAML frontmatter
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            fm = content[3:end]
            for line in fm.splitlines():
                if line.strip().startswith("type:"):
                    meta_type = line.split(":", 1)[1].strip()
                elif "type:" in line and "metadata" not in line:
                    meta_type = line.split("type:", 1)[1].strip()
                if line.strip().startswith("description:"):
                    description = line.split(":", 1)[1].strip()
    # Infer type from filename prefix if not in frontmatter
    if meta_type == "unknown":
        stem = path.stem
        if stem.startswith("feedback_principle"):
            meta_type = "principle"
        elif stem.startswith("feedback_"):
            meta_type = "feedback"
        elif stem.startswith("project_"):
            meta_type = "project"
        elif stem.startswith("reference_"):
            meta_type = "reference"
        elif stem.startswith("user_"):
            meta_type = "user"
        elif stem == "MEMORY":
            meta_type = "index"
    stat = path.stat()
    return {
        "name": path.name,
        "type": meta_type,
        "description": description,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


async def list_files(request: web.Request) -> web.Response:
    if not MEMORY_DIR.is_dir():
        return web.json_response([])
    files = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        try:
            files.append(_file_meta(f))
        except OSError:
            continue
    return web.json_response(files)


async def get_file(request: web.Request) -> web.Response:
    name = _safe_name(request.match_info["name"])
    path = MEMORY_DIR / name
    content, etag = filestore.read_text(path)
    if not content and etag is None:
        raise web.HTTPNotFound(reason=f"{name} not found")
    return web.json_response({"name": name, "content": content, "etag": etag})


async def put_file(request: web.Request) -> web.Response:
    name = _safe_name(request.match_info["name"])
    path = MEMORY_DIR / name
    body = await request.json()
    content = body.get("content", "")
    expected_etag = body.get("etag")

    try:
        new_etag = filestore.write_text(path, content, expected_etag)
    except filestore.ConflictError as e:
        current, current_etag = filestore.read_text(path)
        return web.json_response(
            {"error": "conflict", "message": str(e), "current": current, "etag": current_etag},
            status=409,
        )

    ws = request.app["ws_manager"]
    await ws.broadcast("memory_changed", {"name": name, "etag": new_etag})
    return web.json_response({"name": name, "etag": new_etag})


async def create_file(request: web.Request) -> web.Response:
    body = await request.json()
    name = _safe_name(body.get("name", ""))
    content = body.get("content", "")
    path = MEMORY_DIR / name

    if path.exists():
        raise web.HTTPConflict(reason=f"{name} already exists")

    new_etag = filestore.write_text(path, content)
    ws = request.app["ws_manager"]
    await ws.broadcast("memory_changed", {"name": name, "etag": new_etag})
    return web.json_response({"name": name, "etag": new_etag}, status=201)


async def delete_file(request: web.Request) -> web.Response:
    name = _safe_name(request.match_info["name"])
    if name == "MEMORY.md":
        raise web.HTTPForbidden(reason="cannot delete MEMORY.md index")
    path = MEMORY_DIR / name
    if not path.exists():
        raise web.HTTPNotFound(reason=f"{name} not found")
    filestore.delete_file(path)
    ws = request.app["ws_manager"]
    await ws.broadcast("memory_deleted", {"name": name})
    return web.json_response({"deleted": name})
