"""Generic JSONL codec: lossless encode/decode for arbitrary JSONL files.

Encode: Parse JSONL → group records by structural fingerprint → flatten one
        level deep → write one TSV per group + schema.json + manifest.bin for
        byte-exact round-trip reconstruction.

Decode: Read TSVs + schema/manifest → reconstruct exact original JSONL.

Design principles:
  - No hardcoded field names.  Grouping is by structural fingerprint (the set
    of top-level key names), not by a specific "type" field.
  - One-level flattening: nested objects become dot-notation columns
    (e.g. {"a":{"x":1}} → column "a.x").  Arrays and deeper nesting are stored
    as raw JSON strings.
  - Superset merging: during training, groups whose key sets overlap heavily
    are merged into one group with the union of all columns.  Fewer TSVs →
    more rows per column → better compression.
  - Raw JSON preservation: values are extracted as exact text from the source
    line (no Python float/int round-tripping).
  - Schema/manifest split: static column definitions are saved as schema.json
    alongside the trained compressor.  Per-file routing is a compact binary
    manifest.  When no schema exists (first run / training), a full meta.json
    is written instead.
  - __extra__ column: at compression time with a pre-built schema, any field
    not in the schema is stored as raw JSON in a catch-all column.  This
    preserves losslessness for unseen structures.
  - __skidx__ optimisation: if all records in a group share the same key
    ordering, it is stored once in the schema.  Otherwise a compact integer
    index references a list of observed orderings.
"""

import json
import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class JsonlCodecError(Exception):
    """Raised when JSONL codec encode/decode fails."""


# ---------------------------------------------------------------------------
# Raw JSON scanner — extracts exact text without Python float rounding
# ---------------------------------------------------------------------------

def _skip_ws(s: str, pos: int) -> int:
    while pos < len(s) and s[pos] in " \t\r\n":
        pos += 1
    return pos


def _scan_json_value(s: str, pos: int) -> Tuple[str, int]:
    """Scan a single JSON value starting at *pos*.

    Returns (raw_text, end_pos) where raw_text is the exact substring.
    """
    pos = _skip_ws(s, pos)
    if pos >= len(s):
        return "", pos

    ch = s[pos]

    if ch == '"':
        end = pos + 1
        while end < len(s):
            c = s[end]
            if c == "\\":
                end += 2
            elif c == '"':
                end += 1
                return s[pos:end], end
            else:
                end += 1
        return s[pos:end], end

    if ch == '{':
        return _scan_bracket(s, pos, '{', '}')

    if ch == '[':
        return _scan_bracket(s, pos, '[', ']')

    if s[pos:pos + 4] == "true":
        return "true", pos + 4
    if s[pos:pos + 5] == "false":
        return "false", pos + 5
    if s[pos:pos + 4] == "null":
        return "null", pos + 4

    # Number
    end = pos
    while end < len(s) and s[end] not in ",}] \t\r\n":
        end += 1
    return s[pos:end], end


def _scan_bracket(s: str, pos: int, open_ch: str, close_ch: str) -> Tuple[str, int]:
    """Scan a balanced bracket structure (object or array)."""
    depth = 1
    end = pos + 1
    in_string = False
    while end < len(s) and depth > 0:
        c = s[end]
        if in_string:
            if c == "\\":
                end += 1
            elif c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
        end += 1
    return s[pos:end], end


def _parse_object_raw(s: str, pos: int = 0) -> Tuple[Dict[str, str], int, List[str]]:
    """Parse a JSON object, returning raw value text for each key.

    Returns:
        (dict of key->raw_value_text, end_position, ordered_key_list)
    """
    pos = _skip_ws(s, pos)
    if pos >= len(s) or s[pos] != '{':
        return {}, pos, []

    pos += 1  # skip {
    result = {}
    keys = []

    while True:
        pos = _skip_ws(s, pos)
        if pos >= len(s):
            break
        if s[pos] == '}':
            pos += 1
            break

        # Key (string)
        key_raw, pos = _scan_json_value(s, pos)
        key = key_raw[1:-1]  # strip quotes
        if "\\" in key:
            key = json.loads(key_raw)

        # Colon
        pos = _skip_ws(s, pos)
        if pos < len(s) and s[pos] == ':':
            pos += 1

        # Value (raw text)
        pos = _skip_ws(s, pos)
        val_raw, pos = _scan_json_value(s, pos)

        result[key] = val_raw
        keys.append(key)

        # Comma or end
        pos = _skip_ws(s, pos)
        if pos < len(s) and s[pos] == ',':
            pos += 1

    return result, pos, keys


# ---------------------------------------------------------------------------
# One-level flattening
# ---------------------------------------------------------------------------

def _flatten_record(
    top: Dict[str, str],
    top_keys: List[str],
) -> Tuple[Dict[str, str], List[str]]:
    """Flatten a parsed JSON object one level deep.

    Nested objects have their keys promoted with dot notation.
    Arrays and deeper nesting are kept as raw JSON strings.

    Returns:
        (flat_dict of column_name -> raw_value, ordered flat key list)
    """
    flat = {}
    flat_keys = []

    for key in top_keys:
        val = top[key]
        if val and val[0] == '{':
            # Nested object — flatten one level (unless empty)
            inner, _, inner_keys = _parse_object_raw(val)
            if inner_keys:
                for ik in inner_keys:
                    col_name = f"{key}.{ik}"
                    flat[col_name] = inner[ik]
                    flat_keys.append(col_name)
            else:
                # Empty object {} — keep as raw value
                flat[key] = val
                flat_keys.append(key)
        else:
            # Scalar, array, or anything else — keep as-is
            flat[key] = val
            flat_keys.append(key)

    return flat, flat_keys


# ---------------------------------------------------------------------------
# Structural fingerprint
# ---------------------------------------------------------------------------

def _fingerprint(flat_keys: List[str]) -> str:
    """Compute a structural fingerprint from flattened key names.

    This is a canonical sorted representation used as the group identity.
    """
    return "\x00".join(sorted(flat_keys))


# ---------------------------------------------------------------------------
# Superset merge (training time only)
# ---------------------------------------------------------------------------

_MERGE_OVERLAP_THRESHOLD = 0.65  # merge if intersection/union >= this


