"""JSON compression benchmark engine.

Generates artifacts (original JSON, TOON-like text, ASTBIN binary), compresses
them with multiple compressors, measures timing, verifies SHA-256 round-trip
integrity, and produces structured results.
"""

import csv
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .astbin import json_to_astbin_file
from .toon_convert import json_to_toon_file


@dataclass
class BenchRow:
    """One row of benchmark results."""

    artifact: str
    compressor: str
    level: str
    input_bytes: int
    compressed_bytes: int
    ratio: float
    c_time_med_s: float
    c_time_min_s: float
    c_time_max_s: float
    c_MBps_med: float
    d_time_med_s: float
    d_MBps_med: float
    verify_pass: bool
    error: str = ""


@dataclass
class BenchConfig:
    """Configuration for a benchmark run."""

    input_path: Path
    outdir: Path
    runs: int = 5
    warmup: int = 1
    keep_temp: bool = False
    force: bool = False
    zstd_level: int = 7
    gzip_level: int = 9
    pigz_level: int = 9
    nyx_sddl: str = "astbin_v1.sddl"
    nyx_cmd_template: str = (
        "nyx compress {input} -o {output} --mode train_custom"
        " --sddl {sddl} -f"
    )
    nyx_dec_template: str = "nyx decompress {input} -o {output} -f"
    verbose: bool = False


# ---------------------------------------------------------------------------
# Artifact generation
# ---------------------------------------------------------------------------

