# Compression Ratio Optimization — Agent Instructions

## Mission

You are optimizing a lossless compression pipeline for telegraf JSONL telemetry data. The pipeline uses Meta's OpenZL framework with a custom three-stage approach (structural decomposition → trained compression → container packaging). It was benchmarked at **42x compression** on large aggregated files (184 MB), but in production it achieves only **2.9x** on real per-VM batches (~123 KB). Meanwhile, generic **zstd achieves 12.7x** on the same data with zero training. Your job is to close this gap — make OpenZL match or beat zstd on production-size data.

## Phase 1: Orientation (Do This First)

Read these files in order. Do not write any code until you have read all of them.

### Project context
```
~/work/nom-config-hub/head/CLAUDE.md
```
This explains the product (DNS firewall), the telemetry pipeline (Telegraf → Kafka → InfluxDB), why we built compression, and how OpenZL works.

### Compression codebase
```
~/personal/compression/
```
Read ALL of these:
- `ANALYSIS.md` — Pipeline analysis, benchmark results (if present)
- `HANDOFF.md` — Architecture, file inventory, OpenZL internals, known limitations (if present)
- `telemetry_service.py` — Top-level `compress()` / `decompress()` API, orchestrates the three stages
- `telemetry_codec.py` — Structural decomposition: JSONL → type-grouped TSVs, `__ckeys__` optimization, reconstruction
- `zljsonl.py` — `.zljsonl` container format (magic, directory, CRC32)
- `_telemetry_scanner.c` — C extension for fast JSON scanning with exact numeric preservation
- `models/lossless_telemetry/telemetry_schema.json` — Schema for 35 telegraf metric types
- `telemetry_sidecar.py` — The sidecar daemon (focus on `_flush_batch()` and `--compare-zstd`)
- `setup.py` — Build config for C extension

### Prior experiments
The compression repository has multiple branches with prior experiments and techniques. Explore them:
```bash
cd ~/personal/compression
git branch -a
```
Check out different branches to see what approaches were tried, what schemas were tested, and what results were achieved. This history is critical — do not repeat failed experiments.

### Investigation context
```
~/personal/compression/docs/compression-ratio-investigation-handoff.md
```
This explains what we deployed, the A/B test setup, observed ratios, hypotheses, and all context from the deployment sessions.

### OpenZL and SDDL
Look in the `nyx/` directory (may be on `main` branch) for:
- SDDL schema definitions (`.sddl` files)
- OpenZL build scripts
- Training scripts and configurations
- C++ codec implementations

Understand how `zli train` works, what SDDL schemas control, and how models are generated.

## Phase 2: Data Analysis (Wait for Sample Files)

Production telemetry JSONL files are being captured on live VMs at `/tmp/capture/` on each node. The user will provide these files at `~/Desktop/telemetry-samples/` (or tell you where they are). Once available:

1. **Profile the data**: Type distribution, records per type, field cardinality, record sizes, timestamp patterns
2. **Compare with training data**: How does the production per-VM distribution differ from what the model was trained on?
3. **Measure overhead**: Compress a 123 KB sample, then inspect the `.zljsonl` container — how much is overhead (magic, directory, manifest, schema, per-type-file headers) vs actual compressed payload?
4. **Test batch size sensitivity**: Concatenate samples to 500 KB, 1 MB, 5 MB, 10 MB, 50 MB. Plot OpenZL ratio and zstd ratio vs batch size. Find the crossover point.

## Phase 3: Iterative Optimization (Use Ralph Loop)

Once you understand the data and have baseline measurements, use the Ralph Loop plugin to systematically try improvements.

### How to use Ralph Loop

Ralph Loop runs your prompt iteratively. Each iteration, Claude re-reads its own prior work (git history, tasks/lessons.md, tasks/todo.md) and picks the next experiment. It stops when the completion condition is met or max iterations is reached.

### Before launching Ralph Loop

Create the scaffolding files:

**`~/personal/compression/tasks/todo.md`** — Experiment checklist (start with the hypotheses from the handoff doc, add more as you learn)

**`~/personal/compression/tasks/lessons.md`** — Running log of what you tried and what happened. Format:
```
## Experiment N: <title>
- **What**: <what you changed>
- **Result**: OpenZL Xx, zstd Yx on Z KB input
- **Lesson**: <what this tells us>
```

### Launch command

