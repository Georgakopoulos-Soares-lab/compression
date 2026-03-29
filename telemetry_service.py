"""Telemetry JSONL compression service.

Provides compress() and decompress() for telegraf JSONL data.
Uses pre-trained OpenZL compressor for high-ratio lossless compression
with bit-exact reconstruction.

Usage:
    from telemetry_service import compress, decompress

    compress("input.jsonl", "output.zljsonl")
    decompress("output.zljsonl", "reconstructed.jsonl")
"""

import concurrent.futures
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ZLI = _HERE / "zli"
_MODELS = _HERE / "models" / "lossless_telemetry"
_COMPRESSOR = _MODELS / "telemetry_csv.zl_compressor"
_SCHEMA = _MODELS / "telemetry_schema.json"


def compress(input_path, output_path, models_dir=None, num_workers=0):
    """Compress a telemetry JSONL file into a .zljsonl container.

    Args:
        input_path: Path to input .jsonl file.
        output_path: Path for output .zljsonl file.
        models_dir: Optional path to models directory (default: models/lossless_telemetry/).
        num_workers: Number of parallel workers (0 = auto-detect CPU count).
    """
    import telemetry_codec
    from zljsonl import create_zljsonl

    input_path = Path(input_path)
    output_path = Path(output_path)

    schema_path = (Path(models_dir) / "telemetry_schema.json") if models_dir else _SCHEMA
    compressor = (Path(models_dir) / "telemetry_csv.zl_compressor") if models_dir else _COMPRESSOR
    schema = telemetry_codec.load_schema(schema_path)

    if num_workers == 0:
        num_workers = os.cpu_count() or 4

    tmpdir = Path(tempfile.mkdtemp(prefix="telem_"))
    try:
        tsv_dir = tmpdir / "tsv"
        comp_dir = tmpdir / "compressed"
        comp_dir.mkdir()

        # 1. Encode JSONL → type-grouped TSVs + manifest
        tsv_paths, routing_path, _ = telemetry_codec.encode(
            input_path, tsv_dir, schema=schema, num_workers=num_workers)

        # 2. Compress all parts in parallel
        schema_json = tsv_dir / "schema.json"
        all_parts = [(p, compressor) for p in tsv_paths]
        all_parts.append((routing_path, None))    # manifest.bin → serial profile
        all_parts.append((schema_json, None))      # schema.json → serial profile

        entries = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as ex:
            futs = {}
            for part_path, comp in all_parts:
                out = comp_dir / (part_path.name + ".zl")
                futs[ex.submit(_zli_compress, part_path, out, comp)] = part_path

            for fut in concurrent.futures.as_completed(futs):
                fut.result()  # raise on error
                src = futs[fut]
                cpath = comp_dir / (src.name + ".zl")
                entries[src.name] = (cpath.read_bytes(), src.stat().st_size)

        # 3. Bundle into .zljsonl container
        create_zljsonl(output_path, entries)

        return output_path
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def decompress(input_path, output_path, num_workers=0):
    """Decompress a .zljsonl container back to the original JSONL file.

    Args:
        input_path: Path to .zljsonl container.
        output_path: Path for reconstructed .jsonl file.
        num_workers: Number of parallel workers (0 = auto-detect CPU count).
    """
    import telemetry_codec
    from zljsonl import extract_zljsonl

    input_path = Path(input_path)
    output_path = Path(output_path)

    if num_workers == 0:
        num_workers = os.cpu_count() or 4

    tmpdir = Path(tempfile.mkdtemp(prefix="telem_dec_"))
    try:
        ext_dir = tmpdir / "extracted"
        tsv_dir = tmpdir / "tsv"
        tsv_dir.mkdir()

        # 1. Extract container
        entries = extract_zljsonl(input_path, ext_dir)

        # 2. Decompress all parts in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as ex:
            futs = []
            for name in entries:
                futs.append(ex.submit(
                    _zli_decompress, ext_dir / name, tsv_dir / name))
            for f in concurrent.futures.as_completed(futs):
                f.result()  # raise on error

        # 3. Reconstruct JSONL
        telemetry_codec.decode(tsv_dir, output_path)

        return output_path
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _zli_compress(input_path, output_path, compressor=None):
    """Compress a single file using zli."""
    cmd = [str(_ZLI), "compress", str(input_path),
           "--output", str(output_path), "--force"]
    if compressor and Path(compressor).is_file():
        cmd += ["--compressor", str(compressor)]
    else:
        cmd += ["--profile", "serial", "--profile-arg", ""]
    subprocess.run(cmd, check=True, capture_output=True)


def _zli_decompress(input_path, output_path):
    """Decompress a single file using zli."""
    subprocess.run(
        [str(_ZLI), "decompress", str(input_path),
         "--output", str(output_path), "--force"],
        check=True, capture_output=True)


if __name__ == "__main__":
    import sys
    import time

    if len(sys.argv) < 3:
        print("Usage:")
        print("  python telemetry_service.py compress <input.jsonl> <output.zljsonl>")
        print("  python telemetry_service.py decompress <input.zljsonl> <output.jsonl>")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "compress":
        src, dst = Path(sys.argv[2]), Path(sys.argv[3])
        t0 = time.perf_counter()
        compress(src, dst)
        elapsed = time.perf_counter() - t0
        in_sz = src.stat().st_size
        out_sz = dst.stat().st_size
        ratio = in_sz / out_sz if out_sz else 0
        print(f"{src.name}: {in_sz:,} -> {out_sz:,} bytes "
              f"({ratio:.1f}x, {elapsed:.1f}s)")
    elif cmd == "decompress":
        src, dst = Path(sys.argv[2]), Path(sys.argv[3])
        t0 = time.perf_counter()
        decompress(src, dst)
        elapsed = time.perf_counter() - t0
        print(f"{dst.name}: {dst.stat().st_size:,} bytes ({elapsed:.1f}s)")
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
