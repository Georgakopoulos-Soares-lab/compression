"""OpenAlex shard-based benchmark orchestrator.

Three-step pipeline:
  1. Decompress & shard OpenAlex .gz JSONL files into deterministic JSONL shards
  2. Convert JSONL shards to ASTBIN binary shards
  3. Benchmark compressors on both artifact types, verify SHA-256 round-trip

Reuses compression/decompression runners and result types from json_bench.
"""

import csv
import hashlib
import json
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import click

from .astbin import jsonl_to_astbin_file
from .oabin import jsonl_to_oabin_file
from .json_bench import (
    BenchRow,
    Compressor,
    _collect_versions,
    _human_size,
    _run_compress,
    _run_decompress,
    _sha256,
)
from .json_shard import shard_records, write_index
from .openalex_reader import discover_gz_files, iter_openalex_records


@dataclass
class OpenAlexBenchConfig:
    """Configuration for an OpenAlex benchmark run."""

    input_gz_dir: Path
    outdir: Path
    pattern: str = "*.gz"
    shard_mib: int = 128
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
    limit_records: Optional[int] = None
    verbose: bool = False


# ---------------------------------------------------------------------------
# Step 1: Build JSONL shards
# ---------------------------------------------------------------------------

def step_shard_jsonl(cfg: OpenAlexBenchConfig) -> List[Dict[str, Any]]:
    """Decompress .gz files and produce JSONL shards. Returns shard metadata."""
    jsonl_dir = cfg.outdir / "artifacts" / "jsonl"
    index_path = jsonl_dir / "index.json"

    # Reuse existing shards if index and all shard files exist
    if index_path.exists():
        with open(index_path) as f:
            index_data = json.load(f)
        shard_metas = index_data["shards"]
        all_present = all(
            (jsonl_dir / m["shard_name"]).exists() for m in shard_metas
        )
        if all_present:
            total_records = sum(s["record_count"] for s in shard_metas)
            total_bytes = sum(s["size_bytes"] for s in shard_metas)
            click.echo(
                f"  {len(shard_metas)} shards (cached), {total_records} records, "
                f"{_human_size(total_bytes)} total"
            )
            return shard_metas

    jsonl_dir.mkdir(parents=True, exist_ok=True)

    gz_files = discover_gz_files(cfg.input_gz_dir, cfg.pattern)
    click.echo(f"  Found {len(gz_files)} .gz files")

    records = iter_openalex_records(gz_files, limit=cfg.limit_records)
    shard_metas, index_entries = shard_records(records, jsonl_dir, cfg.shard_mib)

    write_index(index_entries, shard_metas, jsonl_dir)
    total_records = sum(s["record_count"] for s in shard_metas)
    total_bytes = sum(s["size_bytes"] for s in shard_metas)

    click.echo(
        f"  {len(shard_metas)} shards, {total_records} records, "
        f"{_human_size(total_bytes)} total"
    )
    return shard_metas


# ---------------------------------------------------------------------------
# Step 2: Build ASTBIN shards
# ---------------------------------------------------------------------------