```
/ralph-loop "Read ~/personal/compression/docs/compression-ratio-investigation-handoff.md for full context. Read tasks/todo.md and tasks/lessons.md for prior experiments.

Goal: Improve OpenZL compression ratio on ~/Desktop/telemetry-samples/*.jsonl to match or beat zstd (baseline: 12.7x). Current OpenZL: 2.9x on ~123KB batches.

Each iteration:
1. Read tasks/lessons.md — what has been tried, what worked, what failed
2. Read tasks/todo.md — pick the next unchecked experiment
3. Implement ONE change (schema, preprocessing, training, batch strategy)
4. Test: compress sample data, measure ratio, compare with zstd
5. Verify bit-exact reconstruction: decompress(compress(data)) == data
6. Git commit the change with results in the commit message
7. Update tasks/lessons.md with the experiment and result
8. Mark the item complete in tasks/todo.md, add new hypotheses if any
9. If OpenZL ratio >= 12x on 123KB batches, output DONE

Rules:
- ONE experiment per iteration — isolate variables
- Always measure both OpenZL AND zstd on the same input
- Never break bit-exact reconstruction
- Only modify compression pipeline (telemetry_service.py, telemetry_codec.py, schemas, models, nyx/)
- Check git branches for prior experiments before trying something new
- If an approach is clearly a dead end, skip to the next hypothesis" --max-iterations 25 --completion-promise "DONE"
```

### What Ralph Loop will do

Each cycle:
```
Read prior work → Pick experiment → Modify code → Test → Record results → Commit → Repeat
```

It preserves git history between iterations, so you can see the full progression of experiments and revert any that made things worse.

### Cancelling

If the loop goes off track or you want to redirect it:
```
/cancel-ralph
```
Then review `tasks/lessons.md` to see what was tried, adjust the prompt, and relaunch.

## Key Technical Details

### The three-stage pipeline (what you're optimizing)
1. **Structural decomposition** (`telemetry_codec.py`): JSONL → group by `type` field → type-grouped TSV files + manifest + schema
2. **Trained compression** (`telemetry_service.py` → `zli compress`): Each TSV compressed with trained `.zl_compressor` model; manifest/schema with generic `serial` profile
3. **Container** (`zljsonl.py`): Bundle all compressed parts into `.zljsonl` (magic + directory + blobs + CRC32)

### What you can modify
- The SDDL schema (controls how OpenZL models are trained)
- The preprocessing in `telemetry_codec.py` (how JSONL is decomposed into TSVs)
- The training data (what data the model is trained on)
- The compression strategy (which profiles are used, how parts are grouped)
- The batch strategy (recommendations for optimal batch size/time)

### What you must NOT modify
- `telemetry_sidecar.py` (the daemon)
- Deployment code (Ansible playbooks, systemd templates)
- The `.zljsonl` container format (must remain compatible with existing decompressor)

### Testing a change
```bash
cd ~/personal/compression

# Compress with OpenZL
python3 telemetry_service.py compress sample.jsonl output.zljsonl

# Verify bit-exact
python3 telemetry_service.py decompress output.zljsonl reconstructed.jsonl
diff sample.jsonl reconstructed.jsonl  # MUST produce no output

# Compare with zstd
python3 -c "
import zstandard, pathlib
data = pathlib.Path('sample.jsonl').read_bytes()
compressed = zstandard.compress(data)
ratio_zstd = len(data) / len(compressed)
openzl = pathlib.Path('output.zljsonl').read_bytes()
ratio_openzl = len(data) / len(openzl)
print(f'Input:  {len(data):>10,} bytes')
print(f'OpenZL: {len(openzl):>10,} bytes ({ratio_openzl:.1f}x)')
print(f'zstd:   {len(compressed):>10,} bytes ({ratio_zstd:.1f}x)')
print(f'Winner: {\"OpenZL\" if ratio_openzl > ratio_zstd else \"zstd\"} by {abs(ratio_openzl - ratio_zstd):.1f}x')
"
```

## Workflow Rules

Follow these strictly:

- **Plan First**: Enter plan mode for any non-trivial task. Write specs before code.
- **One Thing Per Iteration**: Each Ralph Loop cycle tests ONE hypothesis. Isolate variables.
- **Subagents**: Use subagents for research and parallel analysis. Keep main context clean.
- **Self-Improvement**: After every correction, update `tasks/lessons.md`. Write rules that prevent repeating mistakes.
- **Verification Before Done**: Never claim success without proving it — show the numbers, show the diff.
- **Simplicity First**: Prefer simple changes. The best fix might be "increase batch time to 5 minutes" not "redesign the entire pipeline."
- **Don't Assume, Ask**: If unsure about something, ask the user. Don't guess.
