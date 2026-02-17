"""OpenAlex Authors Binary (OABIN) v1 — record-level columnar format.

Separates OpenAlex author records into typed columns grouped by field,
so that identical fields across records are stored contiguously. This
creates highly compressible streams for schema-aware compressors.

Layout:
  Header (32 bytes)
  Fixed scalar columns  (works_count, cited_by_count, h_index, i10_index, mean_citedness)
  String-ref columns    (id, display_name, works_api_url, updated_date, created_date)
  Variable-length cols  (display_name_alternatives, counts_by_year)
  Complex JSON blob     (ids, affiliations, topics, etc.)
  Deduplicated string table
"""

import json
import struct
from pathlib import Path
from typing import Any, Dict, List, Tuple

OABIN_MAGIC = b"OA01"
OABIN_VERSION = 1
NULL_REF = 0xFFFFFFFF

# String-ref fields: one string pool index per record
STRING_REF_FIELDS = [
    "id",
    "display_name",
    "works_api_url",
    "updated_date",
    "created_date",
]

# Complex nested fields → stored as per-record compact JSON blob
COMPLEX_FIELDS = [
    "ids",
    "orcid",
    "affiliations",
    "last_known_institutions",
    "topics",
    "topic_share",
    "x_concepts",
    "sources",
]


def _build_string_pool(
    strings: List[str],
) -> Tuple[Dict[str, int], bytes, List[int]]:
    """Build deduplicated, sorted string pool.

    Returns (str_to_idx, blob_bytes, offsets) where offsets has len(unique)+1
    entries (sentinel at end).
    """
    unique = sorted(set(strings))
    str_to_idx = {s: i for i, s in enumerate(unique)}

    offsets: List[int] = []
    parts: List[bytes] = []
    pos = 0
    for s in unique:
        offsets.append(pos)
        encoded = s.encode("utf-8")
        parts.append(encoded)
        pos += len(encoded)
    offsets.append(pos)  # sentinel

    return str_to_idx, b"".join(parts), offsets


