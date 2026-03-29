# Compression Ratio Investigation — Handoff Document

## Background: What We Built and What We Observed

### The System

We built a **telemetry compression sidecar** that sits between Telegraf and Kafka on each VM in a DNS firewall product. The architecture:

```
Telegraf (collects metrics every 5s)
    → Unix socket
    → Compression Sidecar (buffers 60s of JSONL, compresses, sends to Kafka)
    → Kafka topic
```

The sidecar uses a **three-stage OpenZL compression pipeline**:
1. **Structural decomposition**: Parse JSONL, group records by `type` field (cpu, mem, disk, kernel, etc.), convert to type-grouped TSV files
2. **Trained OpenZL compression**: Each TSV compressed with a pre-trained `.zl_compressor` model
3. **Container packaging**: All parts bundled into `.zljsonl` binary container

### The Problem: Bad Compression Ratios

**Original benchmark** (on large aggregated files):
- 184 MB JSONL → ~4.4 MB compressed = **42x ratio**
- Tested across 2.6 GB of real production data

**What we're seeing in production (per-VM, 60-second batches)**:
- ~123 KB JSONL → ~43 KB compressed = **2.9x ratio** (OpenZL)
- Same ~123 KB JSONL → ~9.6 KB compressed = **12.7x ratio** (zstd, out of the box)

**zstd is beating OpenZL by 4x on real production data.** This is the opposite of what we expected. The trained OpenZL model was supposed to outperform generic compressors.

### Why We Think This Is Happening

Several hypotheses to investigate:

1. **Batch size is too small**: The 42x benchmark used 184 MB files. Production batches are only ~123 KB (60 seconds of telemetry from one VM). OpenZL's trained model may need much larger inputs to amortize its overhead and exploit cross-record patterns. The container format itself (magic bytes, directory, CRC32, per-type TSV overhead) adds fixed overhead that dominates at small sizes.

2. **Schema mismatch**: The trained model was trained on aggregated telemetry from many VMs and services. A single VM's 60-second window has a narrow type distribution (maybe only cpu, mem, disk, kernel — not all 35 types). The schema's `__ckeys__` optimization and type grouping may not help when there are few types.

3. **Structural decomposition overhead**: For small batches, converting JSONL → type-grouped TSVs → compressed TSVs → container may add more overhead than it removes. With only ~123 KB of input, the manifest, schema, and per-type file overhead could be significant relative to the data.

4. **SDDL schema may need rethinking**: The OpenZL SDDL schema was designed for the aggregated data distribution. It may need to be redesigned for the per-VM, per-interval data distribution we see in production.

5. **Preprocessing may need rethinking**: Maybe type grouping isn't the right structural decomposition for small batches. Alternative approaches: timestamp delta encoding, field-level dictionary compression, or simply treating the entire batch as a single stream.

### What We're Collecting

We deployed a `--capture-dir /tmp/capture` flag on all 37 DC1 sidecar nodes. Each node is writing raw uncompressed JSONL to rolling 1 MB files at `/tmp/capture/capture_NNNN.jsonl`. These files contain the exact data that the sidecar receives from Telegraf before any compression.

**To fetch these files for analysis:**
```bash
# From your Mac, for any node:
scp -F <terraform-dir>/ssh.config centos@<node>:/tmp/capture/*.jsonl ~/Desktop/telemetry-samples/
```

**Nodes collecting data** (environment 194, all DC1 non-Kafka nodes):
dc1-vkms0/1/2, dc1-kvs0/1/2, dc1-vertica0-8, dc1-pgaas0/1/2, dc1-influx0, dc1-levant0/1, dc1-objectindex0/1, dc1-streamproc0/1, dc1-nsm0, dc1-link0/1, dc1-iptracker0, dc1-apps0, dc1-ssm0/1, dc1-sportal0/1, dc1-listmanager0, dc1-cas0/1

**SSH access** (terraform directory for this environment):
```
/Users/prousogl/work/sst-template-migration/head/SB_load_192_dest/run/194/terraform
```

## Files You Must Read Before Starting

### 1. Project Context
- **`~/work/nom-config-hub/head/CLAUDE.md`** — Full project context: what the product is, the telemetry pipeline architecture, the compression POC goals, OpenZL details, the three-stage pipeline, constraints

### 2. Compression Codebase
Read ALL of these in `~/personal/compression/`:
- **`ANALYSIS.md`** — Detailed pipeline analysis and compression results (if present)
- **`HANDOFF.md`** — Architecture, file inventory, OpenZL details, known limitations (if present)
- **`telemetry_service.py`** — Top-level `compress()` / `decompress()` API. This is where the three-stage pipeline is orchestrated.
- **`telemetry_codec.py`** — JSONL ↔ TSV encode/decode. This is the structural decomposition stage. Understand how it groups by `type`, creates TSVs, handles the `__ckeys__` optimization, and reconstructs.
- **`zljsonl.py`** — The `.zljsonl` container format (magic bytes, directory, CRC32). Understand the overhead this adds per container.
- **`_telemetry_scanner.c`** — C extension for fast JSON scanning with exact numeric preservation. Not likely the issue but understand what it does.
- **`telemetry_sidecar.py`** — The sidecar daemon. Focus on the `_flush_batch()` function to understand how batches are compressed and the `--compare-zstd` comparison.
- **`models/lossless_telemetry/telemetry_schema.json`** — The schema definition for 35 telegraf metric types. Understand what types exist and what fields each type has.
- **`models/lossless_telemetry/telemetry_csv.zl_compressor`** — The trained OpenZL model (binary, not human-readable, but understand what it was trained on)
- **`DEPLOY.md`** — Deployment guide with all the steps we took
- **`docs/compression-ratio-investigation-handoff.md`** — This file

