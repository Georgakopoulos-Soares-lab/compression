"""Streaming reader for OpenAlex .gz JSONL snapshot files.

Iterates over gzip-compressed JSONL files, yielding canonicalized JSON
records one at a time without loading the entire dataset into memory.

Canonical form:
  - Keys sorted lexicographically
  - Compact separators: (',', ':')
  - ensure_ascii=False
  - UTF-8 encoded
  - One JSON object per line, trailing newline
"""

import gzip
import json
from pathlib import Path
from typing import Iterator, List, Optional, Tuple


def discover_gz_files(input_dir: Path, pattern: str = "*.gz") -> List[Path]:
    """Find and return .gz files sorted lexicographically."""
    files = sorted(input_dir.glob(pattern))
    if not files:
        raise FileNotFoundError(
            f"No files matching '{pattern}' in {input_dir}"
        )
    return files


def iter_openalex_records(
    gz_paths: List[Path],
    limit: Optional[int] = None,
) -> Iterator[Tuple[str, int, bytes]]:
    """Yield (source_file, line_no, canonical_json_bytes) from .gz JSONL files.

    Records are emitted in deterministic order: files sorted lexicographically,
    lines within each file in original order.

    Each yielded bytes value is a canonical JSON line (sorted keys, compact
    separators, UTF-8 encoded) WITHOUT a trailing newline — the caller adds
    newlines when writing to shards.

    Args:
        gz_paths: Sorted list of .gz file paths.
        limit: If set, stop after this many records total.
    """
    total = 0
    for gz_path in gz_paths:
        line_no = 0
        with gzip.open(gz_path, "rt", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                line_no += 1

                obj = json.loads(raw_line)
                canonical = json.dumps(
                    obj,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")

                yield (gz_path.name, line_no, canonical)

                total += 1
                if limit is not None and total >= limit:
                    return