def _merge_similar_groups(
    group_fingerprints: Dict[str, set],
    group_records: Dict[str, list],
    group_columns: Dict[str, list],
    group_col_index: Dict[str, dict],
    group_key_orders: Dict[str, list],
) -> Dict[str, str]:
    """Merge groups with highly overlapping key sets.

    Returns a mapping from old fingerprint → new (merged) fingerprint.
    Only called during training — not on the compression fast path.
    """
    fps = list(group_fingerprints.keys())
    key_sets = {fp: group_fingerprints[fp] for fp in fps}

    # Build merge mapping: old_fp → canonical_fp
    merge_map: Dict[str, str] = {fp: fp for fp in fps}

    # Sort by record count descending — larger groups absorb smaller ones
    fps.sort(key=lambda fp: len(group_records.get(fp, [])), reverse=True)

    for i in range(len(fps)):
        fp_i = fps[i]
        canon_i = merge_map[fp_i]
        if canon_i != fp_i:
            continue  # already merged into something else

        ks_i = key_sets[fp_i]

        for j in range(i + 1, len(fps)):
            fp_j = fps[j]
            if merge_map[fp_j] != fp_j:
                continue  # already merged

            ks_j = key_sets[fp_j]

            # Check overlap
            intersection = len(ks_i & ks_j)
            union = len(ks_i | ks_j)
            if union == 0:
                continue
            overlap = intersection / union

            if overlap >= _MERGE_OVERLAP_THRESHOLD:
                merge_map[fp_j] = fp_i
                # Expand the absorbing group's key set
                ks_i = ks_i | ks_j
                key_sets[fp_i] = ks_i

    return merge_map


def _apply_merge(
    merge_map: Dict[str, str],
    group_records: Dict[str, list],
    group_columns: Dict[str, list],
    group_col_index: Dict[str, dict],
    group_key_orders: Dict[str, list],
    group_fingerprints: Dict[str, set],
    line_order: list,
):
    """Apply the merge mapping, consolidating groups in-place.

    Updates group_records, group_columns, group_col_index, group_key_orders,
    and line_order so that merged fingerprints point to the canonical group.

    After merging, records within each merged group are reordered to match
    their appearance order in line_order.  This ensures that consecutive
    same-group entries always have sequential indices, which is required
    for the RLE line_order encoding to be correct.
    """
    # Find which fingerprints were actually merged
    merges_needed = {old: new for old, new in merge_map.items() if old != new}
    if not merges_needed:
        return

    for old_fp, new_fp in merges_needed.items():
        if old_fp not in group_records:
            continue

        # Ensure canonical group exists
        if new_fp not in group_columns:
            group_columns[new_fp] = list(group_columns[old_fp])
            group_col_index[new_fp] = dict(group_col_index[old_fp])
            group_records[new_fp] = []
            group_key_orders[new_fp] = list(group_key_orders.get(old_fp, []))

        # Extend columns of the canonical group
        new_cols = group_columns[new_fp]
        new_idx = group_col_index[new_fp]
        old_cols = group_columns[old_fp]

        col_mapping = []
        for c in old_cols:
            if c not in new_idx:
                new_idx[c] = len(new_cols)
                new_cols.append(c)
            col_mapping.append(new_idx[c])

        # Remap and transfer records
        new_ncols = len(new_cols)
        offset = len(group_records[new_fp])
        for row in group_records[old_fp]:
            new_row = [""] * new_ncols
            for i, val in enumerate(row):
                if i < len(col_mapping):
                    new_row[col_mapping[i]] = val
            group_records[new_fp].append(new_row)

        # Transfer key orders
        if old_fp in group_key_orders:
            if new_fp not in group_key_orders:
                group_key_orders[new_fp] = []
            group_key_orders[new_fp].extend(group_key_orders[old_fp])

        # Update line_order: remap old_fp → new_fp with offset
        for i, (fp, idx) in enumerate(line_order):
            if fp == old_fp:
                line_order[i] = (new_fp, idx + offset)

        # Merge fingerprint key sets
        if old_fp in group_fingerprints:
            if new_fp not in group_fingerprints:
                group_fingerprints[new_fp] = set()
            group_fingerprints[new_fp] |= group_fingerprints[old_fp]

        # Clean up old group
        del group_records[old_fp]
        del group_columns[old_fp]
        del group_col_index[old_fp]
        if old_fp in group_key_orders:
            del group_key_orders[old_fp]
        if old_fp in group_fingerprints:
            del group_fingerprints[old_fp]

    # --- Reorder records within each merged group to match line_order ---
    # This is necessary because after merging, interleaved records from
    # different original groups have non-sequential indices.  The RLE
    # encoding assumes sequential indices for consecutive same-group entries.
    affected_groups = set(merges_needed.values())
    for gid in affected_groups:
        if gid not in group_records:
            continue

        # Collect all (line_order_position, current_record_index) for this group
        occurrences = []
        for lo_pos, (fp, idx) in enumerate(line_order):
            if fp == gid:
                occurrences.append((lo_pos, idx))

        if not occurrences:
            continue

        # Build new record list in line_order appearance order
        old_records = group_records[gid]
        old_key_orders = group_key_orders.get(gid, [])
        new_records = []
        new_key_orders = []
        # old_idx → new_idx mapping
        for _, old_idx in occurrences:
            new_idx = len(new_records)
            new_records.append(old_records[old_idx])
            if old_idx < len(old_key_orders):
                new_key_orders.append(old_key_orders[old_idx])

        group_records[gid] = new_records
        if old_key_orders:
            group_key_orders[gid] = new_key_orders

        # Update line_order indices to be sequential (0, 1, 2, ...)
        new_idx_counter = 0
        for lo_pos, _ in occurrences:
            line_order[lo_pos] = (gid, new_idx_counter)
            new_idx_counter += 1


# ---------------------------------------------------------------------------
# Schema / manifest (mirrors telemetry_codec design)
# ---------------------------------------------------------------------------

