"""Packaging metadata + the tracked dist-copy step (D-078 items 22/23).

Two properties are pinned here:

  * SINGLE-SOURCE: pyproject.toml is the ONLY place packaging metadata lives —
    every field that used to be split across setup.py (version, description,
    requires-python, dependencies, the console script, package discovery,
    package-data) must be present in pyproject, `dynamic` must be GONE, and
    setup.py must no longer exist. A half-move (pyproject touched but setup.py
    still present, or `dynamic` still pointing at a deleted setup.py) breaks
    `pip install .`.
  * TRACKED COPY STEP: `npm run build` must populate server/static/ by itself
    via a postbuild hook — a copy script nobody invokes ships a UI-less package.
    The script's behavior is exercised for real (node, throwaway tree), not
    grepped.

Hermetic: pure file reads plus `node` on a tmp_path tree. No pip install, no
network, no repo mutation.
"""

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
COPY_SCRIPT = REPO_ROOT / "scripts" / "copy-dist.mjs"

EXPECTED_DEPENDENCIES = {
    "aiohttp": ">=3.9",
    "python-dotenv": ">=1.0",
    "croniter": ">=2.0",
    "mcp": ">=1.27,<2",
}


@pytest.fixture(scope="module")
def pyproject():
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


# --------------------------------------------------------------------------
# SINGLE-SOURCE: pyproject carries everything, setup.py is gone
# --------------------------------------------------------------------------


def test_version_is_canonical_1_0_0(pyproject):
    """VERSION.md documents v1.0 — the shipped version must match it (not 0.2.0)."""
    assert pyproject["project"]["version"] == "1.0.0"


def test_dynamic_key_is_absent(pyproject):
    """`dynamic = [scripts, dependencies]` pointed at setup.py, which is deleted."""
    assert "dynamic" not in pyproject["project"], (
        "a `dynamic` key would send setuptools looking for the deleted setup.py"
    )


def test_setup_py_does_not_exist():
    assert not (REPO_ROOT / "setup.py").exists(), (
        "setup.py is the second metadata source — it must be deleted, not kept "
        "alongside pyproject.toml"
    )


def test_console_script_entry_point(pyproject):
    assert pyproject["project"]["scripts"] == {"claude-web": "server.cli:main"}


def test_runtime_dependencies_survived_the_move(pyproject):
    deps = pyproject["project"]["dependencies"]
    parsed = {}
    for spec in deps:
        for sep_idx, ch in enumerate(spec):
            if ch in "<>=!~":
                parsed[spec[:sep_idx].strip()] = spec[sep_idx:].strip()
                break
        else:
            parsed[spec.strip()] = ""
    assert parsed == EXPECTED_DEPENDENCIES


def test_requires_python(pyproject):
    assert pyproject["project"]["requires-python"] == ">=3.10"


def test_description_survived_the_move(pyproject):
    assert pyproject["project"]["description"].strip(), "description must not be empty"


def test_package_discovery_and_package_data(pyproject):
    tool = pyproject["tool"]["setuptools"]
    assert tool["packages"]["find"]["include"] == ["server*"]
    assert tool["package-data"]["server"] == ["static/**/*"]


def test_no_data_files_declaration():
    """data_files installed static copies to sys.prefix (venv pollution)."""
    text = PYPROJECT.read_text()
    assert "data-files" not in text and "data_files" not in text


def test_dev_extra_and_pytest_config_preserved(pyproject):
    dev = pyproject["project"]["optional-dependencies"]["dev"]
    assert any(d.startswith("pytest-aiohttp") for d in dev)
    assert pyproject["tool"]["pytest"]["ini_options"]["asyncio_mode"] == "auto"


def test_manifest_does_not_reference_frontend_dist():
    """frontend/dist is gitignored — an sdist include of it is dead weight."""
    manifest = REPO_ROOT / "MANIFEST.in"
    text = manifest.read_text() if manifest.exists() else ""
    assert "frontend/dist" not in text