### 3. OpenZL Build and SDDL
On the `main` branch (or in `nyx/` directory):
- **`nyx/`** directory — Contains OpenZL build scripts, SDDL schemas, C++ codecs
- Look for any `.sddl` files or schema definitions that define how OpenZL models are trained
- `nyx/scripts/build.sh` — How zli is built
- Understand the `zli train` command and how models are created

## The A/B Test Setup

We deployed two identical environments:

| | Environment A (194) | Environment B (1940) |
|---|---|---|
| Telegraf output | Unix socket → Sidecar → Kafka | Direct Kafka output |
| Kafka topic | `nom-telemetry-poc-compressed` | `nom-telemetry-poc-raw` |
| Compression | OpenZL (3-stage pipeline) | None |
| zstd comparison | Logged per batch (not sent to Kafka) | N/A |

**Observed compression ratios on ~123 KB batches (60s, single VM):**

| Compressor | Input | Output | Ratio |
|---|---|---|---|
| OpenZL (trained model) | 123 KB | ~43 KB | **2.9x** |
| zstd (generic, no training) | 123 KB | ~9.6 KB | **12.7x** |
| OpenZL (benchmark, 184 MB) | 184 MB | 4.4 MB | **42x** |

## Your Task

**Goal**: Figure out why OpenZL is performing poorly on production telemetry batches and how to improve the compression ratio to at least match or beat zstd.

### Investigation Areas

1. **Analyze the captured JSONL files**: Understand the actual data distribution. What types appear? How many records per type? What does the field distribution look like? How does a single VM's 60-second window differ from the aggregated 184 MB benchmark data?

2. **Measure overhead**: For a ~123 KB batch, how much of the 43 KB compressed output is overhead (container format, per-type file headers, manifest, schema) vs actual compressed data? What's the overhead ratio?

3. **Test batch size sensitivity**: Concatenate multiple captured files to simulate larger batches (1 MB, 5 MB, 10 MB, 50 MB). Does OpenZL's ratio improve? At what batch size does it match/beat zstd?

4. **Evaluate the SDDL schema**: Is the schema appropriate for this data? Are there types in the schema that never appear in production? Are there production types not in the schema? Does the `__ckeys__` optimization help or hurt at small batch sizes?

5. **Test alternative preprocessing**: What if we skip type grouping and compress the raw JSONL directly with OpenZL's serial profile? What if we use a different structural decomposition?

6. **Retrain the model**: What happens if we train a new model on the actual per-VM batch data instead of the aggregated 184 MB files? Does a model trained on 123 KB batches perform better?

7. **Understand zstd's advantage**: Why does zstd do so well? Is it because it handles small inputs better? Is it because JSONL has enough local redundancy that generic LZ-based compression captures most of the value without structural decomposition?

### Key Questions to Answer

- At what batch size does OpenZL start outperforming zstd?
- Is the structural decomposition (JSONL → type-grouped TSVs) helping or hurting at small batch sizes?
- Can we redesign the pipeline to work better with small batches while still benefiting from OpenZL's trained models?
- Is there a hybrid approach (e.g., use zstd for small batches, OpenZL for large) that gives us the best of both?
- Should we increase the sidecar's batch window (e.g., 5 minutes instead of 60 seconds) to get larger batches?

### How to Test Changes

The compression repo at `~/personal/compression/` has everything you need:

```bash
cd ~/personal/compression

# Compress a sample file with OpenZL
python3 telemetry_service.py compress sample.jsonl output.zljsonl

# Decompress and verify bit-exact
python3 telemetry_service.py decompress output.zljsonl reconstructed.jsonl
diff sample.jsonl reconstructed.jsonl

# Compare with zstd
python3 -c "
import zstandard, pathlib
data = pathlib.Path('sample.jsonl').read_bytes()
compressed = zstandard.compress(data)
print(f'Input: {len(data):,} bytes')
print(f'zstd:  {len(compressed):,} bytes ({len(data)/len(compressed):.1f}x)')
"

# Train a new OpenZL model (if you modify the schema or preprocessing)
# See ANALYSIS.md or HANDOFF.md for training instructions
```

### Iterative Workflow Recommendation

This task requires many iterations of:
1. Hypothesize why compression is poor
2. Modify the schema/preprocessing/training data
3. Retrain the model
4. Test on captured production data
5. Compare ratios
6. Repeat

**Use subagents aggressively** — each hypothesis should be explored in a separate subagent to keep the main context clean. Use one subagent per experiment:
- Subagent 1: Analyze data distribution in captured files
- Subagent 2: Measure container/overhead ratio
- Subagent 3: Test batch size sensitivity
- Subagent 4: Test alternative SDDL schemas
- Subagent 5: Test skipping type grouping
- etc.

Track all experiments and results in `tasks/todo.md` and capture lessons in `tasks/lessons.md`.

## Workflow Rules

### 1. Plan Node Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately - don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One tack per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update tasks/lessons.md with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes - don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests - then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

## Task Management
- **Plan First**: Write plan to tasks/todo.md with checkable items
- **Verify Plan**: Check in before starting implementation
- **Track Progress**: Mark items complete as you go
- **Explain Changes**: High-level summary at each step
- **Document Results**: Add review section to tasks/todo.md
- **Capture Lessons**: Update tasks/lessons.md after corrections

## Core Principles
- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.

## Critical Constraints
- **Bit-exact reconstruction is non-negotiable.** Any new compression approach MUST produce byte-identical output when decompressed: `decompress(compress(data)) == data`
- **Do NOT modify the sidecar or deployment code** — only modify the compression pipeline (telemetry_service.py, telemetry_codec.py, schemas, models)
- **Test every change** against the captured production data files
- **Always compare against zstd** as the baseline — the goal is to beat it
