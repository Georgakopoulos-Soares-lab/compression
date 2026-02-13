---
name: update-context
description: Update PROJECT_CONTEXT.md and process developer change notes into docs/PROJECT_HISTORY.md. Use when the user says update context, refresh context, or sync docs.
---

# Update Context — Refresh Project Documentation

Update the project's reference documentation and process developer change notes. This skill has two responsibilities:

1. **Update `PROJECT_CONTEXT.md`** to reflect the current state of the codebase
2. **Process `docs/CHANGES.md`** into structured entries in `docs/PROJECT_HISTORY.md`

Complete both parts in order.

## Part A: Update PROJECT_CONTEXT.md

### Step 1: Load current documentation

Read `docs/PROJECT_CONTEXT.md` from the repository root.

### Step 2: Identify what changed

Run the following:

1. **Recent commits:**
   ```bash
   git log --oneline -20
   ```

2. **Files changed recently:**
   ```bash
   git log --pretty=format: --name-only -20 | sort -u | grep -v '^$'
   ```

3. **Current file listing:**
   Use the Glob tool with pattern `**/*` to list all project files, excluding `.git/` and `.claude/`.

4. **Check for dependency changes:**
   Read all build/config files (Makefile, Cargo.toml, pyproject.toml, package.json, CMakeLists.txt, or whatever build system this project uses).

5. **Scan for TODOs:**
   Use the Grep tool to search for `TODO|FIXME|HACK|XXX` across all source files (`.py`, `.rs`, `.c`, `.cpp`, `.h`, `.js`, `.ts`, `.go`, `.java`).

### Step 3: Read changed files

For every file that appeared in Step 2 results, read it in full.

### Step 4: Update PROJECT_CONTEXT.md

Rewrite `docs/PROJECT_CONTEXT.md` following these rules:
- Preserve the existing section structure
- Update every section affected by the changes
- Add new files to Directory Structure and Core Components
- Remove references to deleted files
- Update build instructions if dependencies changed
- Update Known Limitations and TODOs with current grep results
- Be specific — use actual file names, function names, and paths

## Part B: Process Change Notes

Read the reference file for detailed instructions on processing change notes:

See [REFERENCE.md](REFERENCE.md) for the complete changelog processing workflow.

Follow every step in REFERENCE.md to process `docs/CHANGES.md` into `docs/PROJECT_HISTORY.md`.

## Final Output

After completing both parts, respond with:

**Context update:**
- Which sections of PROJECT_CONTEXT.md were updated and why
- New files added to documentation
- Files removed from documentation

**Changelog:**
- How many change entries were processed from docs/CHANGES.md
- Summary of entries added to docs/PROJECT_HISTORY.md
- Confirmation that docs/CHANGES.md was cleared

**Issues:**
- Any `UNCLEAR:` items that need human input
