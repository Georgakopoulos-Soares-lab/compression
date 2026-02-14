"""
Evaluator for OpenZL training hyperparameter optimization.

This evaluator is called by OpenEvolve for each candidate program. It:
  1. Imports the evolved get_training_config() function
  2. Validates the returned config (Stage 1 — milliseconds)
  3. Runs zli train + compress on pre-prepared data (Stage 2 — minutes)
  4. Returns compression ratio and training time as metrics

Prerequisites:
  - nyx is built (zli and genomic_preprocessor available)
  - evolve_data/ prepared via prepare_data.sh
  - SDDL schema at nyx/schemas/fasta_packed.sddl
"""

import importlib.util
import os
import shutil
import subprocess
import tempfile
import time
import traceback
from pathlib import Path

from openevolve.evaluation_result import EvaluationResult

# ── Path resolution ──────────────────────────────────────────────────────────
# This file lives at nyx/evolve/evaluator.py
_EVOLVE_DIR = Path(__file__).resolve().parent
_NYX_ROOT = _EVOLVE_DIR.parent
_REPO_ROOT = _NYX_ROOT.parent
_EVOLVE_DATA = _REPO_ROOT / "evolve_data"
_FULL_GENOME_CHUNKS = _EVOLVE_DATA / "full_genome_chunks"
_ORIGINAL_SIZE_FILE = _FULL_GENOME_CHUNKS / "original_genome_bytes.txt"
_SCHEMA = _NYX_ROOT / "schemas" / "fasta_packed.sddl"

# Locate zli binary
_ZLI = _NYX_ROOT / "openzl" / "zli"
if not _ZLI.is_file():
    # Fallback: check PATH
    _zli_on_path = shutil.which("zli")
    if _zli_on_path:
        _ZLI = Path(_zli_on_path)

# Valid values for discrete hyperparameters
VALID_TRAINERS = {"greedy", "full-split", "bottom-up"}
VALID_TRAIN_SIZES = {50, 100, 200}


def _validate_config(config: dict) -> list[str]:
    """Validate a training config dict. Returns a list of error strings (empty = valid)."""
    errors = []

    if not isinstance(config, dict):
        return [f"get_training_config() must return a dict, got {type(config).__name__}"]

    # Required keys
    required = {
        "trainer": str,
        "max_time_secs": (int, float),
        "no_ace_successors": bool,
        "no_clustering": bool,
        "target_train_mib": (int, float),
        "threads": (int, float),
        "compress_jobs": (int, float),
    }

    for key, expected_type in required.items():
        if key not in config:
            errors.append(f"Missing required key: '{key}'")
            continue
        if not isinstance(config[key], expected_type):
            errors.append(
                f"'{key}' must be {expected_type}, got {type(config[key]).__name__}"
            )

    if errors:
        return errors

    # Value constraints
    if config["trainer"] not in VALID_TRAINERS:
        errors.append(
            f"trainer must be one of {VALID_TRAINERS}, got '{config['trainer']}'"
        )

    max_time = int(config["max_time_secs"])
    if max_time < 10 or max_time > 3600:
        errors.append(f"max_time_secs must be 10–3600, got {max_time}")

    target = int(config["target_train_mib"])
    if target not in VALID_TRAIN_SIZES:
        errors.append(
            f"target_train_mib must be one of {VALID_TRAIN_SIZES}, got {target}"
        )

    threads = int(config["threads"])
    if threads < 1 or threads > 64:
        errors.append(f"threads must be 1–64, got {threads}")

    cj = int(config["compress_jobs"])
    if cj < 1 or cj > 32:
        errors.append(f"compress_jobs must be 1–32, got {cj}")

    return errors


