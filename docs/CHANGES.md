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