def extract_schema(
    group_columns: Dict[str, List[str]],
    group_key_orders: Dict[str, List[str]],
    group_record_counts: Dict[str, int],
    group_fingerprints: Dict[str, set],
) -> dict:
    """Build a reusable schema from training data.

    The schema captures:
      - group definitions (fingerprint → columns, key orderings)
      - group_index for compact binary manifest references
    """
    schema = {"version": 6, "codec": "jsonl_generic", "groups": {}}
    sorted_groups = sorted(group_columns.keys())
    schema["group_index"] = {g: i for i, g in enumerate(sorted_groups)}

    for gid in sorted_groups:
        cols = group_columns[gid]

        # Collect unique key orderings
        key_orders = group_key_orders.get(gid, [])
        unique_orders = list(dict.fromkeys(
            "\x01".join(ko) if isinstance(ko, list) else ko
            for ko in key_orders
        ))

        ginfo = {
            "columns": cols,
            "fingerprint_keys": sorted(group_fingerprints.get(gid, set())),
        }

        if len(unique_orders) == 1:
            # Uniform key order — store once, no per-row index needed
            order_str = unique_orders[0]
            ginfo["key_order"] = order_str.split("\x01") if "\x01" in order_str else [order_str]
        else:
            ginfo["key_orders"] = [
                o.split("\x01") if "\x01" in o else [o]
                for o in unique_orders
            ]

        schema["groups"][gid] = ginfo

    return schema