def _run_zli(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    """Run zli with the given arguments. Raises on failure."""
    cmd = [str(_ZLI)] + args
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def _total_file_bytes(directory: Path, pattern: str = "*.fasta_packed.bin") -> int:
    """Sum the byte sizes of all files matching pattern in directory."""
    return sum(f.stat().st_size for f in directory.glob(pattern))


def _total_compressed_bytes(directory: Path) -> int:
    """Sum the byte sizes of all .zl files in directory."""
    return sum(f.stat().st_size for f in directory.glob("*.zl"))


# ── Stage 1: Quick validation (milliseconds) ────────────────────────────────

def evaluate_stage1(program_path: str) -> EvaluationResult:
    """Fast validation: load program, call get_training_config(), check values."""
    try:
        spec = importlib.util.spec_from_file_location("evolved_program", program_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        if not hasattr(module, "get_training_config"):
            return EvaluationResult(
                metrics={"combined_score": 0.0, "valid": 0.0},
                artifacts={
                    "error_type": "MissingFunction",
                    "error_message": "Program must define get_training_config()",
                },
            )

        config = module.get_training_config()
        errors = _validate_config(config)

        if errors:
            return EvaluationResult(
                metrics={"combined_score": 0.0, "valid": 0.0},
                artifacts={
                    "error_type": "ValidationError",
                    "error_message": "; ".join(errors),
                    "suggestion": (
                        "Ensure get_training_config() returns a dict with keys: "
                        "trainer, max_time_secs, no_ace_successors, no_clustering, "
                        "target_train_mib, threads, compress_jobs. "
                        "trainer must be 'greedy', 'full-split', or 'bottom-up'. "
                        "target_train_mib must be 50, 100, or 200."
                    ),
                },
            )

        # Config is valid — return a passing score so cascade proceeds to stage 2
        return EvaluationResult(
            metrics={"combined_score": 1.0, "valid": 1.0},
            artifacts={
                "stage1_result": "Config validated successfully",
                "config_summary": str(config),
            },
        )

    except Exception as e:
        return EvaluationResult(
            metrics={"combined_score": 0.0, "valid": 0.0},
            artifacts={
                "error_type": type(e).__name__,
                "error_message": str(e),
                "full_traceback": traceback.format_exc(),
            },
        )


# ── Stage 2: Full evaluation (minutes) ──────────────────────────────────────

def evaluate_stage2(program_path: str) -> EvaluationResult:
    """Full evaluation: train compressor, compress held-out test data, measure ratio."""
    return evaluate(program_path)


def evaluate(program_path: str) -> EvaluationResult:
    """
    Full evaluation pipeline:
      1. Load evolved config
      2. Train a compressor on the specified training data
      3. Compress ALL full-genome chunks (matching real nyx compress pipeline)
      4. Measure compression ratio and wall-clock time
    """
    tmpdir = None
    try:
        # ── Load config ──────────────────────────────────────────────────
        spec = importlib.util.spec_from_file_location("evolved_program", program_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        if not hasattr(module, "get_training_config"):
            return EvaluationResult(
                metrics={"combined_score": 0.0},
                artifacts={"error_message": "Missing get_training_config()"},
            )

        config = module.get_training_config()
        errors = _validate_config(config)
        if errors:
            return EvaluationResult(
                metrics={"combined_score": 0.0},
                artifacts={"error_message": "; ".join(errors)},
            )

        # ── Resolve paths ────────────────────────────────────────────────
        target_mib = int(config["target_train_mib"])
        train_dir = _EVOLVE_DATA / f"train_{target_mib}MiB"

        if not train_dir.is_dir():
            return EvaluationResult(
                metrics={"combined_score": 0.0},
                artifacts={
                    "error_message": f"Training data not found: {train_dir}. Run prepare_data.sh first."
                },
            )

        if not _ZLI.is_file():
            return EvaluationResult(
                metrics={"combined_score": 0.0},
                artifacts={
                    "error_message": f"zli binary not found at {_ZLI}. Run 'nyx build' first."
                },
            )

        if not _SCHEMA.is_file():
            return EvaluationResult(
                metrics={"combined_score": 0.0},
                artifacts={"error_message": f"Schema not found: {_SCHEMA}"},
            )

        # ── Prepare temp directory ───────────────────────────────────────
        tmpdir = Path(tempfile.mkdtemp(prefix="evolve_eval_"))
        compressor_path = tmpdir / "compressor.model"

        # ── Step 1: Train ────────────────────────────────────────────────
        trainer = config["trainer"]
        max_time = int(config["max_time_secs"])
        threads = int(config["threads"])
        no_ace = config["no_ace_successors"]
        no_clust = config["no_clustering"]

        train_args = [
            "train", str(train_dir),
            "--output", str(compressor_path),
            "--profile", "sddl",
            "--profile-arg", str(_SCHEMA),
            "--threads", str(threads),
            "--max-time-secs", str(max_time),
            "--trainer", trainer,
            "--use-all-samples",
            "--force",
        ]
        if no_ace:
            train_args.append("--no-ace-successors")
        if no_clust:
            train_args.append("--no-clustering")

        train_start = time.monotonic()
        result = _run_zli(train_args, timeout=max_time + 120)
        train_elapsed = time.monotonic() - train_start

        if result.returncode != 0:
            return EvaluationResult(
                metrics={"combined_score": 0.0, "training_time": train_elapsed},
                artifacts={
                    "error_type": "TrainingFailed",
                    "error_message": f"zli train exited {result.returncode}",
                    "stderr": result.stderr[:2000],
                    "config": str(config),
                },
            )

        if not compressor_path.is_file():
            return EvaluationResult(
                metrics={"combined_score": 0.0, "training_time": train_elapsed},
                artifacts={"error_message": "Training produced no compressor file"},
            )

        # ── Step 2: Compress ALL full-genome chunks ─────────────────────
        # The real nyx pipeline trains on a ~200 MiB sample, then compresses
        # the ENTIRE genome (~2.7 GB, ~16 chunks). Different hyperparameters
        # produce compressors that generalize differently across varied data.
        if not _FULL_GENOME_CHUNKS.is_dir():
            return EvaluationResult(
                metrics={"combined_score": 0.0},
                artifacts={
                    "error_message": (
                        f"Full genome chunks not found: {_FULL_GENOME_CHUNKS}. "
                        "Run prepare_data.sh to preprocess the full genome."
                    )
                },
            )
        eval_chunks = sorted(_FULL_GENOME_CHUNKS.glob("*.fasta_packed.bin"))
        if not eval_chunks:
            return EvaluationResult(
                metrics={"combined_score": 0.0},
                artifacts={"error_message": "No genome chunks found to compress"},
            )

        # Read original text genome size (written by prepare_data.sh).
        # This makes compression_ratio = text_size / compressed_size,
        # directly comparable to "nyx compress" output.
        if not _ORIGINAL_SIZE_FILE.is_file():
            return EvaluationResult(
                metrics={"combined_score": 0.0},
                artifacts={
                    "error_message": (
                        f"Original genome size file not found: {_ORIGINAL_SIZE_FILE}. "
                        "Re-run prepare_data.sh to generate it."
                    )
                },
            )
        original_text_bytes = int(_ORIGINAL_SIZE_FILE.read_text().strip())

        compress_jobs = int(config["compress_jobs"])
        compressed_dir = tmpdir / "compressed"
        compressed_dir.mkdir()

        binary_bytes = 0
        compressed_bytes = 0
        compress_start = time.monotonic()

        for chunk in eval_chunks:
            out = compressed_dir / (chunk.name + ".zl")
            cresult = _run_zli(
                [
                    "compress", str(chunk),
                    "--compressor", str(compressor_path),
                    "--output", str(out),
                    "--force",
                ],
                timeout=300,
            )

            if cresult.returncode != 0:
                return EvaluationResult(
                    metrics={
                        "combined_score": 0.0,
                        "training_time": train_elapsed,
                    },
                    artifacts={
                        "error_type": "CompressionFailed",
                        "error_message": f"Failed to compress {chunk.name}",
                        "stderr": cresult.stderr[:2000],
                    },
                )

            binary_bytes += chunk.stat().st_size
            if out.is_file():
                compressed_bytes += out.stat().st_size

        compress_elapsed = time.monotonic() - compress_start

        # ── Step 3: Compute metrics ──────────────────────────────────────
        if compressed_bytes == 0:
            return EvaluationResult(
                metrics={"combined_score": 0.0, "training_time": train_elapsed},
                artifacts={"error_message": "All compressed files are empty"},
            )

        # Compression ratio uses original TEXT size (not binary chunk size)
        # so scores are directly comparable to "nyx compress" output.
        compression_ratio = original_text_bytes / compressed_bytes
        binary_ratio = binary_bytes / compressed_bytes
        total_time = train_elapsed + compress_elapsed

        # Primary score: compression ratio (higher is better)
        # This is what we optimize.
        combined_score = compression_ratio

        artifacts = {
            "config": str(config),
            "original_text_bytes": str(original_text_bytes),
            "binary_bytes": str(binary_bytes),
            "compressed_bytes": str(compressed_bytes),
            "compression_ratio": f"{compression_ratio:.4f}",
            "binary_ratio": f"{binary_ratio:.4f}",
            "training_time_secs": f"{train_elapsed:.1f}",
            "compress_time_secs": f"{compress_elapsed:.1f}",
            "total_time_secs": f"{total_time:.1f}",
            "num_eval_chunks": str(len(eval_chunks)),
            "compressor_size": str(compressor_path.stat().st_size),
        }

        return EvaluationResult(
            metrics={
                "combined_score": combined_score,
                "compression_ratio": compression_ratio,
                "training_time": train_elapsed,
                "total_time": total_time,
            },
            artifacts=artifacts,
        )

    except subprocess.TimeoutExpired:
        return EvaluationResult(
            metrics={"combined_score": 0.0},
            artifacts={
                "error_type": "Timeout",
                "error_message": "zli subprocess timed out",
            },
        )
    except Exception as e:
        return EvaluationResult(
            metrics={"combined_score": 0.0},
            artifacts={
                "error_type": type(e).__name__,
                "error_message": str(e),
                "full_traceback": traceback.format_exc(),
            },
        )
    finally:
        if tmpdir and tmpdir.is_dir():
            shutil.rmtree(tmpdir, ignore_errors=True)
