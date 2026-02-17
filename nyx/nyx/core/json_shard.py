"""Deterministic JSONL shard writer.

Streams canonical JSON records into fixed-size shard files, producing
an index that tracks the source provenance of every record.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple


def shard_records(
    records: Iterator[Tuple[str, int, bytes]],
    output_dir: Path,
    shard_mib: int = 128,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Write canonical JSONL records into deterministic shards.

    Args:
        records: Iterator yielding (source_file, line_no, canonical_json_bytes).
        output_dir: Directory under which shard files are written.
        shard_mib: Target shard size in MiB (uncompressed).

    Returns:
        (shard_metas, index_entries)
        shard_metas: List of per-shard metadata dicts.
        index_entries: List of per-record provenance dicts.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    shard_limit = shard_mib * 1024 * 1024

    shard_metas: List[Dict[str, Any]] = []
    index_entries: List[Dict[str, Any]] = []

    shard_num = 0
    shard_path: Optional[Path] = None
    shard_f = None
    shard_bytes = 0
    shard_records_count = 0
    shard_hasher = hashlib.sha256()

    def _close_shard():
        nonlocal shard_f, shard_path, shard_bytes, shard_records_count, shard_hasher
        if shard_f is not None:
            shard_f.close()
            shard_metas.append({
                "shard_name": shard_path.name,
                "shard_num": shard_num,
                "record_count": shard_records_count,
                "size_bytes": shard_bytes,
                "sha256": shard_hasher.hexdigest(),
            })
            shard_f = None

    def _open_shard():
        nonlocal shard_num, shard_path, shard_f, shard_bytes, shard_records_count, shard_hasher
        shard_num += 1
        shard_path = output_dir / f"shard_{shard_num:05d}.jsonl"
        shard_f = open(shard_path, "wb")
        shard_bytes = 0
        shard_records_count = 0
        shard_hasher = hashlib.sha256()

    _open_shard()

    for source_file, line_no, canonical_bytes in records:
        line = canonical_bytes + b"\n"
        offset = shard_bytes

        shard_f.write(line)
        shard_hasher.update(line)
        shard_bytes += len(line)
        shard_records_count += 1

        index_entries.append({
            "source_gz_file": source_file,
            "source_line_no": line_no,
            "shard_name": shard_path.name,
            "shard_offset_bytes": offset,
            "shard_length_bytes": len(line),
        })

        # Roll to next shard if we exceed the target
        if shard_bytes >= shard_limit:
            _close_shard()
            _open_shard()

    _close_shard()

    # Remove empty last shard if no records were written to it
    if shard_metas and shard_metas[-1]["record_count"] == 0:
        empty = output_dir / shard_metas[-1]["shard_name"]
        if empty.exists():
            empty.unlink()
        shard_metas.pop()

    return shard_metas, index_entries


def write_index(
    index_entries: List[Dict[str, Any]],
    shard_metas: List[Dict[str, Any]],
    output_dir: Path,
) -> Path:
    """Write index.json summarizing shards and record provenance."""
    index_path = output_dir / "index.json"
    index_data = {
        "total_records": len(index_entries),
        "total_shards": len(shard_metas),
        "shards": shard_metas,
    }
    with open(index_path, "w") as f:
        json.dump(index_data, f, indent=2)
        f.write("\n")
    return index_path