def step_build_astbin(
    cfg: OpenAlexBenchConfig,
    shard_metas: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert each JSONL shard to an ASTBIN shard. Returns ASTBIN metadata."""
    jsonl_dir = cfg.outdir / "artifacts" / "jsonl"
    astbin_dir = cfg.outdir / "artifacts" / "astbin"
    astbin_dir.mkdir(parents=True, exist_ok=True)

    astbin_metas: List[Dict[str, Any]] = []

    for i, meta in enumerate(shard_metas, 1):
        shard_name = meta["shard_name"]
        jsonl_path = jsonl_dir / shard_name
        astbin_name = shard_name.replace(".jsonl", ".astbin")
        astbin_path = astbin_dir / astbin_name

        if astbin_path.exists():
            click.echo(f"  [{i}/{len(shard_metas)}] {astbin_name} (cached)")
            info = {
                "shard_name": astbin_name,
                "total_bytes": astbin_path.stat().st_size,
                "sha256": _sha256(astbin_path),
            }
        else:
            click.echo(f"  [{i}/{len(shard_metas)}] {shard_name} -> {astbin_name}")
            info = jsonl_to_astbin_file(str(jsonl_path), str(astbin_path))
            info["shard_name"] = astbin_name
            info["sha256"] = _sha256(astbin_path)
        astbin_metas.append(info)

    total_bytes = sum(m["total_bytes"] for m in astbin_metas)
    click.echo(f"  {len(astbin_metas)} ASTBIN shards, {_human_size(total_bytes)} total")
    return astbin_metas


# ---------------------------------------------------------------------------
# Step 2b: Build OABIN shards
# ---------------------------------------------------------------------------

def step_build_oabin(
    cfg: OpenAlexBenchConfig,
    shard_metas: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert each JSONL shard to an OABIN shard. Returns OABIN metadata."""
    jsonl_dir = cfg.outdir / "artifacts" / "jsonl"
    oabin_dir = cfg.outdir / "artifacts" / "oabin"
    oabin_dir.mkdir(parents=True, exist_ok=True)

    oabin_metas: List[Dict[str, Any]] = []

    for i, meta in enumerate(shard_metas, 1):
        shard_name = meta["shard_name"]
        jsonl_path = jsonl_dir / shard_name
        oabin_name = shard_name.replace(".jsonl", ".oabin")
        oabin_path = oabin_dir / oabin_name

        if oabin_path.exists():
            click.echo(f"  [{i}/{len(shard_metas)}] {oabin_name} (cached)")
            info = {
                "shard_name": oabin_name,
                "total_bytes": oabin_path.stat().st_size,
                "sha256": _sha256(oabin_path),
            }
        else:
            click.echo(f"  [{i}/{len(shard_metas)}] {shard_name} -> {oabin_name}")
            info = jsonl_to_oabin_file(str(jsonl_path), str(oabin_path))
            info["shard_name"] = oabin_name
            info["sha256"] = _sha256(oabin_path)
        oabin_metas.append(info)

    total_bytes = sum(m["total_bytes"] for m in oabin_metas)
    click.echo(f"  {len(oabin_metas)} OABIN shards, {_human_size(total_bytes)} total")
    return oabin_metas


# ---------------------------------------------------------------------------
# Step 3: Benchmark
# ---------------------------------------------------------------------------

def _build_shard_compressors(cfg: OpenAlexBenchConfig, sddl_path: str) -> List[Compressor]:
    """Build compressor list for shard benchmarking."""
    compressors: List[Compressor] = []

    # gzip
    if shutil.which("gzip"):
        compressors.append(Compressor(
            name="gzip",
            compress_cmd=["gzip", f"-{cfg.gzip_level}", "-c", "{input}"],
            decompress_cmd=["gzip", "-d", "-c", "{input}"],
            artifacts=["jsonl", "astbin", "oabin"],
            level=str(cfg.gzip_level),
        ))

    # pigz
    if shutil.which("pigz"):
        compressors.append(Compressor(
            name="pigz",
            compress_cmd=["pigz", f"-{cfg.pigz_level}", "-c", "{input}"],
            decompress_cmd=["pigz", "-d", "-c", "{input}"],
            artifacts=["jsonl", "astbin", "oabin"],
            level=str(cfg.pigz_level),
        ))

    # zstd
    if shutil.which("zstd"):
        compressors.append(Compressor(
            name="zstd",
            compress_cmd=["zstd", f"-{cfg.zstd_level}", "-q", "-c", "{input}"],
            decompress_cmd=["zstd", "-d", "-q", "-c", "{input}"],
            artifacts=["jsonl", "astbin", "oabin"],
            level=str(cfg.zstd_level),
        ))

    # nyx serial (ASTBIN + OABIN) — generic compression, no schema
    nyx_available = False
    try:
        from ..utils.paths import find_zli
        find_zli()
        nyx_available = True
    except (FileNotFoundError, ImportError):
        pass

    if nyx_available:
        # nyx trained (OABIN) — train a compressor with SDDL, then compress
        compressors.append(Compressor(
            name="nyx",
            compress_cmd=[],
            decompress_cmd=[],
            artifacts=["oabin"],
            level="sddl-trained",
            uses_stdout=False,
            _use_zli_trained=True,
        ))

    return compressors


def step_benchmark(
    cfg: OpenAlexBenchConfig,
    jsonl_metas: List[Dict[str, Any]],
    astbin_metas: List[Dict[str, Any]],
    oabin_metas: Optional[List[Dict[str, Any]]] = None,
) -> List[BenchRow]:
    """Benchmark all compressors on all shards."""
    from ..utils.paths import find_schema

    try:
        sddl_path = str(find_schema("oabin_v1.sddl"))
    except FileNotFoundError:
        sddl_path = cfg.nyx_sddl

    jsonl_dir = cfg.outdir / "artifacts" / "jsonl"
    astbin_dir = cfg.outdir / "artifacts" / "astbin"
    oabin_dir = cfg.outdir / "artifacts" / "oabin"
    compressed_dir = cfg.outdir / "compressed"
    decompressed_dir = cfg.outdir / "decompressed"

    for sub in ("jsonl", "astbin", "oabin"):
        (compressed_dir / sub).mkdir(parents=True, exist_ok=True)
    decompressed_dir.mkdir(parents=True, exist_ok=True)

    compressors = _build_shard_compressors(cfg, sddl_path)

    # Build JSONL size lookup: base shard name (no extension) -> size in bytes
    # e.g. "shard_00001" -> 134217728
    jsonl_size_by_base: Dict[str, int] = {}
    for meta in jsonl_metas:
        base = meta["shard_name"].replace(".jsonl", "")
        jsonl_size_by_base[base] = meta["size_bytes"]

    # Build artifact list: (type, shard_name, path)
    artifacts: List[tuple] = []
    for meta in jsonl_metas:
        p = jsonl_dir / meta["shard_name"]
        if p.exists():
            artifacts.append(("jsonl", meta["shard_name"], p))
    for meta in astbin_metas:
        p = astbin_dir / meta["shard_name"]
        if p.exists():
            artifacts.append(("astbin", meta["shard_name"], p))
    for meta in (oabin_metas or []):
        p = oabin_dir / meta["shard_name"]
        if p.exists():
            artifacts.append(("oabin", meta["shard_name"], p))

    # Pre-train once for compressors that need a trained model.
    # All shards share the same binary structure, so we train on the first
    # shard and reuse the compressor for all remaining shards.
    from .json_bench import _run_zli_train

    trained_models: Dict[str, Path] = {}  # comp key -> model path
    for comp in compressors:
        if not comp.available or not comp._use_zli_trained:
            continue
        # Find the first available artifact for training
        for atype, shard_name, artifact_path in artifacts:
            if atype not in comp.artifacts:
                continue
            comp_key = f"{comp.name}_{comp.level}"
            train_dir = compressed_dir / atype / f"_train_{comp_key}"
            train_dir.mkdir(parents=True, exist_ok=True)
            import shutil as _shutil
            _shutil.copy2(artifact_path, train_dir / artifact_path.name)
            model_path = compressed_dir / atype / f"_model_{comp_key}.compressor"
            click.echo(
                f"  Training nyx compressor on {atype}/{shard_name} "
                f"(reused for all shards)..."
            )
            train_time = _run_zli_train(train_dir, sddl_path, model_path)
            click.echo(f"  Trained in {train_time:.1f}s")
            trained_models[comp_key] = model_path
            break  # only need one shard for training

    results: List[BenchRow] = []
    total_pairs = sum(
        1 for comp in compressors
        for atype, _, _ in artifacts
        if atype in comp.artifacts
    )
    pair_num = 0

    for comp in compressors:
        if not comp.available:
            continue
        comp_key = f"{comp.name}_{comp.level}"
        model_path = trained_models.get(comp_key)

        for atype, shard_name, artifact_path in artifacts:
            if atype not in comp.artifacts:
                continue
            pair_num += 1
            click.echo(
                f"  [{pair_num}/{total_pairs}] {comp.name} -{comp.level} "
                f"on {atype}/{shard_name}"
            )

            # For non-JSONL artifacts, compute ratio from the original JSONL size
            original_bytes = 0
            if atype != "jsonl":
                # Strip format extension to get base shard name
                base = shard_name
                for ext in (".astbin", ".oabin"):
                    base = base.replace(ext, "")
                original_bytes = jsonl_size_by_base.get(base, 0)

            row = _bench_shard_pair(
                comp=comp,
                artifact_type=atype,
                shard_name=shard_name,
                artifact_path=artifact_path,
                compressed_dir=compressed_dir / atype,
                decompressed_dir=decompressed_dir,
                sddl_path=sddl_path,
                runs=cfg.runs,
                warmup=cfg.warmup,
                original_bytes=original_bytes,
                model_path=model_path,
            )
            results.append(row)

    return results


def _bench_shard_pair(
    comp: Compressor,
    artifact_type: str,
    shard_name: str,
    artifact_path: Path,
    compressed_dir: Path,
    decompressed_dir: Path,
    sddl_path: str,
    runs: int,
    warmup: int,
    original_bytes: int = 0,
    model_path: Optional[Path] = None,
) -> BenchRow:
    """Benchmark one (shard, compressor) pair. Reuses json_bench runners.

    Args:
        original_bytes: If > 0, use this as the denominator for ratio instead
                        of the artifact size. This allows computing ratio from
                        the original input (e.g. JSONL) to the compressed output.
        model_path: Pre-trained compressor model for _use_zli_trained compressors.
    """
    import statistics

    input_bytes = artifact_path.stat().st_size
    ratio_base = original_bytes if original_bytes > 0 else input_bytes
    original_hash = _sha256(artifact_path)

    tag = f"{artifact_type}_{shard_name}_{comp.name}_{comp.level}"
    comp_sub = compressed_dir / comp.name
    comp_sub.mkdir(parents=True, exist_ok=True)
    comp_out = comp_sub / f"{shard_name}.compressed"
    decomp_out = decompressed_dir / f"{tag}.decompressed"

    c_times: List[float] = []
    d_times: List[float] = []
    compressed_bytes = 0
    verify_pass = True
    error = ""

    try:
        for i in range(warmup + runs):
            is_warmup = i < warmup

            if comp_out.exists():
                comp_out.unlink()
            ct = _run_compress(
                comp, artifact_path, comp_out, sddl_path,
                model_path=model_path,
            )
            if not is_warmup:
                c_times.append(ct)
                compressed_bytes = comp_out.stat().st_size

            if decomp_out.exists():
                decomp_out.unlink()
            dt = _run_decompress(comp, comp_out, decomp_out, sddl_path)
            if not is_warmup:
                d_times.append(dt)

            if not is_warmup:
                dh = _sha256(decomp_out)
                if dh != original_hash:
                    verify_pass = False
                    error = (
                        f"SHA-256 mismatch run {i - warmup}: "
                        f"expected {original_hash[:16]}..., got {dh[:16]}..."
                    )
    except Exception as e:
        error = str(e)
        if not c_times:
            c_times = [0.0]
        if not d_times:
            d_times = [0.0]
        verify_pass = False

    c_med = statistics.median(c_times)
    d_med = statistics.median(d_times)
    ratio = ratio_base / compressed_bytes if compressed_bytes > 0 else 0.0
    input_mib = ratio_base / (1024 * 1024)

    return BenchRow(
        artifact=f"{artifact_type}/{shard_name}",
        compressor=f"{comp.name} -{comp.level}",
        level=comp.level,
        input_bytes=ratio_base,
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
# Write manifest
# ---------------------------------------------------------------------------

def write_manifest(
    outdir: Path,
    jsonl_metas: List[Dict[str, Any]],
    astbin_metas: List[Dict[str, Any]],
    oabin_metas: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Write artifacts/manifest.json with per-shard metadata and hashes."""
    manifest = {
        "jsonl_shards": jsonl_metas,
        "astbin_shards": astbin_metas,
    }
    if oabin_metas:
        manifest["oabin_shards"] = oabin_metas
    path = outdir / "artifacts" / "manifest.json"
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Write results
# ---------------------------------------------------------------------------

def write_openalex_results(
    results: List[BenchRow],
    cfg: OpenAlexBenchConfig,
) -> None:
    """Write results.csv, results.json, and env.json."""
    outdir = cfg.outdir
    versions = _collect_versions()

    # env.json
    with open(outdir / "env.json", "w") as f:
        json.dump(versions, f, indent=2)
        f.write("\n")

    # results.csv
    fieldnames = [
        "artifact", "compressor", "level", "input_bytes", "compressed_bytes",
        "ratio", "c_time_med_s", "c_time_min_s", "c_time_max_s", "c_MBps_med",
        "d_time_med_s", "d_MBps_med", "verify_pass", "error",
    ]
    with open(outdir / "results.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow(asdict(row))

    # results.json
    output = {
        "input_gz_dir": str(cfg.input_gz_dir),
        "config": {
            "shard_mib": cfg.shard_mib,
            "runs": cfg.runs,
            "warmup": cfg.warmup,
            "zstd_level": cfg.zstd_level,
            "gzip_level": cfg.gzip_level,
            "pigz_level": cfg.pigz_level,
            "limit_records": cfg.limit_records,
        },
        "versions": versions,
        "results": [asdict(row) for row in results],
    }
    with open(outdir / "results.json", "w") as f:
        json.dump(output, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Print table
# ---------------------------------------------------------------------------

def print_openalex_table(results: List[BenchRow]) -> str:
    """Print aggregated results table grouped by artifact type and compressor.

    Instead of one row per shard, aggregates across shards to show totals.
    """
    if not results:
        return "No results.\n"

    # Aggregate by (artifact_type, compressor)
    @dataclass
    class Agg:
        input_bytes: int = 0
        compressed_bytes: int = 0
        c_times: list = None
        d_times: list = None
        all_pass: bool = True
        errors: list = None

        def __post_init__(self):
            if self.c_times is None:
                self.c_times = []
            if self.d_times is None:
                self.d_times = []
            if self.errors is None:
                self.errors = []

    aggregated: Dict[tuple, Agg] = {}

    for r in results:
        # Extract artifact type from "jsonl/shard_00001.jsonl"
        atype = r.artifact.split("/")[0] if "/" in r.artifact else r.artifact
        key = (atype, r.compressor)
        if key not in aggregated:
            aggregated[key] = Agg()
        a = aggregated[key]
        a.input_bytes += r.input_bytes
        a.compressed_bytes += r.compressed_bytes
        a.c_times.append(r.c_time_med_s)
        a.d_times.append(r.d_time_med_s)
        if not r.verify_pass:
            a.all_pass = False
        if r.error:
            a.errors.append(r.error)

    lines: List[str] = []
    lines.append("")
    lines.append("## OpenAlex Benchmark Results (aggregated)")
    lines.append("Ratio = original JSONL size / compressed size "
                 "(for astbin/oabin artifacts)")
    lines.append("")

    header = (
        f"| {'Artifact':<10} | {'Compressor':<20} | {'Orig Input':>10} | "
        f"{'Compressed':>12} | {'Ratio':>7} | {'C MB/s':>8} | "
        f"{'D MB/s':>8} | {'Verify':>6} |"
    )
    sep = "|" + "|".join(
        "-" * (w + 2) for w in [10, 20, 10, 12, 7, 8, 8, 6]
    ) + "|"

    lines.append(header)
    lines.append(sep)

    for (atype, compressor), a in sorted(aggregated.items()):
        ratio = a.input_bytes / a.compressed_bytes if a.compressed_bytes > 0 else 0.0
        total_c = sum(a.c_times)
        total_d = sum(a.d_times)
        input_mib = a.input_bytes / (1024 * 1024)
        c_mbps = input_mib / total_c if total_c > 0 else 0.0
        d_mbps = input_mib / total_d if total_d > 0 else 0.0
        verify = "PASS" if a.all_pass else "FAIL"

        lines.append(
            f"| {atype:<10} | {compressor:<20} | "
            f"{_human_size(a.input_bytes):>10} | "
            f"{_human_size(a.compressed_bytes):>12} | "
            f"{ratio:>6.2f}x | "
            f"{c_mbps:>7.1f} | "
            f"{d_mbps:>7.1f} | "
            f"{verify:>6} |"
        )

    lines.append("")
    table = "\n".join(lines)
    return table


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_openalex_benchmark(cfg: OpenAlexBenchConfig) -> List[BenchRow]:
    """Execute the full OpenAlex benchmark pipeline."""
    cfg.outdir.mkdir(parents=True, exist_ok=True)

    # Step 1
    click.echo("Step 1/4: Sharding JSONL from .gz files...")
    t0 = time.perf_counter()
    jsonl_metas = step_shard_jsonl(cfg)
    click.echo(f"  Done in {time.perf_counter() - t0:.1f}s\n")

    # Step 2a
    click.echo("Step 2/4: Building ASTBIN shards...")
    t0 = time.perf_counter()
    astbin_metas = step_build_astbin(cfg, jsonl_metas)
    click.echo(f"  Done in {time.perf_counter() - t0:.1f}s\n")

    # Step 2b
    click.echo("Step 3/4: Building OABIN shards...")
    t0 = time.perf_counter()
    oabin_metas = step_build_oabin(cfg, jsonl_metas)
    click.echo(f"  Done in {time.perf_counter() - t0:.1f}s\n")

    # Write manifest
    write_manifest(cfg.outdir, jsonl_metas, astbin_metas, oabin_metas)

    # Step 3
    click.echo("Step 4/4: Benchmarking compressors...")
    t0 = time.perf_counter()
    results = step_benchmark(cfg, jsonl_metas, astbin_metas, oabin_metas)
    click.echo(f"  Done in {time.perf_counter() - t0:.1f}s\n")

    # Write results
    write_openalex_results(results, cfg)

    # Clean up
    if not cfg.keep_temp:
        decomp_dir = cfg.outdir / "decompressed"
        if decomp_dir.exists():
            shutil.rmtree(decomp_dir, ignore_errors=True)

    return results
