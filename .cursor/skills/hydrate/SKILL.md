---
name: hydrate
description: Load full project context by reading PROJECT_CONTEXT.md and gathering current git state. Use when the user says hydrate, load context, or read the project context.
---

# Hydrate — Project Context Loader

Load context for the Nyx genomic compression CLI project. Complete both phases before responding to any further instructions.

## Phase 1: Load Static Context

Read the following file from the repository root. This is the primary knowledge base about the project:

- `docs/PROJECT_CONTEXT.md` — Full architecture, components, data flow, build instructions, and detailed breakdowns of every file in the project.

After reading, internalize the content. You now understand what this project does, how it's structured, how to build and run it, and how every component connects.

## Phase 2: Gather Dynamic State

Run the following commands to understand the current state of the repository:

1. **Current branch and status:**

   ```bash
   git branch --show-current && git status --short
   ```

2. **Recent commits (last 10):**

   ```bash
   git log --oneline -10
   ```

3. **Uncommitted changes (if any):**

   ```bash
   git diff --stat
   ```

4. **Diff from main branch (if on a feature branch):**

   ```bash
   git diff --stat main 2>/dev/null || git diff --stat master 2>/dev/null || echo "On default branch"
   ```

5. **Any stashed work:**

   ```bash
   git stash list
   ```

## After Hydration

Once both phases are complete, respond with a brief confirmation that includes:

- Project name and one-sentence summary
- Current branch and whether the working tree is clean or has changes
- Last commit message
- A note that you are ready to help

Do NOT dump the entire PROJECT_CONTEXT.md back to the user. Just confirm you've loaded it and show the dynamic state summary.
