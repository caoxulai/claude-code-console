#!/usr/bin/env node
// Copy the built frontend into server/static/ so `pip install .` ships the UI
// as package data (pyproject [tool.setuptools.package-data]). Wired as the
// frontend's `postbuild`, so a plain `npm run build` populates it automatically.
//
// Paths resolve from this file's own location, never the current working
// directory — npm runs postbuild from frontend/, a human from the repo root.
import { existsSync, realpathSync, rmSync, cpSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dist = resolve(repoRoot, "frontend", "dist");
const staticDir = resolve(repoRoot, "server", "static");

if (!existsSync(dist)) {
  console.error(
    `copy-dist: ${dist} does not exist — run \`npm run build\` in frontend/ first.`,
  );
  process.exit(1);
}

// Wipe first: hashed bundles from a previous build would otherwise linger and
// be shipped alongside the current ones.
rmSync(staticDir, { recursive: true, force: true });
// dereference: server/static must be a real tree — a symlink (dist itself is one
// in linked worktrees) is not shippable package data.
cpSync(realpathSync(dist), staticDir, { recursive: true, dereference: true });
console.log(`copy-dist: ${dist} -> ${staticDir}`);
