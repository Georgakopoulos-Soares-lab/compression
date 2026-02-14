# OpenEvolve: OpenZL Training Hyperparameter Optimization

Automated hyperparameter search for Meta's OpenZL compression framework using
[OpenEvolve](https://github.com/codelion/openevolve), an LLM-guided evolutionary
coding agent.

## What This Does

OpenEvolve uses an LLM (OpenAI's gpt-4o-mini) as a "mutation engine" to
iteratively improve a Python function that returns OpenZL training hyperparameters.
Each candidate configuration is evaluated by actually training a compressor and
measuring the compression ratio on held-out genomic data.

The search maintains a quality-diversity archive (MAP-Elites) across two feature
dimensions — **compression ratio** and **training time** — so you get a Pareto
frontier of configurations ranging from fast-but-decent to slow-but-optimal.

### Hyperparameters Being Tuned

| Parameter | Type | Range | Description |
|-----------|------|-------|-------------|
| `trainer` | str | `greedy`, `full-split`, `bottom-up` | Training algorithm |
| `max_time_secs` | int | 10–3600 | Training time budget (seconds) |
| `no_ace_successors` | bool | True/False | Disable ACE successor models |
| `no_clustering` | bool | True/False | Skip clustering phase |
| `target_train_mib` | int | 50, 100, 200 | Training data size (MiB) |
| `threads` | int | 1–64 | CPU threads for training |
| `compress_jobs` | int | 1–32 | Parallel compression jobs |

## Prerequisites

- **Python >= 3.10** (required by OpenEvolve)
- **nyx** built and working (`nyx build` completed successfully)
- **OpenAI API key** saved to `api_key.txt` in the repository root
- **~5 GB disk space** for genome data and preprocessed chunks
- **Internet access** (for downloading the genome, one-time)

## Step-by-Step Setup

### 1. Ensure nyx is built

```bash
# From the repository root
cd nyx
source .venv/bin/activate
nyx build
```

Verify that both binaries exist:

```bash
ls -la nyx/openzl/zli nyx/bin/genomic_preprocessor
```

### 2. Install OpenEvolve into the nyx venv

Both nyx and OpenEvolve require Python >= 3.10. They share the same venv at
`nyx/.venv`:

```bash
source nyx/.venv/bin/activate
pip install -r nyx/evolve/requirements.txt
```

Verify installation:

```bash
python3 -c "import openevolve; print('OpenEvolve OK')"
```

Note: `run.sh` automatically activates `nyx/.venv`, so you don't need to
activate it manually before running the evolution.

### 3. Save your OpenAI API key

Create `api_key.txt` in the repository root:

```bash
echo "sk-your-key-here" > api_key.txt
```

**IMPORTANT:** This file is gitignored and must NEVER be committed. Verify:

```bash
grep api_key.txt .gitignore
```

### 4. Prepare evaluation data

This downloads the GRCm39 mouse genome (~800 MB compressed), creates training
samples at three sizes (50/100/200 MiB), creates a held-out test set, and
preprocesses everything into FAV4 binary chunks:

```bash
bash nyx/evolve/prepare_data.sh
```

This takes 5–15 minutes depending on network speed and CPU. The output goes to
`evolve_data/` (gitignored). You only need to run this once.

Expected output structure:

```
evolve_data/
  train_50MiB.fasta         # 50 MiB FASTA training sample
  train_50MiB/              # Preprocessed FAV4 chunks
    chunk_000.fasta_packed.bin
  train_100MiB.fasta
  train_100MiB/
    chunk_000.fasta_packed.bin
  train_200MiB.fasta
  train_200MiB/
    chunk_000.fasta_packed.bin
    ...
  test_200MiB.fasta          # Held-out test FASTA
  test_chunks/               # Preprocessed test chunks
    chunk_000.fasta_packed.bin
    ...
```

### 5. Run the evolution

```bash
bash nyx/evolve/run.sh
```

This launches OpenEvolve with 50 iterations (the default). Each iteration:

1. The LLM mutates `get_training_config()` in `initial_program.py`
2. Stage 1 validates the config (milliseconds)
3. Stage 2 trains a compressor + compresses test data (1–5 minutes)
4. Results are stored in the MAP-Elites archive

**Customizing the run:**

```bash
# Run 100 iterations instead of 50
bash nyx/evolve/run.sh --iterations 100

# Resume from a checkpoint
bash nyx/evolve/run.sh --checkpoint nyx/evolve/openevolve_output/checkpoints/checkpoint_25
```

## How to Interpret Results

### Output directory

Results are saved to `nyx/evolve/openevolve_output/`:

```
openevolve_output/
  best_program.py              # Best overall configuration found
  checkpoints/
    checkpoint_5/              # Periodic snapshots (every 5 iterations)
    checkpoint_10/
    ...
  evolution_log.json           # Full iteration history
```

### Understanding scores

The primary metric is **compression_ratio** (= original_text_bytes /
compressed_bytes). This measures the end-to-end ratio from the raw text FASTA
file to the final `.zl` compressed output — directly comparable to what
`nyx compress` reports. Higher is better.

For reference on GRCm39 FASTA data:

| Compressor | Typical Ratio |
|------------|---------------|
| gzip -1    | ~2.8x         |
| zstd -9    | ~3.0x         |
| nyx (default config) | ~4.0–4.5x |

The MAP-Elites archive organizes results along two dimensions:

- **compression_ratio**: How well the compressor packs the data
- **training_time**: How long training took (seconds)

This gives you a set of Pareto-optimal configurations. A "fast" config (e.g.,
`full-split` with 60s time budget) might achieve 3.5x, while a "thorough" config
(e.g., `greedy` with 900s) might reach 5.0x.

### Reading the best configuration

After the run completes, check the best program:

```bash
cat nyx/evolve/openevolve_output/best_program.py
```

This will contain a `get_training_config()` function with the optimal values.
You can use these values directly with `nyx compress`:

```bash
# Example: apply discovered optimal settings
nyx compress data/your_file.fasta \
  --trainer greedy \
  --max-time-secs 600 \
  --threads 10 \
  --target-train-mib 200 \
  --no-clustering  # if the best config says no_clustering: True
```

## Cost Estimate

Using **gpt-4o-mini** via OpenAI's API:

| Iterations | Estimated LLM Cost | Wall-Clock Time |
|------------|-------------------|-----------------|
| 25         | ~$0.03            | 1–2 hours       |
| 50         | ~$0.05            | 2–4 hours       |
| 100        | ~$0.10            | 4–8 hours       |

The LLM cost is negligible. The real cost is CPU time for training.

## How to Extend

### Adding new hyperparameters

1. Add the parameter to `get_training_config()` in `initial_program.py`
2. Add validation for it in `evaluator.py` (`_validate_config()`)
3. Wire it into the `zli train` command in `evaluator.py` (the `train_args` list)
4. Document the parameter in the system message in `config.yaml`

### Changing the fitness function

Edit `evaluator.py`. The `combined_score` is currently just `compression_ratio`.
You could weight it differently, for example:

```python
# Penalize configs that take too long
time_penalty = max(0, 1.0 - train_elapsed / 1800)
combined_score = compression_ratio * (0.8 + 0.2 * time_penalty)
```

### Using a different LLM

Edit `config.yaml`:

```yaml
# For OpenAI GPT-4o (more capable, ~10x more expensive)
llm:
  api_base: "https://api.openai.com/v1"
  primary_model: "gpt-4o"

# For Google Gemini (requires Gemini API key in OPENAI_API_KEY)
llm:
  api_base: "https://generativelanguage.googleapis.com/v1beta/openai/"
  primary_model: "gemini-2.5-flash"

# For a local model via Ollama
llm:
  api_base: "http://localhost:11434/v1"
  primary_model: "codellama:7b"
```

### Using a different dataset

1. Place your FASTA file in `data/`
2. Edit `prepare_data.sh` to point `GENOME` at your file
3. Re-run `bash nyx/evolve/prepare_data.sh`
4. Run the evolution as normal

## File Reference

| File | Purpose |
|------|---------|
| `initial_program.py` | Seed configuration — OpenEvolve mutates the `EVOLVE-BLOCK` |
| `evaluator.py` | Fitness function: validate config, train, compress, measure |
| `config.yaml` | OpenEvolve settings: LLM, population, system message |
| `run.sh` | Convenience launcher (loads API key, runs checks) |
| `prepare_data.sh` | One-time data prep (download, sample, preprocess) |
| `requirements.txt` | Python dependencies (openevolve) |

## Troubleshooting

### "zli not found"

Run `nyx build` from the nyx directory. Ensure the venv is active.

### "Training data not found"

Run `bash nyx/evolve/prepare_data.sh`. Check that `evolve_data/` exists.

### "API key not found"

Create `api_key.txt` in the repo root with your OpenAI key. Or export
`OPENAI_API_KEY` in your shell before running.

### "openevolve not installed"

Run `pip install -r nyx/evolve/requirements.txt` with the nyx venv active.

### Evolution seems stuck at low scores

- Check `openevolve_output/` for error artifacts — they explain what went wrong
- Ensure `evolve_data/` has actual chunk files (not empty directories)
- Try increasing `max_iterations` or `max_time_secs` in the initial config