def records_to_oabin(records: List[Dict[str, Any]]) -> bytes:
    """Serialize a list of OpenAlex author records to OABIN v1 binary."""
    n = len(records)

    # ── Collect column values ──────────────────────────────────────────
    works_counts: List[int] = []
    cited_by_counts: List[int] = []
    h_indices: List[int] = []
    i10_indices: List[int] = []
    mean_citedness_scaled: List[int] = []

    string_refs: Dict[str, List] = {f: [] for f in STRING_REF_FIELDS}
    all_strings: List[str] = []

    alt_offsets = [0]
    alt_strings: List[str] = []

    cby_offsets = [0]
    cby_years: List[int] = []
    cby_works: List[int] = []
    cby_cited: List[int] = []

    complex_jsons: List[bytes] = []

    for rec in records:
        # Fixed scalars
        works_counts.append(rec.get("works_count", 0) or 0)
        cited_by_counts.append(rec.get("cited_by_count", 0) or 0)

        ss = rec.get("summary_stats") or {}
        h_indices.append(ss.get("h_index", 0) or 0)
        i10_indices.append(ss.get("i10_index", 0) or 0)
        mc = ss.get("2yr_mean_citedness", 0.0) or 0.0
        scaled = int(round(mc * 1e6))
        scaled = max(-2147483648, min(2147483647, scaled))
        mean_citedness_scaled.append(scaled)

        # String refs
        for field in STRING_REF_FIELDS:
            val = rec.get(field)
            if val is not None:
                val = str(val)
                all_strings.append(val)
                string_refs[field].append(val)
            else:
                string_refs[field].append(None)

        # display_name_alternatives (variable-length string array)
        alts = rec.get("display_name_alternatives") or []
        for a in alts:
            s = str(a)
            all_strings.append(s)
            alt_strings.append(s)
        alt_offsets.append(alt_offsets[-1] + len(alts))

        # counts_by_year (variable-length struct array)
        cby = rec.get("counts_by_year") or []
        for entry in cby:
            cby_years.append(entry.get("year", 0) or 0)
            cby_works.append(entry.get("works_count", 0) or 0)
            cby_cited.append(entry.get("cited_by_count", 0) or 0)
        cby_offsets.append(cby_offsets[-1] + len(cby))

        # Complex nested fields → compact JSON
        complex_obj = {}
        for field in COMPLEX_FIELDS:
            if field in rec and rec[field] is not None:
                complex_obj[field] = rec[field]
        complex_jsons.append(
            json.dumps(
                complex_obj, separators=(",", ":"),
                sort_keys=True, ensure_ascii=False,
            ).encode("utf-8")
        )

    # ── Build string pool ──────────────────────────────────────────────
    str_to_idx, string_blob, string_offsets = _build_string_pool(all_strings)
    n_strings = len(string_offsets) - 1

    def ref(val):
        return NULL_REF if val is None else str_to_idx[val]

    # ── Build complex blob ─────────────────────────────────────────────
    complex_offsets = [0]
    for cj in complex_jsons:
        complex_offsets.append(complex_offsets[-1] + len(cj))
    complex_blob = b"".join(complex_jsons)

    n_alts_total = alt_offsets[-1]
    n_cby_total = cby_offsets[-1]

    # ── Serialize ──────────────────────────────────────────────────────
    parts: List[bytes] = []

    # Header (32 bytes)
    parts.append(struct.pack(
        "<4sHHIIIIII",
        OABIN_MAGIC,
        OABIN_VERSION,
        0,  # reserved
        n,
        n_strings,
        n_alts_total,
        n_cby_total,
        len(complex_blob),
        len(string_blob),
    ))

    # Fixed scalar columns (i32 LE each)
    parts.append(struct.pack(f"<{n}i", *works_counts))
    parts.append(struct.pack(f"<{n}i", *cited_by_counts))
    parts.append(struct.pack(f"<{n}i", *h_indices))
    parts.append(struct.pack(f"<{n}i", *i10_indices))
    parts.append(struct.pack(f"<{n}i", *mean_citedness_scaled))

    # String-ref columns (u32 LE indices)
    for field in STRING_REF_FIELDS:
        refs = [ref(v) for v in string_refs[field]]
        parts.append(struct.pack(f"<{n}I", *refs))

    # display_name_alternatives
    parts.append(struct.pack(f"<{n + 1}I", *alt_offsets))
    if n_alts_total > 0:
        alt_refs = [str_to_idx[s] for s in alt_strings]
        parts.append(struct.pack(f"<{n_alts_total}I", *alt_refs))

    # counts_by_year (3 separate i32 columns)
    parts.append(struct.pack(f"<{n + 1}I", *cby_offsets))
    if n_cby_total > 0:
        parts.append(struct.pack(f"<{n_cby_total}i", *cby_years))
        parts.append(struct.pack(f"<{n_cby_total}i", *cby_works))
        parts.append(struct.pack(f"<{n_cby_total}i", *cby_cited))

    # Complex blob
    parts.append(struct.pack(f"<{n + 1}I", *complex_offsets))
    parts.append(complex_blob)

    # String table
    parts.append(struct.pack(f"<{n_strings + 1}I", *string_offsets))
    parts.append(string_blob)

    return b"".join(parts)


def jsonl_to_oabin_file(jsonl_path: str, out_path: str) -> Dict[str, Any]:
    """Convert a JSONL shard of OpenAlex authors to OABIN v1.

    Returns metadata dict.
    """
    records: List[Dict[str, Any]] = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    payload = records_to_oabin(records)
    Path(out_path).write_bytes(payload)

    (_, version, _, n_records, n_strings, n_alts_total, n_cby_total,
     complex_blob_len, string_blob_len) = struct.unpack(
        "<4sHHIIIIII", payload[:32]
    )

    return {
        "version": version,
        "n_records": n_records,
        "n_strings": n_strings,
        "n_alts_total": n_alts_total,
        "n_cby_total": n_cby_total,
        "complex_blob_len": complex_blob_len,
        "string_blob_len": string_blob_len,
        "total_bytes": len(payload),
        "record_count": len(records),
    }
