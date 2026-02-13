# Changelog Processing Reference

This document contains detailed instructions for processing developer change notes from `docs/CHANGES.md` into structured entries in `docs/PROJECT_HISTORY.md`.

## Step 1: Read docs/CHANGES.md

Read `docs/CHANGES.md` from the repository root. Look at the content **below** the `<!-- Write your changes below this line -->` marker.

If there is no content below the marker (empty or only whitespace), skip all remaining steps in this reference and report "No change notes to process" in your final output.

## Step 2: Read existing docs/PROJECT_HISTORY.md

Read `docs/PROJECT_HISTORY.md` from the repository root so you know the existing format and can append consistently.

## Step 3: Correlate notes with git history

The developer's notes in docs/CHANGES.md are informal. Cross-reference them with recent git history to add precision:
```bash
git log --oneline -20
```

For each note the developer wrote, try to identify the corresponding commit(s). This helps you:
- Add exact dates to entries
- Identify files that were actually changed
- Fill in details the developer may have omitted

## Step 4: Create structured entries

For each change note (or group of related notes), create a structured entry in the following format:
```markdown
## YYYY-MM-DD — Brief Title

**What changed:** One paragraph describing the change in clear, specific language. Reference actual file names and function names where relevant.

**Why:** One sentence on the motivation or context, drawn from the developer's notes.

**Files affected:** Bulleted list of files that were added, modified, or deleted.

**Commits:** List of relevant commit hashes (short form) with their messages.
```

Rules for creating entries:
- Use the date from the most recent related commit. If no commit matches, use today's date.
- Group related notes into a single entry if they describe parts of the same change.
- Keep separate notes as separate entries if they describe unrelated changes.
- Preserve the developer's intent and context — do not lose information from their notes.
- Add technical precision from the git history and file analysis, but don't contradict what the developer wrote.

## Step 5: Append to docs/PROJECT_HISTORY.md

Append the new entries to `docs/PROJECT_HISTORY.md`, placing them at the **end** of the file. Newest entries go at the bottom so the file reads chronologically from top to bottom.

Do not modify or rewrite existing entries in docs/PROJECT_HISTORY.md.

## Step 6: Clear docs/CHANGES.md

After successfully appending entries to docs/PROJECT_HISTORY.md, reset `docs/CHANGES.md` to its clean state. Write the following content (this preserves the instructions for the developer):
```markdown
# Changes

Write your notes about recent changes here. Be as brief or detailed as you want — the `/update-context` skill will process these into structured entries.

When you run `/update-context`, these notes will be:
1. Read and processed into structured changelog entries
2. Appended to `docs/PROJECT_HISTORY.md`
3. This file will be cleared so it's ready for your next round of notes

## Format

No required format. Just write what you did. Examples:

- "Added gzip baseline comparison to benchmarks"
- "Refactored the compression pipeline to support streaming input. Had to change how OpenZL buffers are allocated because the old approach loaded the entire file into memory."
- "Fixed bug where FASTA files with multiple sequences weren't being handled correctly"

---

<!-- Write your changes below this line -->
```

Do NOT clear docs/CHANGES.md if the append to docs/PROJECT_HISTORY.md failed for any reason.