# --------------------------------------------------------------------------
# TRACKED COPY STEP
# --------------------------------------------------------------------------


def test_copy_script_exists():
    assert COPY_SCRIPT.exists(), "scripts/copy-dist.mjs must exist"


def test_npm_build_triggers_the_copy_step():
    """A script nobody invokes is the UI-LESS PACKAGE trap — wire it to postbuild."""
    pkg = json.loads((REPO_ROOT / "frontend" / "package.json").read_text())
    postbuild = pkg["scripts"].get("postbuild", "")
    assert "copy-dist.mjs" in postbuild, (
        f"frontend postbuild must run the copy step, got {postbuild!r}"
    )
    assert postbuild.startswith("node "), "the copy step must be plain node, no new dependency"


def test_copy_script_resolves_paths_from_its_own_location():
    """cwd-relative resolution breaks when npm runs it from frontend/."""
    text = COPY_SCRIPT.read_text()
    assert "import.meta.url" in text
    assert "process.cwd()" not in text


def test_server_static_output_stays_gitignored():
    text = (REPO_ROOT / ".gitignore").read_text()
    assert "server/static/" in text
    assert "build-tools/bin/custom-build" not in text, (
        ".gitignore credits a copy step that no longer exists — stale comment"
    )


@pytest.fixture
def fake_tree(tmp_path):
    """A throwaway repo root holding the real copy script under test."""
    if not COPY_SCRIPT.exists():
        pytest.fail(f"{COPY_SCRIPT} does not exist — the copy step must be created")
    if shutil.which("node") is None:
        pytest.skip("node not available")
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "copy-dist.mjs").write_text(COPY_SCRIPT.read_text())

    def run(cwd=None):
        return subprocess.run(
            ["node", str(root / "scripts" / "copy-dist.mjs")],
            cwd=str(cwd or (root / "frontend")),
            capture_output=True,
            text=True,
            timeout=60,
        )

    return root, run


def test_copy_step_populates_server_static(fake_tree):
    root, run = fake_tree
    dist = root / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>real ui</html>")
    (dist / "assets" / "app-abc123.js").write_text("console.log(1)")

    result = run()
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    static = root / "server" / "static"
    assert (static / "index.html").read_text() == "<html>real ui</html>"
    assert (static / "assets" / "app-abc123.js").read_text() == "console.log(1)"


def test_copy_step_replaces_stale_output(fake_tree):
    """A stale hashed bundle left behind would be served forever."""
    root, run = fake_tree
    dist = root / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("new")
    static = root / "server" / "static"
    static.mkdir(parents=True)
    (static / "stale-bundle.js").write_text("old")

    assert run().returncode == 0
    assert not (static / "stale-bundle.js").exists()
    assert (static / "index.html").read_text() == "new"


def test_copy_step_dereferences_symlinked_dist(fake_tree):
    """server/static must be a REAL tree — a symlink there is not shippable package
    data (and a symlinked frontend/dist is common in linked worktrees/CI caches)."""
    root, run = fake_tree
    real = root / "elsewhere-dist"
    real.mkdir(parents=True)
    (real / "index.html").write_text("linked ui")
    (root / "frontend").mkdir(parents=True, exist_ok=True)
    (root / "frontend" / "dist").symlink_to(real, target_is_directory=True)

    assert run().returncode == 0
    static = root / "server" / "static"
    assert not static.is_symlink(), "server/static must be a real directory, not a symlink"
    assert (static / "index.html").read_text() == "linked ui"
    assert not (static / "index.html").is_symlink()


def test_copy_step_fails_loudly_without_dist(fake_tree):
    root, run = fake_tree
    result = run(cwd=root)
    assert result.returncode != 0, "a missing frontend/dist must be a loud failure"
    assert "dist" in (result.stderr + result.stdout).lower()