def save_schema(schema: dict, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(schema, f, separators=(",", ":"))


def load_schema(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Binary manifest (compact per-file routing)
# ---------------------------------------------------------------------------
#
# Format:
#   [4 bytes]  U32LE  source_lines
#   [1 byte]   U8     num_groups_present
#   For each group present (sorted by group id):
#     [2 bytes]  U16LE  group_index (references schema.group_index)
#     [4 bytes]  U32LE  record_count
#   line_order (binary RLE):
#     [4 bytes]  U32LE  num_runs
#     For each run:
#       [2 bytes]  U16LE  group_index (0xFFFF = __blank__)
#       [4 bytes]  U32LE  count
# ---------------------------------------------------------------------------

_BLANK_GROUP_IDX = 0xFFFF


def build_manifest(
    source_lines: int,
    group_record_counts: Dict[str, int],
    line_order_rle: list,
    group_index: Dict[str, int],
) -> bytes:
    buf = bytearray()
    buf += struct.pack("<I", source_lines)

    present = sorted(group_record_counts.keys())
    buf += struct.pack("<B", len(present))
    for gid in present:
        gidx = group_index[gid]
        rc = group_record_counts[gid]
        buf += struct.pack("<HI", gidx, rc)

    buf += struct.pack("<I", len(line_order_rle))
    for gid, count in line_order_rle:
        if gid == "__blank__":
            gidx = _BLANK_GROUP_IDX
        else:
            gidx = group_index[gid]
        buf += struct.pack("<HI", gidx, count)

    return bytes(buf)


def parse_manifest(
    data: bytes,
    index_to_group: Dict[int, str],
) -> Tuple[int, Dict[str, int], List[Tuple[str, int]]]:
    off = 0
    source_lines = struct.unpack_from("<I", data, off)[0]
    off += 4

    num_groups = struct.unpack_from("<B", data, off)[0]
    off += 1

    record_counts: Dict[str, int] = {}
    for _ in range(num_groups):
        gidx, rc = struct.unpack_from("<HI", data, off)
        off += 6
        gid = index_to_group[gidx]
        record_counts[gid] = rc

    num_runs = struct.unpack_from("<I", data, off)[0]
    off += 4

    group_counters: Dict[str, int] = {}
    line_order: List[Tuple[str, int]] = []
    _extend = line_order.extend
    for _ in range(num_runs):
        gidx, count = struct.unpack_from("<HI", data, off)
        off += 6
        if gidx == _BLANK_GROUP_IDX:
            gid = "__blank__"
        else:
            gid = index_to_group[gidx]
        start_idx = group_counters.get(gid, 0)
        _extend((gid, start_idx + i) for i in range(count))
        group_counters[gid] = start_idx + count

    return source_lines, record_counts, line_order


# ---------------------------------------------------------------------------
# RLE utilities
# ---------------------------------------------------------------------------

def _rle_encode(line_order: list) -> list:
    """RLE encode line_order into (group_id, count) runs."""
    if not line_order:
        return []
    result = []
    cur = line_order[0][0]
    count = 1
    for i in range(1, len(line_order)):
        g = line_order[i][0]
        if g == cur:
            count += 1
        else:
            result.append((cur, count))
            cur = g
            count = 1
    result.append((cur, count))
    return result


def _rle_decode(compact_order: list) -> List[Tuple[str, int]]:
    """Decode RLE line_order.  Handles both (g, count) and (g, start, count)."""
    result = []
    group_counters: Dict[str, int] = {}
    for entry in compact_order:
        if len(entry) == 3:
            gid, start_idx, count = entry
            for i in range(count):
                result.append((gid, start_idx + i))
        else:
            gid, count = entry
            start_idx = group_counters.get(gid, 0)
            for i in range(count):
                result.append((gid, start_idx + i))
            group_counters[gid] = start_idx + count
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KORDER_SEP = "\x01"

# TSV cell escaping: raw JSON string values start with " which OpenZL's CSV
# parser interprets as CSV quote-escaping, breaking column parsing.
#
# Solution: type-prefix scheme.  Every cell gets a 1-character prefix:
#   "s" → JSON string; the outer quotes are stripped, content follows
#   "r" → raw JSON literal (number, boolean, null, array, object)
#
# This guarantees no cell ever starts with " and is fully reversible.
# We also escape literal tab/newline bytes for TSV safety.
_TSV_ESC = "\x1e"
_TSV_ESC_B = b"\x1e"


def _tsv_escape(val: str) -> str:
    """Encode a raw JSON value for safe TSV storage.

    Uses a type-prefix: 's' for strings (outer quotes stripped),
    'r' for raw literals (numbers, bools, null, arrays, objects).
    Also escapes literal tab/newline bytes.
    """
    if not val:
        return "s"  # empty value → treat as empty string

    # Determine type and strip outer quotes for strings
    if val[0] == '"':
        # JSON string — strip outer quotes
        prefix = "s"
        inner = val[1:-1] if len(val) >= 2 and val[-1] == '"' else val[1:]
    else:
        # Raw literal (number, bool, null, array, object)
        prefix = "r"
        inner = val

    # Escape marker character first (for unambiguous reversal)
    if _TSV_ESC in inner:
        inner = inner.replace(_TSV_ESC, _TSV_ESC + _TSV_ESC)
    # Escape literal tab and newline bytes
    if "\t" in inner:
        inner = inner.replace("\t", _TSV_ESC + "T")
    if "\n" in inner:
        inner = inner.replace("\n", _TSV_ESC + "N")
    # Escape double-quote characters — OpenZL's CSV parser interprets
    # " anywhere in a cell as CSV quoting, breaking column parsing
    if '"' in inner:
        inner = inner.replace('"', _TSV_ESC + "Q")

    return prefix + inner


def _tsv_unescape(val: bytes) -> bytes:
    """Reverse _tsv_escape.  Operates on bytes for decoder performance.

    Returns the original raw JSON value text (with quotes for strings).
    """
    if not val:
        return b'""'  # empty → was empty string

    prefix = val[:1]
    rest = val[1:]

    # Unescape sequences: \x1eT→tab, \x1eN→newline, \x1eQ→", \x1e\x1e→\x1e
    if _TSV_ESC_B in rest:
        out = bytearray()
        i = 0
        while i < len(rest):
            if rest[i:i + 1] == _TSV_ESC_B and i + 1 < len(rest):
                nxt = rest[i + 1:i + 2]
                if nxt == b"T":
                    out.append(0x09)  # tab
                    i += 2
                    continue
                elif nxt == b"N":
                    out.append(0x0A)  # newline
                    i += 2
                    continue
                elif nxt == b"Q":
                    out.append(0x22)  # double quote
                    i += 2
                    continue
                elif nxt == _TSV_ESC_B:
                    out.extend(_TSV_ESC_B)
                    i += 2
                    continue
            out.append(rest[i])
            i += 1
        rest = bytes(out)

    if prefix == b"s":
        # Re-wrap with JSON string quotes
        return b'"' + rest + b'"'
    else:
        # Raw literal — return as-is
        return rest


def _safe_filename(name: str) -> str:
    """Sanitise a group fingerprint into a safe filename."""
    # Use a short hash of the fingerprint + a readable prefix
    import hashlib
    h = hashlib.md5(name.encode()).hexdigest()[:10]
    # Take first few key names for readability
    parts = name.split("\x00")
    prefix = "_".join(parts[:3])[:40]
    prefix = prefix.replace(".", "_").replace("/", "_").replace(" ", "_")
    return f"{prefix}_{h}" if prefix else h


# ---------------------------------------------------------------------------
# Encode
# ---------------------------------------------------------------------------

def encode(
    input_file: Path,
    output_dir: Path,
    schema: Optional[dict] = None,
    verbose: bool = False,
) -> Tuple[List[Path], Path, dict]:
    """Encode a JSONL file into structurally-grouped TSVs.

    If *schema* is provided (compression with pre-trained schema), records are
    mapped to known groups and unseen fields go to __extra__.  Writes a compact
    binary manifest.bin.

    If *schema* is None (training run), discovers groups from the data and
    writes a full meta.json.

    Returns:
        (list of TSV paths, routing_path, group_meta dict)
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    if schema is not None:
        return _encode_with_schema(input_file, output_dir, schema, verbose)
    else:
        return _encode_discover(input_file, output_dir, verbose)


def _detect_json_format(line: str) -> Tuple[str, str]:
    """Detect JSON separator style from a source line.

    Returns (kv_sep, item_sep) — e.g. (": ", ", ") for standard json.dumps
    or (":", ",") for compact format.
    """
    # Look for ": " (space after colon) in a key-value context
    # We check after the first key's closing quote
    kv_sep = ":"
    item_sep = ","
    # Find first ": pattern
    i = line.find('":')
    if i >= 0 and i + 2 < len(line) and line[i + 2] == ' ':
        kv_sep = ": "
    # Find first ", pattern (after a value)
    # Look for comma followed by space then quote (next key)
    j = line.find('", ')
    if j >= 0:
        item_sep = ", "
    return kv_sep, item_sep


def _encode_discover(
    input_file: Path,
    output_dir: Path,
    verbose: bool,
) -> Tuple[List[Path], Path, dict]:
    """Encode with structure discovery (training path)."""
    group_records: Dict[str, List[List[str]]] = {}
    group_columns: Dict[str, List[str]] = {}
    group_col_index: Dict[str, Dict[str, int]] = {}
    group_key_orders: Dict[str, List[str]] = {}
    group_fingerprints: Dict[str, set] = {}
    line_order: List[Tuple[str, int]] = []

    # Detect JSON formatting style from source for byte-exact reconstruction.
    json_sep_kv = ": "   # default: standard json.dumps
    json_sep_item = ", "  # default: standard json.dumps

    line_num = 0
    with open(input_file, "r", encoding="utf-8") as f:
        for raw_line in f:
            stripped = raw_line.rstrip("\n").rstrip("\r")
            if not stripped:
                line_order.append(("__blank__", 0))
                continue

            # Detect formatting from first non-blank line
            if line_num == 0:
                json_sep_kv, json_sep_item = _detect_json_format(stripped)

            top, _, top_keys = _parse_object_raw(stripped)
            if not top:
                raise JsonlCodecError(
                    f"Failed to parse JSON at line {line_num + 1}")

            # Flatten one level
            flat, flat_keys = _flatten_record(top, top_keys)

            # Structural fingerprint
            fp = _fingerprint(flat_keys)

            # Key order for this record (for round-trip reconstruction)
            korder = _KORDER_SEP.join(flat_keys)

            # Initialise group on first encounter
            if fp not in group_columns:
                group_columns[fp] = list(flat.keys())
                group_col_index[fp] = {
                    c: i for i, c in enumerate(group_columns[fp])
                }
                group_records[fp] = []
                group_key_orders[fp] = []
                group_fingerprints[fp] = set(flat_keys)

            # Schema evolution: extend columns for new keys
            cols = group_columns[fp]
            col_idx = group_col_index[fp]
            for k in flat:
                if k not in col_idx:
                    col_idx[k] = len(cols)
                    cols.append(k)

            # Build row
            row = [""] * len(cols)
            for k, v in flat.items():
                row[col_idx[k]] = v

            group_records[fp].append(row)
            group_key_orders[fp].append(korder)
            line_order.append((fp, len(group_records[fp]) - 1))
            line_num += 1

    # Superset merge (only during discovery/training)
    merge_map = _merge_similar_groups(
        group_fingerprints, group_records, group_columns,
        group_col_index, group_key_orders)
    _apply_merge(merge_map, group_records, group_columns,
                 group_col_index, group_key_orders, group_fingerprints,
                 line_order)

    # --- Post-process and write TSVs ---
    group_meta = {}
    tsv_paths = []

    for gid in sorted(group_records):
        records = group_records[gid]
        cols = list(group_columns[gid])
        ncols = len(cols)

        # Pad short rows
        for row in records:
            if len(row) < ncols:
                row.extend([""] * (ncols - len(row)))

        # Collect unique key orderings
        korders = group_key_orders.get(gid, [])
        unique_orders = list(dict.fromkeys(korders))

        ginfo = {
            "record_count": len(records),
            "columns": cols,
            "fingerprint_keys": sorted(group_fingerprints.get(gid, set())),
        }

        if len(unique_orders) == 1:
            # Uniform key order — store once
            ginfo["key_order"] = unique_orders[0]
            # No __koidx__ column needed
        else:
            # Multiple key orderings — add __koidx__ column
            ginfo["key_orders"] = unique_orders
            order_map = {o: str(i) for i, o in enumerate(unique_orders)}

            koidx_col = len(cols)
            cols.append("__koidx__")
            ginfo["columns"] = cols

            for i, row in enumerate(records):
                if len(row) < len(cols):
                    row.extend([""] * (len(cols) - len(row)))
                row[koidx_col] = order_map[korders[i]]

        safe_name = _safe_filename(gid)
        tsv_name = f"{safe_name}.tsv"
        ginfo["tsv_file"] = tsv_name

        tsv_path = output_dir / tsv_name
        with open(tsv_path, "w", encoding="utf-8", newline="") as out:
            out.write("\t".join(cols))
            out.write("\n")
            for row in records:
                out.write("\t".join(_tsv_escape(v) for v in row))
                out.write("\n")

        tsv_paths.append(tsv_path)
        group_meta[gid] = ginfo

        if verbose:
            print(f"  [jsonl_codec] group {safe_name}: "
                  f"{len(records)} records -> {tsv_name}")

    # Write meta.json
    compact_order = _rle_encode(line_order)
    meta = {
        "version": 6,
        "codec": "jsonl_generic",
        "source_lines": line_num,
        "groups": group_meta,
        "line_order": compact_order,
        "json_kv_sep": json_sep_kv,
        "json_item_sep": json_sep_item,
    }
    meta_path = output_dir / "meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, separators=(",", ":"))

    if verbose:
        total_tsv = sum(p.stat().st_size for p in tsv_paths)
        orig = input_file.stat().st_size
        print(f"  [jsonl_codec] {len(group_meta)} groups, {line_num} records, "
              f"TSV total {total_tsv:,} bytes "
              f"({total_tsv / orig * 100:.1f}% of original)")

    return sorted(tsv_paths), meta_path, group_meta


def _encode_with_schema(
    input_file: Path,
    output_dir: Path,
    schema: dict,
    verbose: bool,
) -> Tuple[List[Path], Path, dict]:
    """Encode using a pre-built schema (compression fast path)."""
    schema_groups = schema["groups"]
    group_index = dict(schema["group_index"])

    # Build lookup: fingerprint_key_set → group_id for fast matching
    fp_to_gid: Dict[frozenset, str] = {}
    gid_columns: Dict[str, List[str]] = {}
    gid_col_index: Dict[str, Dict[str, int]] = {}
    gid_records: Dict[str, List[List[str]]] = {}

    for gid, ginfo in schema_groups.items():
        fk = frozenset(ginfo["fingerprint_keys"])
        fp_to_gid[fk] = gid
        cols = list(ginfo["columns"])
        # Ensure __extra__ column exists
        if "__extra__" not in cols:
            cols.append("__extra__")
        gid_columns[gid] = cols
        gid_col_index[gid] = {c: i for i, c in enumerate(cols)}
        gid_records[gid] = []

    # Pre-compute group key sets for subset matching
    gid_key_sets: Dict[str, frozenset] = {
        gid: frozenset(ginfo["fingerprint_keys"])
        for gid, ginfo in schema_groups.items()
    }

    line_order: List[Tuple[str, int]] = []
    group_key_orders: Dict[str, List[str]] = {}
    new_groups: Dict[str, dict] = {}  # groups not in schema (rare)
    group_fingerprints: Dict[str, set] = {}

    # Detect JSON formatting style
    json_sep_kv = ": "
    json_sep_item = ", "

    line_num = 0
    with open(input_file, "r", encoding="utf-8") as f:
        for raw_line in f:
            stripped = raw_line.rstrip("\n").rstrip("\r")
            if not stripped:
                line_order.append(("__blank__", 0))
                continue

            if line_num == 0:
                json_sep_kv, json_sep_item = _detect_json_format(stripped)

            top, _, top_keys = _parse_object_raw(stripped)
            if not top:
                raise JsonlCodecError(
                    f"Failed to parse JSON at line {line_num + 1}")

            flat, flat_keys = _flatten_record(top, top_keys)
            flat_set = frozenset(flat_keys)
            korder = _KORDER_SEP.join(flat_keys)

            # Try exact fingerprint match first
            gid = fp_to_gid.get(flat_set)

            if gid is None:
                # Try finding best matching group (most overlapping keys)
                best_gid = None
                best_overlap = 0
                for candidate_gid, candidate_keys in gid_key_sets.items():
                    overlap = len(flat_set & candidate_keys)
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_gid = candidate_gid
                # Accept if overlap is significant
                if best_gid and best_overlap >= len(flat_set) * 0.5:
                    gid = best_gid
                else:
                    # Create a new group (unseen structure)
                    fp = _fingerprint(flat_keys)
                    gid = fp
                    if gid not in gid_columns:
                        cols = list(flat.keys()) + ["__extra__", "__koidx__"]
                        gid_columns[gid] = cols
                        gid_col_index[gid] = {c: i for i, c in enumerate(cols)}
                        gid_records[gid] = []
                        group_key_orders[gid] = []
                        group_fingerprints[gid] = set(flat_keys)
                        new_groups[gid] = {
                            "columns": cols,
                            "fingerprint_keys": sorted(flat_keys),
                        }
                        # Assign a new group_index
                        next_idx = max(group_index.values()) + 1 if group_index else 0
                        group_index[gid] = next_idx

            # Build row
            cols = gid_columns[gid]
            col_idx = gid_col_index[gid]

            # Separate known vs extra fields
            extra_pairs = []
            row = [""] * len(cols)
            for k, v in flat.items():
                if k in col_idx:
                    row[col_idx[k]] = v
                else:
                    extra_pairs.append((k, v))

            # Store extra fields as raw JSON in __extra__
            if extra_pairs:
                extra_json = "{" + ",".join(
                    f'"{k}":{v}' for k, v in extra_pairs
                ) + "}"
                extra_idx = col_idx.get("__extra__")
                if extra_idx is not None:
                    row[extra_idx] = extra_json

            # Key order
            if gid not in group_key_orders:
                group_key_orders[gid] = []
            group_key_orders[gid].append(korder)

            gid_records[gid].append(row)
            line_order.append((gid, len(gid_records[gid]) - 1))
            line_num += 1

    # Write TSVs
    group_meta = {}
    tsv_paths = []

    for gid in sorted(gid_records):
        records = gid_records[gid]
        if not records:
            continue

        cols = gid_columns[gid]
        ncols = len(cols)

        for row in records:
            if len(row) < ncols:
                row.extend([""] * (ncols - len(row)))

        # Key order optimization
        korders = group_key_orders.get(gid, [])
        unique_orders = list(dict.fromkeys(korders))

        ginfo = {
            "record_count": len(records),
            "columns": cols,
        }

        if gid in schema_groups:
            ginfo["fingerprint_keys"] = schema_groups[gid]["fingerprint_keys"]
        elif gid in new_groups:
            ginfo["fingerprint_keys"] = new_groups[gid]["fingerprint_keys"]
        else:
            ginfo["fingerprint_keys"] = sorted(
                group_fingerprints.get(gid, set()))

        if len(unique_orders) <= 1:
            if unique_orders:
                ginfo["key_order"] = unique_orders[0]
        else:
            # Add __koidx__ column
            order_map = {o: str(i) for i, o in enumerate(unique_orders)}
            ginfo["key_orders"] = unique_orders

            if "__koidx__" not in {c for c in cols}:
                koidx_col = len(cols)
                cols.append("__koidx__")
                ginfo["columns"] = cols
                for row in records:
                    if len(row) <= koidx_col:
                        row.extend([""] * (koidx_col + 1 - len(row)))
            else:
                koidx_col = cols.index("__koidx__")

            for i, row in enumerate(records):
                if i < len(korders):
                    row[koidx_col] = order_map.get(korders[i], "0")

        safe_name = _safe_filename(gid)
        tsv_name = f"{safe_name}.tsv"
        ginfo["tsv_file"] = tsv_name

        tsv_path = output_dir / tsv_name
        with open(tsv_path, "w", encoding="utf-8", newline="") as out:
            out.write("\t".join(cols))
            out.write("\n")
            for row in records:
                out.write("\t".join(_tsv_escape(v) for v in row))
                out.write("\n")

        tsv_paths.append(tsv_path)
        group_meta[gid] = ginfo

    # Write manifest + schema
    compact_order = _rle_encode(line_order)

    # Extend schema with new groups
    # Build the extended schema for the container: merge training schema
    # with per-file group_meta (which has tsv_file, columns, key_order, etc.)
    extended_schema = dict(schema)
    extended_groups = {}
    for gid, gm in group_meta.items():
        # Start from training schema info if available
        base = dict(schema_groups.get(gid, {}))
        # Override with per-file info (tsv_file, columns, key_order/key_orders)
        base.update(gm)
        extended_groups[gid] = base
    # Also include any new groups not in the original schema
    for gid, ginfo in new_groups.items():
        if gid not in extended_groups:
            extended_groups[gid] = ginfo
    extended_schema["groups"] = extended_groups
    extended_schema["group_index"] = group_index
    extended_schema["json_kv_sep"] = json_sep_kv
    extended_schema["json_item_sep"] = json_sep_item

    manifest_data = build_manifest(
        line_num, {gid: gm["record_count"] for gid, gm in group_meta.items()},
        compact_order, group_index)
    manifest_path = output_dir / "manifest.bin"
    with open(manifest_path, "wb") as f:
        f.write(manifest_data)

    schema_path = output_dir / "schema.json"
    save_schema(extended_schema, schema_path)

    if verbose:
        total_tsv = sum(p.stat().st_size for p in tsv_paths)
        orig = input_file.stat().st_size
        print(f"  [jsonl_codec] {len(group_meta)} groups, {line_num} records, "
              f"TSV total {total_tsv:,} bytes "
              f"({total_tsv / orig * 100:.1f}% of original)")

    return sorted(tsv_paths), manifest_path, group_meta


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------

def decode(
    input_dir: Path,
    output_file: Path,
    verbose: bool = False,
) -> Path:
    """Decode structurally-grouped TSVs back to original JSONL.

    Supports:
      - v6: schema.json + manifest.bin (generic codec)
      - v6: meta.json (generic codec, training run)
      - v3: legacy meta.json (old type-based codec, backward compat)
    """
    schema_path = input_dir / "schema.json"
    manifest_path = input_dir / "manifest.bin"
    meta_path = input_dir / "meta.json"

    if schema_path.exists() and manifest_path.exists():
        # Check if this is actually our generic codec or telemetry
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        if schema.get("codec") == "jsonl_generic":
            return _decode_v6_manifest(input_dir, output_file, schema,
                                       manifest_path, verbose)
        # Not ours — fall through to meta.json
    if meta_path.exists():
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        if meta.get("codec") == "jsonl_generic":
            return _decode_v6_meta(input_dir, output_file, meta, verbose)
        else:
            # Legacy v3 format
            return _decode_v3(input_dir, output_file, meta, verbose)

    raise JsonlCodecError(
        f"No schema.json+manifest.bin or meta.json found in {input_dir}")


def _load_tsv_rows(tsv_file: Path) -> List[List[bytes]]:
    """Bulk-read a TSV file into rows of bytes, skipping the header.

    Applies _tsv_unescape to every cell to reverse the escaping done during
    encoding (quote-prefix, embedded tab/newline escapes).
    """
    raw = tsv_file.read_bytes()
    first_nl = raw.index(b"\n")
    body = raw[first_nl + 1:]
    if not body:
        return []
    rows = []
    for line in body.split(b"\n"):
        if line:
            rows.append([_tsv_unescape(cell) for cell in line.split(b"\t")])
    return rows


def _decode_v6_manifest(
    input_dir: Path,
    output_file: Path,
    schema: dict,
    manifest_path: Path,
    verbose: bool,
) -> Path:
    """Decode using v6 schema + binary manifest."""
    index_to_group = {i: g for g, i in schema["group_index"].items()}
    manifest_data = manifest_path.read_bytes()
    source_lines, record_counts, line_order = parse_manifest(
        manifest_data, index_to_group)

    group_schema = schema["groups"]
    group_data, group_cols = _load_group_tsvs(
        input_dir, group_schema, record_counts)

    kv_sep = schema.get("json_kv_sep", ": ")
    item_sep = schema.get("json_item_sep", ", ")
    _write_jsonl(output_file, line_order, group_schema, group_data,
                 group_cols, verbose, json_kv_sep=kv_sep, json_item_sep=item_sep)
    return output_file


def _decode_v6_meta(
    input_dir: Path,
    output_file: Path,
    meta: dict,
    verbose: bool,
) -> Path:
    """Decode using v6 meta.json (training run output)."""
    group_info = meta["groups"]
    group_data: Dict[str, List[List[bytes]]] = {}
    group_cols: Dict[str, List[str]] = {}

    for gid, ginfo in group_info.items():
        tsv_file = input_dir / ginfo["tsv_file"]
        if not tsv_file.exists():
            raise JsonlCodecError(f"TSV file not found: {tsv_file}")
        group_cols[gid] = ginfo["columns"]
        group_data[gid] = _load_tsv_rows(tsv_file)

    line_order = _rle_decode(meta["line_order"])
    kv_sep = meta.get("json_kv_sep", ": ")
    item_sep = meta.get("json_item_sep", ", ")
    _write_jsonl(output_file, line_order, group_info, group_data,
                 group_cols, verbose, json_kv_sep=kv_sep, json_item_sep=item_sep)
    return output_file


def _decode_v3(
    input_dir: Path,
    output_file: Path,
    meta: dict,
    verbose: bool,
) -> Path:
    """Decode legacy v3 meta.json (old type-based codec).

    This provides backward compatibility for archives created by the
    old jsonl_codec that used hardcoded envelope/content field splitting.
    """
    type_meta = meta["types"]
    type_data: Dict[str, List[List[str]]] = {}
    type_cols: Dict[str, List[str]] = {}

    _ENVELOPE_FIELDS_SET = frozenset([
        "creator", "current-time", "host-name",
        "key", "node-id", "server-start-time", "type",
    ])
    _CKEYS_SEP = ","

    for type_name, tinfo in type_meta.items():
        tsv_file = input_dir / tinfo["tsv_file"]
        if not tsv_file.exists():
            raise JsonlCodecError(f"TSV file not found: {tsv_file}")
        columns = tinfo["columns"]
        type_cols[type_name] = columns
        records = []
        with open(tsv_file, "r", encoding="utf-8") as fh:
            fh.readline()  # skip header
            for line in fh:
                records.append(line.rstrip("\n").split("\t"))
        type_data[type_name] = records

    line_order_raw = meta["line_order"]
    line_order = []
    for entry in line_order_raw:
        record_type, start_idx, count = entry
        for i in range(count):
            line_order.append((record_type, start_idx + i))

    with open(output_file, "w", encoding="utf-8", newline="") as out:
        for record_type, idx in line_order:
            if record_type == "__blank__":
                out.write("\n")
                continue

            tinfo = type_meta[record_type]
            columns = type_cols[record_type]
            field_order = tinfo.get("field_order", [])
            row = type_data[record_type][idx]

            cell = {}
            for i, col in enumerate(columns):
                cell[col] = row[i] if i < len(row) else ""

            ckeys_raw = cell.get("__ckeys__", "")
            content_keys = ckeys_raw.split(_CKEYS_SEP) if ckeys_raw else []

            pairs = []
            for field_name in field_order:
                if field_name == "type":
                    pairs.append(f'"type":"{record_type}"')
                elif field_name == "content":
                    cpairs = []
                    for fname in content_keys:
                        val = cell.get(f"c.{fname}", "")
                        if val:
                            cpairs.append(f'"{fname}":{val}')
                    pairs.append('"content":{' + ",".join(cpairs) + "}")
                elif field_name in _ENVELOPE_FIELDS_SET:
                    val = cell.get(field_name, "")
                    if val:
                        pairs.append(f'"{field_name}":{val}')
                else:
                    ekey = f"e.{field_name}"
                    val = cell.get(ekey, "")
                    if val:
                        pairs.append(f'"{field_name}":{val}')

            out.write("{" + ",".join(pairs) + "}\n")

    if verbose:
        total = sum(len(recs) for recs in type_data.values())
        print(f"  [jsonl_codec] Reconstructed {total} records -> {output_file.name}")

    return output_file


def _load_group_tsvs(
    input_dir: Path,
    group_schema: dict,
    record_counts: Dict[str, int],
) -> Tuple[Dict[str, List[List[bytes]]], Dict[str, List[str]]]:
    """Load TSV data for groups that are present in this file."""
    group_data: Dict[str, List[List[bytes]]] = {}
    group_cols: Dict[str, List[str]] = {}

    for gid, rc in record_counts.items():
        ginfo = group_schema[gid]
        tsv_file = input_dir / ginfo["tsv_file"]
        if not tsv_file.exists():
            raise JsonlCodecError(f"TSV file not found: {tsv_file}")
        group_cols[gid] = ginfo["columns"]
        group_data[gid] = _load_tsv_rows(tsv_file)

    return group_data, group_cols


def _write_jsonl(
    output_file: Path,
    line_order: List[Tuple[str, int]],
    group_info_map: Dict[str, dict],
    group_data: Dict[str, list],
    group_cols: Dict[str, List[str]],
    verbose: bool,
    json_kv_sep: str = ": ",
    json_item_sep: str = ", ",
) -> None:
    """Reconstruct JSONL from columnar TSV data.

    Uses buffered binary I/O for performance.
    """
    _COMMA = json_item_sep.encode("utf-8")
    _KV_SEP = json_kv_sep.encode("utf-8")
    _NEST_OPEN = (json_kv_sep + "{").encode("utf-8")  # ": {" or ":{"
    _EMPTY = b""
    _OPEN = b"{"
    _CLOSE_NL = b"}\n"
    _NL = b"\n"
    _KORDER_SEP_B = _KORDER_SEP.encode("utf-8")
    _FLUSH_SIZE = 1 << 20  # 1 MiB

    # Pre-build reconstruction templates per group
    group_templates = {}
    for gid, ginfo in group_info_map.items():
        if gid not in group_data:
            continue
        columns = group_cols[gid]
        col_map = {c: i for i, c in enumerate(columns)}

        # Key order info
        has_uniform_order = "key_order" in ginfo
        if has_uniform_order:
            ko = ginfo["key_order"]
            if isinstance(ko, list):
                uniform_keys = ko
            else:
                uniform_keys = ko.split(_KORDER_SEP)
        else:
            uniform_keys = None

        key_orders_list = None
        koidx_col = None
        if "key_orders" in ginfo:
            key_orders_list = []
            for ko in ginfo["key_orders"]:
                if isinstance(ko, list):
                    key_orders_list.append(ko)
                else:
                    key_orders_list.append(ko.split(_KORDER_SEP))
            koidx_col = col_map.get("__koidx__", -1)

        extra_col = col_map.get("__extra__", -1)

        group_templates[gid] = {
            "col_map": col_map,
            "uniform_keys": uniform_keys,
            "key_orders_list": key_orders_list,
            "koidx_col": koidx_col,
            "extra_col": extra_col,
            "columns": columns,
        }

    buf = bytearray()

    with open(output_file, "wb") as out:
        for gid, orig_idx in line_order:
            if gid == "__blank__":
                buf.extend(_NL)
                if len(buf) >= _FLUSH_SIZE:
                    out.write(buf)
                    buf.clear()
                continue

            tmpl = group_templates[gid]
            row = group_data[gid][orig_idx]
            col_map = tmpl["col_map"]
            columns = tmpl["columns"]

            # Determine key order for this record
            if tmpl["uniform_keys"] is not None:
                flat_keys = tmpl["uniform_keys"]
            elif tmpl["key_orders_list"] is not None:
                koidx_col = tmpl["koidx_col"]
                koidx_val = row[koidx_col] if koidx_col >= 0 and koidx_col < len(row) else b"0"
                koidx = int(koidx_val) if koidx_val else 0
                flat_keys = tmpl["key_orders_list"][koidx]
            else:
                # Fallback: use columns in order (minus special columns)
                flat_keys = [c for c in columns
                             if c not in ("__koidx__", "__extra__")]

            # Reconstruct JSON object
            parts = [_OPEN]
            first = True

            # Track which columns have been written (for __extra__)
            for key in flat_keys:
                # Check if this is a dot-notation key (nested object)
                dot_pos = key.find(".")
                if dot_pos == -1:
                    # Simple field
                    cidx = col_map.get(key, -1)
                    if cidx >= 0 and cidx < len(row):
                        val = row[cidx]
                        if val:
                            if not first:
                                parts.append(_COMMA)
                            parts.append(b'"')
                            parts.append(key.encode("utf-8") if isinstance(key, str) else key)
                            parts.append(b'":')
                            parts.append(val if isinstance(val, bytes) else val.encode("utf-8"))
                            first = False
                else:
                    # Dot-notation: need to reconstruct nested object
                    # Collect all subkeys for this parent
                    parent = key[:dot_pos]
                    # Find all columns that start with parent.
                    sub_parts = []
                    prefix = parent + "."
                    # Walk remaining flat_keys to collect all from same parent
                    # (they should be contiguous due to original key order)
                    # But we handle this by emitting the parent object when we
                    # encounter the first subkey, then skipping subsequent ones.
                    # Check if this is the first subkey for this parent
                    child = key[dot_pos + 1:]
                    cidx = col_map.get(key, -1)
                    if cidx >= 0 and cidx < len(row):
                        val = row[cidx]
                        if val:
                            sub_parts.append((child, val))

                    # Peek ahead in flat_keys for more subkeys of same parent
                    # We need to handle this more carefully - collect all subkeys
                    # and emit the nested object all at once. We'll use a flag
                    # to skip subkeys after the first.
                    pass  # handled below

            # The above naive approach doesn't handle nested reconstruction well.
            # Let's use a proper reconstruction strategy.
            parts = [_OPEN]
            first = True

            # Group flat_keys by parent for nested reconstruction
            i = 0
            while i < len(flat_keys):
                key = flat_keys[i]
                dot_pos = key.find(".")

                if dot_pos == -1:
                    # Simple field
                    cidx = col_map.get(key, -1)
                    if cidx >= 0 and cidx < len(row):
                        val = row[cidx]
                        if val:
                            if not first:
                                parts.append(_COMMA)
                            parts.append(b'"')
                            parts.append(key.encode("utf-8") if isinstance(key, str) else key)
                            parts.append(b'"')
                            parts.append(_KV_SEP)
                            parts.append(val if isinstance(val, bytes) else val.encode("utf-8"))
                            first = False
                    i += 1
                else:
                    # Nested object: collect all consecutive subkeys for this parent
                    parent = key[:dot_pos]
                    prefix = parent + "."
                    sub_pairs = []

                    while i < len(flat_keys) and flat_keys[i].startswith(prefix):
                        child = flat_keys[i][len(prefix):]
                        cidx = col_map.get(flat_keys[i], -1)
                        if cidx >= 0 and cidx < len(row):
                            val = row[cidx]
                            if val:
                                sub_pairs.append((child, val))
                        i += 1

                    if sub_pairs:
                        if not first:
                            parts.append(_COMMA)
                        parts.append(b'"')
                        parts.append(parent.encode("utf-8"))
                        parts.append(b'"')
                        parts.append(_NEST_OPEN)
                        for si, (child, val) in enumerate(sub_pairs):
                            if si > 0:
                                parts.append(_COMMA)
                            parts.append(b'"')
                            parts.append(child.encode("utf-8") if isinstance(child, str) else child)
                            parts.append(b'"')
                            parts.append(_KV_SEP)
                            parts.append(val if isinstance(val, bytes) else val.encode("utf-8"))
                        parts.append(b"}")
                        first = False

            # Append __extra__ fields
            extra_col = tmpl["extra_col"]
            if extra_col >= 0 and extra_col < len(row):
                extra_val = row[extra_col]
                if extra_val and extra_val != _EMPTY:
                    # Extra is a raw JSON object like {"k1":v1,"k2":v2}
                    # Strip the outer braces and append the pairs
                    extra_str = extra_val if isinstance(extra_val, bytes) else extra_val.encode("utf-8")
                    if extra_str.startswith(b"{") and extra_str.endswith(b"}"):
                        inner = extra_str[1:-1]
                        if inner:
                            if not first:
                                parts.append(_COMMA)
                            parts.append(inner)
                            first = False

            parts.append(_CLOSE_NL)
            buf.extend(b"".join(parts))

            if len(buf) >= _FLUSH_SIZE:
                out.write(buf)
                buf.clear()

        if buf:
            out.write(buf)

    if verbose:
        total = sum(len(recs) for recs in group_data.values())
        print(f"  [jsonl_codec] Reconstructed {total} records -> {output_file.name}")
