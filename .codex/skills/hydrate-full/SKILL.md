---
name: hydrate-full
description: Full hydration — loads project context, gathers git state, AND fetches live OpenZL documentation from the web. Use when the user says hydrate-full, full hydrate, or load full context.
---

# Hydrate Full — Project Context + Live OpenZL Documentation

You are loading the complete context for the Nyx genomic compression CLI project, including live official documentation from the web. Complete ALL THREE phases before responding.

## Phase 1: Load Project Context

Read the following file. This is your primary knowledge base about the project:

- `docs/PROJECT_CONTEXT.md` — Full architecture, components, data flow, build instructions, and detailed breakdowns of every file in the project.

After reading, internalize the content.

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

## Phase 3: Fetch Live OpenZL Documentation

OpenZL is an open-source compression framework by Meta. Fetch the following official documentation pages **from the web** so you have up-to-date knowledge of OpenZL, its CLI, its SDDL schema language, and its APIs.

### 3a. Fetch these pages by browsing each URL directly:

1. **GitHub README** — project overview, build instructions, project status:
   `https://github.com/facebook/openzl`

2. **Quick Start** — hands-on CLI tutorial (compress, decompress, train, visualize):
   `https://openzl.org/getting-started/quick-start/`

3. **Introduction** — what OpenZL is, how it works, compression graph model:
   `https://openzl.org/getting-started/introduction/`

4. **Core Concepts** — codecs, graphs, edges, selectors, function graphs:
   `https://openzl.org/getting-started/concepts/`

5. **Using OpenZL** — decision tree for parsing structured data, SDDL vs custom parser:
   `https://openzl.org/getting-started/using-openzl/`

6. **CLI Guide** — profiles, training, inline training, strict mode, custom compressors:
   `https://openzl.org/getting-started/cli/`

7. **SDDL Overview** — what SDDL is, documentation structure, key features:
   `https://openzl.org/sddl/`

8. **SDDL for LLMs** — the complete SDDL v0.6 specification written specifically for AI agents. This is the most important reference for writing schemas:
   `https://openzl.org/sddl/sddl-for-llm/`

9. **SDDL API Reference** — current implementation syntax, built-in types, records, arrays, variables, operations, examples (SAO, BMP, STL):
   `https://openzl.org/api/c/graphs/sddl/`

### 3b. Internalize the content

After fetching all pages, you now understand:
- What OpenZL is and how it works (graph-based typed compression)
- How to use the `zli` CLI (compress, decompress, train, profiles, flags)
- How to write SDDL schemas (types, records, arrays, variables, expressions, expect, _rem)
- Training strategies (greedy, full-split, bottom-up, ACE, Pareto frontier)
- The difference between profiles and trained compressors
- How SDDL decomposes binary data into typed streams for compression

## After Hydration

Once all three phases are complete, respond with a brief confirmation that includes:

- Project name and one-sentence summary
- Current branch and whether the working tree is clean or has changes
- Last commit message
- Confirmation that you loaded live OpenZL documentation (list the pages fetched)
- A note that you are ready to help

Do NOT dump the contents of PROJECT_CONTEXT.md or the fetched docs back to the user. Just confirm you've loaded everything and show the dynamic state summary.
