"""JSONL codec: lossless encode/decode between JSONL and type-grouped TSVs.

Encode: Parse JSONL -> group records by 'type' field -> write one TSV per type
        + meta.json for column schemas, line ordering, and byte-exact round-trip
        reconstruction.

Decode: Read TSVs + meta.json -> reconstruct exact original JSONL.

Strategy: values are stored as their RAW JSON text extracted directly from the
original line (never round-tripped through Python float/int).  A __ckeys__
column records the ordered content-field names per record so different content
schemas within the same type survive the round-trip.
"""

import json
from pathlib import Path
from typing import Dict, List, Tuple


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
        # String
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
        key = key_raw[1:-1]  # strip quotes (simple keys, no escapes expected)
        # Handle escaped key names
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
# Helpers
# ---------------------------------------------------------------------------

_ENVELOPE_FIELDS_SET = frozenset([
    "creator", "current-time", "host-name",
    "key", "node-id", "server-start-time", "type",
])

_CKEYS_SEP = ","


# ---------------------------------------------------------------------------
# Encode
# ---------------------------------------------------------------------------

def encode(
    input_file: Path,
    output_dir: Path,
    verbose: bool = False,
) -> Tuple[List[Path], Path]:
    """Encode a JSONL file into type-grouped TSV files + meta.json.

    Returns:
        Tuple of (list of TSV file paths, path to meta.json).
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    type_records: Dict[str, List[List[str]]] = {}
    type_columns: Dict[str, List[str]] = {}
    type_col_index: Dict[str, Dict[str, int]] = {}
    line_order: List[Tuple[str, int]] = []
    type_field_orders: Dict[str, List[str]] = {}

    line_num = 0
    with open(input_file, "r", encoding="utf-8") as f:
        for raw_line in f:
            stripped = raw_line.rstrip("\n").rstrip("\r")
            if not stripped:
                line_order.append(("__blank__", 0))
                continue

            # Raw-parse the top-level object
            top, _, top_keys = _parse_object_raw(stripped)
            if not top:
                raise JsonlCodecError(
                    f"Failed to parse JSON at line {line_num + 1}"
                )

            # Extract type
            type_raw = top.get("type", '"__notype__"')
            # Unquote if string
            if type_raw.startswith('"'):
                record_type = type_raw[1:-1]
            else:
                record_type = type_raw

            # Build row cells: all values as raw JSON text
            row_dict: Dict[str, str] = {}

            # Envelope fields
            for k in top_keys:
                if k == "type":
                    continue
                if k == "content":
                    continue
                if k in _ENVELOPE_FIELDS_SET:
                    row_dict[k] = top[k]
                else:
                    row_dict[f"e.{k}"] = top[k]

            # Content: raw-parse the content object
            content_raw = top.get("content")
            content_keys: List[str] = []
            if content_raw is not None:
                if content_raw.startswith("{"):
                    content_obj, _, content_keys = _parse_object_raw(content_raw)
                    for ck in content_keys:
                        row_dict[f"c.{ck}"] = content_obj[ck]
                else:
                    # Non-object content (array, scalar)
                    row_dict["c.__raw__"] = content_raw
                    content_keys = ["__raw__"]

            # Content key order column
            row_dict["__ckeys__"] = _CKEYS_SEP.join(content_keys)

            # Initialize type schema on first record
            if record_type not in type_columns:
                type_columns[record_type] = list(row_dict.keys())
                type_col_index[record_type] = {
                    c: i for i, c in enumerate(type_columns[record_type])
                }
                type_records[record_type] = []
                type_field_orders[record_type] = top_keys

            # Schema evolution
            cols = type_columns[record_type]
            col_idx = type_col_index[record_type]
            for k in row_dict:
                if k not in col_idx:
                    col_idx[k] = len(cols)
                    cols.append(k)

            # Build row array
            row = [""] * len(cols)
            for k, v in row_dict.items():
                row[col_idx[k]] = v

            idx = len(type_records[record_type])
            type_records[record_type].append(row)
            line_order.append((record_type, idx))
            line_num += 1

    # Write TSV files
    tsv_paths = []
    for record_type in sorted(type_records):
        records = type_records[record_type]
        cols = type_columns[record_type]
        safe_name = _safe_filename(record_type)
        tsv_path = output_dir / f"{safe_name}.tsv"
        ncols = len(cols)

        with open(tsv_path, "w", encoding="utf-8", newline="") as out:
            out.write("\t".join(cols))
            out.write("\n")
            for row in records:
                if len(row) < ncols:
                    row.extend([""] * (ncols - len(row)))
                out.write("\t".join(row))
                out.write("\n")

        tsv_paths.append(tsv_path)
        if verbose:
            print(
                f"  [jsonl_codec] {record_type}: "
                f"{len(records)} records -> {tsv_path.name}"
            )

    # Compact line ordering
    compact_order = _rle_encode(line_order)

    meta = {
        "version": 3,
        "source_lines": line_num,
        "types": {
            t: {
                "tsv_file": _safe_filename(t) + ".tsv",
                "columns": type_columns[t],
                "record_count": len(type_records[t]),
                "field_order": type_field_orders.get(t, []),
            }
            for t in sorted(type_records)
        },
        "line_order": compact_order,
    }

    meta_path = output_dir / "meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, separators=(",", ":"))

    if verbose:
        total_tsv = sum(p.stat().st_size for p in tsv_paths)
        orig = input_file.stat().st_size
        meta_sz = meta_path.stat().st_size
        print(
            f"  [jsonl_codec] {len(type_records)} types, "
            f"{line_num} records, "
            f"TSV total {total_tsv:,} bytes "
            f"({total_tsv / orig * 100:.1f}% of original), "
            f"meta {meta_sz:,} bytes"
        )

    return sorted(tsv_paths), meta_path


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------

def decode(
    input_dir: Path,
    output_file: Path,
    verbose: bool = False,
) -> Path:
    """Decode type-grouped TSVs + meta.json back to original JSONL."""
    meta_path = input_dir / "meta.json"
    if not meta_path.exists():
        raise JsonlCodecError(f"meta.json not found in {input_dir}")

    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    if meta.get("version", 3) not in (2, 3):
        raise JsonlCodecError(f"Unsupported meta version: {meta['version']}")

    type_meta = meta["types"]
    type_data: Dict[str, List[List[str]]] = {}
    type_cols: Dict[str, List[str]] = {}

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

    # Reconstruct JSONL
    line_order = _rle_decode(meta["line_order"])

    with open(output_file, "w", encoding="utf-8", newline="") as out:
        for record_type, idx in line_order:
            if record_type == "__blank__":
                out.write("\n")
                continue

            tinfo = type_meta[record_type]
            columns = type_cols[record_type]
            field_order = tinfo.get("field_order", [])
            row = type_data[record_type][idx]

            # Column lookup
            cell: Dict[str, str] = {}
            for i, col in enumerate(columns):
                cell[col] = row[i] if i < len(row) else ""

            # Content key order for this specific record
            ckeys_raw = cell.get("__ckeys__", "")
            content_keys = ckeys_raw.split(_CKEYS_SEP) if ckeys_raw else []

            # Rebuild JSON in original key order
            pairs: List[str] = []
            for field_name in field_order:
                if field_name == "type":
                    pairs.append(f'"type":"{record_type}"')
                elif field_name == "content":
                    pairs.append(
                        '"content":'
                        + _rebuild_content(cell, content_keys)
                    )
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
        print(
            f"  [jsonl_codec] Reconstructed {total} records "
            f"-> {output_file.name}"
        )

    return output_file


def _rebuild_content(
    cell: Dict[str, str],
    content_keys: List[str],
) -> str:
    """Rebuild content as raw JSON string from TSV cells."""
    pairs = []
    for fname in content_keys:
        val = cell.get(f"c.{fname}", "")
        if val:
            pairs.append(f'"{fname}":{val}')
    return "{" + ",".join(pairs) + "}"


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def _safe_filename(type_name: str) -> str:
    return (
        type_name.replace(".", "_")
        .replace("/", "_")
        .replace(" ", "_")
    )


def _rle_encode(line_order: List[Tuple[str, int]]) -> List:
    """Run-length encode consecutive same-type sequences."""
    if not line_order:
        return []
    result = []
    cur_type, cur_start = line_order[0]
    count = 1
    for i in range(1, len(line_order)):
        t, idx = line_order[i]
        if t == cur_type and idx == cur_start + count:
            count += 1
        else:
            result.append([cur_type, cur_start, count])
            cur_type, cur_start = t, idx
            count = 1
    result.append([cur_type, cur_start, count])
    return result


def _rle_decode(compact_order: List) -> List[Tuple[str, int]]:
    """Decode run-length encoded line ordering."""
    result = []
    for record_type, start_idx, count in compact_order:
        for i in range(count):
            result.append((record_type, start_idx + i))
    return result
