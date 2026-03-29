# Agent Prompt: Investigate and Improve OpenZL Compression Ratios

## Your Task

Our OpenZL compression pipeline achieves only **2.9x** compression on production telemetry data, while generic **zstd achieves 12.7x** on the same data. The original benchmark showed 42x on large files. Your job is to figure out why and fix it.

## Before You Start

1. Read **`~/work/nom-config-hub/head/CLAUDE.md`** — full project context, telemetry pipeline, OpenZL details
2. Read **everything** in **`~/personal/compression/`**:
   - `ANALYSIS.md` and `HANDOFF.md` (if present) — architecture docs
   - `telemetry_service.py` — the three-stage compression pipeline
   - `telemetry_codec.py` — structural decomposition (JSONL → type-grouped TSVs)
   - `zljsonl.py` — container format
   - `models/lossless_telemetry/telemetry_schema.json` — the 35-type schema
   - `telemetry_sidecar.py` — the sidecar with `--compare-zstd` and `--capture-dir`
3. Read **`~/personal/compression/docs/compression-ratio-investigation-handoff.md`** — full context on what we deployed, what we observed, and all hypotheses

## Sample Data

Raw uncompressed JSONL captured from production VMs is at `/tmp/capture/` on each node. Fetch to your local machine:
```bash
scp -F ~/work/sst-template-migration/head/SB_load_192_dest/run/194/terraform/ssh.config \
  centos@dc1-nsm0:/tmp/capture/*.jsonl ~/Desktop/telemetry-samples/
```

Collect from multiple nodes for diversity (different services produce different telemetry types).

## What To Do

1. **Analyze the data** — understand what's in the captured JSONL files (type distribution, field patterns, record sizes)
2. **Measure the overhead** — how much of OpenZL's output is container/format overhead vs compressed data?
3. **Test batch size sensitivity** — concatenate files, test at 1 MB, 5 MB, 50 MB
4. **Experiment** — try alternative SDDL schemas, preprocessing strategies, training data
5. **Iterate** — use subagents for parallel experiments, track everything in tasks/todo.md

## Success Criteria

- Achieve compression ratio that **matches or beats zstd** on production-size batches (~123 KB)
- OR demonstrate a batch size threshold where OpenZL wins, with a recommendation for the optimal batch window
- Maintain **bit-exact reconstruction** — `decompress(compress(data)) == data`
- Document all findings and the recommended production configuration

## Constraints

- Do NOT modify telemetry_sidecar.py or deployment code
- Only modify compression pipeline code (telemetry_service.py, telemetry_codec.py, schemas, models)
- Always test against captured production data
- Always compare against zstd as baseline