def generate_artifacts(input_path: Path, artifacts_dir: Path) -> Dict[str, Path]:
    """Produce the three benchmark artifacts and return {name: path}."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # 1. Original JSON (copy verbatim)
    json_artifact = artifacts_dir / "original.json"
    shutil.copy2(input_path, json_artifact)

    # 2. TOON-like normalized text
    toon_artifact = artifacts_dir / "normalized.toon"
    json_to_toon_file(str(input_path), str(toon_artifact))

    # 3. ASTBIN binary
    astbin_artifact = artifacts_dir / "data.astbin"
    json_to_astbin_file(str(input_path), str(astbin_artifact))

    return {
        "json": json_artifact,
        "toon": toon_artifact,
        "astbin": astbin_artifact,
    }


# ---------------------------------------------------------------------------
# Compressor definitions
# ---------------------------------------------------------------------------

@dataclass
class Compressor:
    name: str
    compress_cmd: List[str]   # {input} and {output} are placeholders
    decompress_cmd: List[str]
    artifacts: List[str]      # which artifact names this runs on
    level: str = ""
    available: bool = True
    uses_stdout: bool = True  # compress writes to stdout (redirect to output)
    _use_zli_direct: bool = False  # use internal openzl.py wrapper (serial profile)
    _use_zli_sddl: bool = False   # use SDDL profile with inline training
    _use_zli_trained: bool = False  # train compressor first, then compress with it


def _build_compressors(cfg: BenchConfig, sddl_path: str) -> List[Compressor]:
    """Build the list of compressors to benchmark."""
    compressors = []

    # gzip
    if shutil.which("gzip"):
        compressors.append(Compressor(
            name="gzip",
            compress_cmd=["gzip", f"-{cfg.gzip_level}", "-c", "{input}"],
            decompress_cmd=["gzip", "-d", "-c", "{input}"],
            artifacts=["json", "toon", "astbin"],
            level=str(cfg.gzip_level),
            uses_stdout=True,
        ))
    else:
        compressors.append(Compressor(
            name="gzip", compress_cmd=[], decompress_cmd=[],
            artifacts=[], level=str(cfg.gzip_level), available=False,
        ))

    # pigz
    if shutil.which("pigz"):
        compressors.append(Compressor(
            name="pigz",
            compress_cmd=["pigz", f"-{cfg.pigz_level}", "-c", "{input}"],
            decompress_cmd=["pigz", "-d", "-c", "{input}"],
            artifacts=["json", "toon", "astbin"],
            level=str(cfg.pigz_level),
            uses_stdout=True,
        ))
    else:
        compressors.append(Compressor(
            name="pigz", compress_cmd=[], decompress_cmd=[],
            artifacts=[], level=str(cfg.pigz_level), available=False,
        ))

    # zstd
    if shutil.which("zstd"):
        compressors.append(Compressor(
            name="zstd",
            compress_cmd=[
                "zstd", f"-{cfg.zstd_level}", "-q", "-c", "{input}",
            ],
            decompress_cmd=["zstd", "-d", "-q", "-c", "{input}"],
            artifacts=["json", "toon", "astbin"],
            level=str(cfg.zstd_level),
            uses_stdout=True,
        ))
    else:
        compressors.append(Compressor(
            name="zstd", compress_cmd=[], decompress_cmd=[],
            artifacts=[], level=str(cfg.zstd_level), available=False,
        ))

    # nyx (OpenZL via zli) — ASTBIN only
    # Uses the internal openzl.py wrapper (zli binary) with the serial
    # profile. The SDDL profile has a CBOR serializer bug in the pinned
    # OpenZL commit, so we fall back to serial (generic) compression.
    nyx_available = False
    try:
        from ..utils.paths import find_zli
        find_zli()
        nyx_available = True
    except (FileNotFoundError, ImportError):
        pass

    if nyx_available:
        compressors.append(Compressor(
            name="nyx",
            compress_cmd=[],  # handled specially via _USE_ZLI_DIRECT
            decompress_cmd=[],
            artifacts=["astbin"],
            level="serial",
            uses_stdout=False,
            _use_zli_direct=True,
        ))
    else:
        compressors.append(Compressor(
            name="nyx", compress_cmd=[], decompress_cmd=[],
            artifacts=[], level="sddl", available=False,
        ))

    return compressors


# ---------------------------------------------------------------------------
# SHA-256 helper
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Single compress / decompress run
# ---------------------------------------------------------------------------

def _run_compress(comp: Compressor, input_path: Path, output_path: Path,
                  sddl_path: str, model_path: Optional[Path] = None) -> float:
    """Run one compression, return wall-clock seconds."""
    if comp._use_zli_trained and model_path:
        return _run_zli_compress_with_model(input_path, output_path, model_path)
    if comp._use_zli_sddl:
        return _run_zli_compress_sddl(input_path, output_path, sddl_path)
    if comp._use_zli_direct:
        return _run_zli_compress(input_path, output_path, sddl_path)

    cmd = [
        tok.format(input=str(input_path), output=str(output_path), sddl=sddl_path)
        for tok in comp.compress_cmd
    ]
    start = time.perf_counter()
    if comp.uses_stdout:
        with open(output_path, "wb") as out_f:
            proc = subprocess.run(cmd, stdout=out_f, stderr=subprocess.PIPE)
    else:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    elapsed = time.perf_counter() - start
    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace").strip()
        raise RuntimeError(
            f"{comp.name} compress failed (rc={proc.returncode}): {stderr}"
        )
    return elapsed


def _run_decompress(comp: Compressor, input_path: Path, output_path: Path,
                    sddl_path: str) -> float:
    """Run one decompression, return wall-clock seconds."""
    if comp._use_zli_direct or comp._use_zli_trained or comp._use_zli_sddl:
        return _run_zli_decompress(input_path, output_path)

    cmd = [
        tok.format(input=str(input_path), output=str(output_path), sddl=sddl_path)
        for tok in comp.decompress_cmd
    ]
    start = time.perf_counter()
    if comp.uses_stdout:
        with open(output_path, "wb") as out_f:
            proc = subprocess.run(cmd, stdout=out_f, stderr=subprocess.PIPE)
    else:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    elapsed = time.perf_counter() - start
    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace").strip()
        raise RuntimeError(
            f"{comp.name} decompress failed (rc={proc.returncode}): {stderr}"
        )
    return elapsed


def _run_zli_compress(input_path: Path, output_path: Path,
                      sddl_path: str) -> float:
    """Compress via zli — uses SDDL profile with training if sddl_path is
    provided and the compressor level is 'sddl', otherwise serial profile."""
    from .openzl import compress as zli_compress
    start = time.perf_counter()
    zli_compress(
        input_file=input_path,
        output_file=output_path,
        profile="serial",
        force=True,
    )
    return time.perf_counter() - start


def _run_zli_compress_sddl(input_path: Path, output_path: Path,
                            sddl_path: str) -> float:
    """Compress via zli with SDDL profile and inline training."""
    from .openzl import compress as zli_compress
    start = time.perf_counter()
    zli_compress(
        input_file=input_path,
        output_file=output_path,
        profile="sddl",
        profile_arg=sddl_path,
        train_inline=True,
        force=True,
    )
    return time.perf_counter() - start


def _run_zli_train(train_dir: Path, sddl_path: str, model_path: Path) -> float:
    """Train a compressor via zli with SDDL profile. Returns wall-clock seconds."""
    from .openzl import train as zli_train
    start = time.perf_counter()
    zli_train(
        sample_dir=train_dir,
        output_file=model_path,
        profile="sddl",
        profile_arg=sddl_path,
        force=True,
    )
    return time.perf_counter() - start


def _run_zli_compress_with_model(input_path: Path, output_path: Path,
                                  model_path: Path) -> float:
    """Compress via zli using a pre-trained compressor model."""
    from .openzl import compress as zli_compress
    start = time.perf_counter()
    zli_compress(
        input_file=input_path,
        output_file=output_path,
        compressor=model_path,
        force=True,
    )
    return time.perf_counter() - start


def _run_zli_decompress(input_path: Path, output_path: Path) -> float:
    """Decompress via zli."""
    from .openzl import decompress as zli_decompress
    start = time.perf_counter()
    zli_decompress(
        input_file=input_path,
        output_file=output_path,
        force=True,
    )
    return time.perf_counter() - start


# ---------------------------------------------------------------------------
# Benchmark one (artifact, compressor) pair
# ---------------------------------------------------------------------------

def _bench_pair(
    comp: Compressor,
    artifact_name: str,
    artifact_path: Path,
    compressed_dir: Path,
    decompressed_dir: Path,
    sddl_path: str,
    runs: int,
    warmup: int,
) -> BenchRow:
    """Benchmark a single (artifact, compressor) pair."""
    input_bytes = artifact_path.stat().st_size
    original_hash = _sha256(artifact_path)

    tag = f"{artifact_name}_{comp.name}"
    comp_out = compressed_dir / f"{tag}.compressed"
    decomp_out = decompressed_dir / f"{tag}.decompressed"

    c_times: List[float] = []
    d_times: List[float] = []
    compressed_bytes = 0
    verify_pass = True
    error = ""

    total_iterations = warmup + runs

    try:
        for i in range(total_iterations):
            is_warmup = i < warmup

            # Compress
            if comp_out.exists():
                comp_out.unlink()
            ct = _run_compress(comp, artifact_path, comp_out, sddl_path)
            if not is_warmup:
                c_times.append(ct)
                compressed_bytes = comp_out.stat().st_size

            # Decompress
            if decomp_out.exists():
                decomp_out.unlink()
            dt = _run_decompress(comp, comp_out, decomp_out, sddl_path)
            if not is_warmup:
                d_times.append(dt)

            # Verify
            if not is_warmup:
                decomp_hash = _sha256(decomp_out)
                if decomp_hash != original_hash:
                    verify_pass = False
                    error = (
                        f"SHA-256 mismatch on run {i - warmup}: "
                        f"expected {original_hash[:16]}..., "
                        f"got {decomp_hash[:16]}..."
                    )
    except Exception as e:
        error = str(e)
        # Fill in whatever we have
        if not c_times:
            c_times = [0.0]
        if not d_times:
            d_times = [0.0]
        verify_pass = False

    c_med = statistics.median(c_times)
    d_med = statistics.median(d_times)
    ratio = input_bytes / compressed_bytes if compressed_bytes > 0 else 0.0
    input_mib = input_bytes / (1024 * 1024)

    return BenchRow(
        artifact=artifact_name,
        compressor=f"{comp.name} -{comp.level}",
        level=comp.level,
        input_bytes=input_bytes,
        compressed_bytes=compressed_bytes,
        ratio=round(ratio, 4),
        c_time_med_s=round(c_med, 6),
        c_time_min_s=round(min(c_times), 6),
        c_time_max_s=round(max(c_times), 6),
        c_MBps_med=round(input_mib / c_med, 2) if c_med > 0 else 0.0,
        d_time_med_s=round(d_med, 6),
        d_MBps_med=round(input_mib / d_med, 2) if d_med > 0 else 0.0,
        verify_pass=verify_pass,
        error=error,
    )


# ---------------------------------------------------------------------------
# Environment info
# ---------------------------------------------------------------------------

def _collect_versions() -> Dict[str, str]:
    """Collect tool versions for reproducibility."""
    info: Dict[str, str] = {}

    for name, cmd in [
        ("python", ["python3", "--version"]),
        ("gzip", ["gzip", "--version"]),
        ("pigz", ["pigz", "--version"]),
        ("zstd", ["zstd", "--version"]),
        ("nyx", ["nyx", "--version"]),
    ]:
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5,
            )
            out = (r.stdout or r.stderr).strip().split("\n")[0]
            info[name] = out
        except Exception:
            info[name] = "not available"

    try:
        info["uname"] = platform.platform()
    except Exception:
        info["uname"] = "unknown"

    return info


# ---------------------------------------------------------------------------
# Main benchmark entry point
# ---------------------------------------------------------------------------

def run_json_benchmark(cfg: BenchConfig) -> List[BenchRow]:
    """Execute the full JSON benchmark suite. Returns list of result rows."""
    outdir = cfg.outdir
    outdir.mkdir(parents=True, exist_ok=True)

    artifacts_dir = outdir / "artifacts"
    compressed_dir = outdir / "compressed"
    decompressed_dir = outdir / "decompressed"

    for d in (artifacts_dir, compressed_dir, decompressed_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Resolve SDDL
    from ..utils.paths import find_schema
    try:
        sddl_path = str(find_schema(cfg.nyx_sddl))
    except FileNotFoundError:
        sddl_path = cfg.nyx_sddl  # pass through as-is

    # Generate artifacts
    artifacts = generate_artifacts(cfg.input_path, artifacts_dir)

    # Build compressor list
    compressors = _build_compressors(cfg, sddl_path)

    # Run benchmarks
    results: List[BenchRow] = []
    for comp in compressors:
        if not comp.available:
            continue
        for art_name in comp.artifacts:
            if art_name not in artifacts:
                continue
            art_path = artifacts[art_name]
            row = _bench_pair(
                comp=comp,
                artifact_name=art_name,
                artifact_path=art_path,
                compressed_dir=compressed_dir,
                decompressed_dir=decompressed_dir,
                sddl_path=sddl_path,
                runs=cfg.runs,
                warmup=cfg.warmup,
            )
            results.append(row)

    # Write outputs
    _write_results(results, outdir, cfg)

    # Clean up decompressed if not keeping
    if not cfg.keep_temp and decompressed_dir.exists():
        shutil.rmtree(decompressed_dir, ignore_errors=True)

    return results


# ---------------------------------------------------------------------------
# Output: table, CSV, JSON
# ---------------------------------------------------------------------------

def _write_results(results: List[BenchRow], outdir: Path,
                   cfg: BenchConfig) -> None:
    """Write results.csv, results.json, and print markdown table."""
    versions = _collect_versions()

    # CSV
    csv_path = outdir / "results.csv"
    fieldnames = [
        "artifact", "compressor", "level", "input_bytes", "compressed_bytes",
        "ratio", "c_time_med_s", "c_time_min_s", "c_time_max_s", "c_MBps_med",
        "d_time_med_s", "d_MBps_med", "verify_pass", "error",
    ]
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow(asdict(row))

    # JSON
    json_path = outdir / "results.json"
    output = {
        "input_file": str(cfg.input_path),
        "config": {
            "runs": cfg.runs,
            "warmup": cfg.warmup,
            "zstd_level": cfg.zstd_level,
            "gzip_level": cfg.gzip_level,
            "pigz_level": cfg.pigz_level,
        },
        "versions": versions,
        "results": [asdict(row) for row in results],
    }
    with open(json_path, "w") as f:
        json.dump(output, f, indent=2)
        f.write("\n")


def print_results_table(results: List[BenchRow]) -> str:
    """Format results as a markdown table grouped by artifact. Returns the table string."""
    if not results:
        return "No results.\n"

    lines: List[str] = []
    lines.append("")
    lines.append("## JSON Benchmark Results")
    lines.append("")

    # Group by artifact
    artifacts_seen: List[str] = []
    by_artifact: Dict[str, List[BenchRow]] = {}
    for r in results:
        if r.artifact not in by_artifact:
            by_artifact[r.artifact] = []
            artifacts_seen.append(r.artifact)
        by_artifact[r.artifact].append(r)

    header = (
        f"| {'Compressor':<16} | {'Input':>10} | {'Compressed':>12} | "
        f"{'Ratio':>7} | {'C MB/s':>8} | {'D MB/s':>8} | "
        f"{'C med(s)':>9} | {'Verify':>6} |"
    )
    sep = "|" + "|".join(
        "-" * (w + 2) for w in [16, 10, 12, 7, 8, 8, 9, 6]
    ) + "|"

    for art_name in artifacts_seen:
        rows = by_artifact[art_name]
        lines.append(f"### Artifact: {art_name}")
        lines.append("")
        lines.append(header)
        lines.append(sep)
        for r in sorted(rows, key=lambda x: x.compressed_bytes if x.compressed_bytes > 0 else float("inf")):
            verify = "PASS" if r.verify_pass else "FAIL"
            lines.append(
                f"| {r.compressor:<16} | "
                f"{_human_size(r.input_bytes):>10} | "
                f"{_human_size(r.compressed_bytes):>12} | "
                f"{r.ratio:>6.2f}x | "
                f"{r.c_MBps_med:>7.1f} | "
                f"{r.d_MBps_med:>7.1f} | "
                f"{r.c_time_med_s:>8.4f} | "
                f"{verify:>6} |"
            )
            if r.error:
                lines.append(f"|   Error: {r.error}")
        lines.append("")

    table = "\n".join(lines)
    return table


def _human_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"
