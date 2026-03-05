"""Telemetry JSONL codec: optimized encode/decode for telemetry JSONL data.

Optimized fork of jsonl_codec for telemetry workloads (nom-telegraf, nom-link,
etc.).  Key differences from the generic codec:

  A1 — __ckeys__ elimination: uniform-schema types store the content key list
       once in the schema instead of repeating it per row.  Multi-schema types
       use a compact integer index (__skidx__) instead of the full key string.

  Schema/manifest split: static type definitions (columns, field_order,
  content_schema) are extracted into a reusable schema.json saved alongside
  the trained compressor.  Per-file metadata is reduced to a compact binary
  manifest (~KB instead of ~MB).

Meta version: 5.
"""

import json
import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class TelemetryCodecError(Exception):
    """Raised when telemetry codec encode/decode fails."""


# ---------------------------------------------------------------------------
# Raw JSON scanner — same as jsonl_codec (preserves exact numeric text)
# ---------------------------------------------------------------------------

def _skip_ws(s: str, pos: int) -> int:
    while pos < len(s) and s[pos] in " \t\r\n":
        pos += 1
    return pos


def _scan_json_value(s: str, pos: int) -> Tuple[str, int]:
    """Scan a single JSON value starting at *pos*."""
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

    end = pos
    while end < len(s) and s[end] not in ",}] \t\r\n":
        end += 1
    return s[pos:end], end


def _scan_bracket(s: str, pos: int, open_ch: str, close_ch: str) -> Tuple[str, int]:
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
    pos = _skip_ws(s, pos)
    if pos >= len(s) or s[pos] != '{':
        return {}, pos, []

    pos += 1
    result = {}
    keys = []

    while True:
        pos = _skip_ws(s, pos)
        if pos >= len(s):
            break
        if s[pos] == '}':
            pos += 1
            break

        key_raw, pos = _scan_json_value(s, pos)
        key = key_raw[1:-1]
        if "\\" in key:
            key = json.loads(key_raw)

        pos = _skip_ws(s, pos)
        if pos < len(s) and s[pos] == ':':
            pos += 1

        pos = _skip_ws(s, pos)
        val_raw, pos = _scan_json_value(s, pos)

        result[key] = val_raw
        keys.append(key)

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


def _safe_filename(type_name: str) -> str:
    return (
        type_name.replace(".", "_")
        .replace("/", "_")
        .replace(" ", "_")
    )


# ---------------------------------------------------------------------------
# Schema: static type definitions (shared across files)
# ---------------------------------------------------------------------------

def extract_schema(meta_types: Dict[str, dict]) -> dict:
    """Extract the static schema from per-type metadata.

    The schema contains everything that is constant across files of the same
    telemetry source: column names, field ordering, content schemas, TSV
    filenames.  It does NOT contain per-file data like record_count.
    """
    schema = {"version": 5, "codec": "telemetry", "types": {}}
    # Build type_index for compact binary references
    sorted_types = sorted(meta_types.keys())
    schema["type_index"] = {t: i for i, t in enumerate(sorted_types)}

    for type_name in sorted_types:
        tinfo = meta_types[type_name]
        stype = {
            "tsv_file": tinfo["tsv_file"],
            "field_order": tinfo["field_order"],
            "columns": tinfo["columns"],
        }
        if "content_schema" in tinfo:
            stype["content_schema"] = tinfo["content_schema"]
        if "content_schemas" in tinfo:
            stype["content_schemas"] = tinfo["content_schemas"]
        schema["types"][type_name] = stype

    return schema


def save_schema(schema: dict, path: Path) -> None:
    """Save schema to a JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(schema, f, separators=(",", ":"))


def load_schema(path: Path) -> dict:
    """Load schema from a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Manifest: compact binary per-file routing data
# ---------------------------------------------------------------------------
#
# Format:
#   [4 bytes]  U32LE  source_lines (total lines in original JSONL)
#   [1 byte]   U8     num_types_present
#   For each type present (sorted by type name):
#     [1 byte]   U8     type_index (references schema.type_index)
#     [4 bytes]  U32LE  record_count
#   line_order (binary RLE):
#     [4 bytes]  U32LE  num_runs
#     For each run:
#       [1 byte]   U8     type_index (0xFF = __blank__)
#       [4 bytes]  U32LE  start_idx
#       [4 bytes]  U32LE  count
# ---------------------------------------------------------------------------

_BLANK_TYPE_IDX = 0xFF


def build_manifest(
    source_lines: int,
    meta_types: Dict[str, dict],
    line_order_rle: List,
    type_index: Dict[str, int],
) -> bytes:
    """Build a compact binary manifest for per-file routing."""
    buf = bytearray()

    # Header
    buf += struct.pack("<I", source_lines)

    # Types present
    present_types = sorted(meta_types.keys())
    buf += struct.pack("<B", len(present_types))
    for type_name in present_types:
        tidx = type_index[type_name]
        rc = meta_types[type_name]["record_count"]
        buf += struct.pack("<BI", tidx, rc)

    # line_order RLE (binary)
    buf += struct.pack("<I", len(line_order_rle))
    for entry in line_order_rle:
        type_name, start_idx, count = entry[0], entry[1], entry[2]
        if type_name == "__blank__":
            tidx = _BLANK_TYPE_IDX
        else:
            tidx = type_index[type_name]
        buf += struct.pack("<BII", tidx, start_idx, count)

    return bytes(buf)


def parse_manifest(
    data: bytes,
    index_to_type: Dict[int, str],
) -> Tuple[int, Dict[str, int], List[Tuple[str, int]]]:
    """Parse a binary manifest.

    Returns:
        (source_lines, {type_name: record_count}, line_order as flat list)
    """
    off = 0
    source_lines = struct.unpack_from("<I", data, off)[0]
    off += 4

    num_types = struct.unpack_from("<B", data, off)[0]
    off += 1

    record_counts: Dict[str, int] = {}
    for _ in range(num_types):
        tidx, rc = struct.unpack_from("<BI", data, off)
        off += 5
        type_name = index_to_type[tidx]
        record_counts[type_name] = rc

    num_runs = struct.unpack_from("<I", data, off)[0]
    off += 4

    line_order: List[Tuple[str, int]] = []
    for _ in range(num_runs):
        tidx, start_idx, count = struct.unpack_from("<BII", data, off)
        off += 9
        if tidx == _BLANK_TYPE_IDX:
            type_name = "__blank__"
        else:
            type_name = index_to_type[tidx]
        for i in range(count):
            line_order.append((type_name, start_idx + i))

    return source_lines, record_counts, line_order


# ---------------------------------------------------------------------------
# Encode (optimized for telemetry)
# ---------------------------------------------------------------------------

def encode(
    input_file: Path,
    output_dir: Path,
    schema: Optional[dict] = None,
    verbose: bool = False,
) -> Tuple[List[Path], Path, dict]:
    """Encode a telemetry JSONL file into optimized type-grouped TSVs.

    If *schema* is provided, writes a compact binary manifest.bin instead of
    the full meta.json.  The schema is also embedded as schema.json in the
    output for self-contained decompression.

    If *schema* is None (first run / training), writes a full meta.json (v4
    format) and returns the meta_types dict so the caller can extract a schema.

    Returns:
        Tuple of (list of TSV paths, manifest_or_meta path, meta_types dict).
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

            top, _, top_keys = _parse_object_raw(stripped)
            if not top:
                raise TelemetryCodecError(
                    f"Failed to parse JSON at line {line_num + 1}"
                )

            type_raw = top.get("type", '"__notype__"')
            if type_raw.startswith('"'):
                record_type = type_raw[1:-1]
            else:
                record_type = type_raw

            row_dict: Dict[str, str] = {}

            for k in top_keys:
                if k == "type":
                    continue
                if k == "content":
                    continue
                if k in _ENVELOPE_FIELDS_SET:
                    row_dict[k] = top[k]
                else:
                    row_dict[f"e.{k}"] = top[k]

            content_raw = top.get("content")
            content_keys: List[str] = []
            if content_raw is not None:
                if content_raw.startswith("{"):
                    content_obj, _, content_keys = _parse_object_raw(content_raw)
                    for ck in content_keys:
                        row_dict[f"c.{ck}"] = content_obj[ck]
                else:
                    row_dict["c.__raw__"] = content_raw
                    content_keys = ["__raw__"]

            # Store __ckeys__ during accumulation (optimized later before write)
            row_dict["__ckeys__"] = _CKEYS_SEP.join(content_keys)

            if record_type not in type_columns:
                type_columns[record_type] = list(row_dict.keys())
                type_col_index[record_type] = {
                    c: i for i, c in enumerate(type_columns[record_type])
                }
                type_records[record_type] = []
                type_field_orders[record_type] = top_keys

            cols = type_columns[record_type]
            col_idx = type_col_index[record_type]
            for k in row_dict:
                if k not in col_idx:
                    col_idx[k] = len(cols)
                    cols.append(k)

            row = [""] * len(cols)
            for k, v in row_dict.items():
                row[col_idx[k]] = v

            idx = len(type_records[record_type])
            type_records[record_type].append(row)
            line_order.append((record_type, idx))
            line_num += 1

    # --- Post-processing: optimize before writing ---
    meta_types = {}
    tsv_paths = []

    for record_type in sorted(type_records):
        records = type_records[record_type]
        cols = list(type_columns[record_type])  # copy
        ncols = len(cols)

        # Pad short rows
        for row in records:
            if len(row) < ncols:
                row.extend([""] * (ncols - len(row)))

        type_info = {
            "tsv_file": _safe_filename(record_type) + ".tsv",
            "record_count": len(records),
            "field_order": type_field_orders.get(record_type, []),
        }

        # --- A1: __ckeys__ optimization ---
        ckeys_col_idx = cols.index("__ckeys__") if "__ckeys__" in cols else None

        if ckeys_col_idx is not None:
            unique_schemas = list(
                dict.fromkeys(row[ckeys_col_idx] for row in records)
            )

            if len(unique_schemas) == 1:
                # Uniform schema: store once, drop column
                type_info["content_schema"] = unique_schemas[0]
                cols = [c for i, c in enumerate(cols) if i != ckeys_col_idx]
                records = [
                    [v for i, v in enumerate(row) if i != ckeys_col_idx]
                    for row in records
                ]
            else:
                # Multi-schema: replace with compact index
                schema_map = {s: str(i) for i, s in enumerate(unique_schemas)}
                type_info["content_schemas"] = unique_schemas
                cols[ckeys_col_idx] = "__skidx__"
                for row in records:
                    row[ckeys_col_idx] = schema_map[row[ckeys_col_idx]]

        type_info["columns"] = cols

        meta_types[record_type] = type_info

        # Write TSV
        safe_name = _safe_filename(record_type)
        tsv_path = output_dir / f"{safe_name}.tsv"

        with open(tsv_path, "w", encoding="utf-8", newline="") as out:
            out.write("\t".join(cols))
            out.write("\n")
            for row in records:
                out.write("\t".join(row))
                out.write("\n")

        tsv_paths.append(tsv_path)
        if verbose:
            print(
                f"  [telemetry_codec] {record_type}: "
                f"{len(records)} records -> {tsv_path.name}"
            )

    # Compact line ordering (RLE)
    compact_order = _rle_encode(line_order)

    # Decide output format based on whether schema is provided
    if schema is not None:
        # Schema mode: write compact binary manifest
        type_index = schema["type_index"]
        manifest_data = build_manifest(
            line_num, meta_types, compact_order, type_index)
        manifest_path = output_dir / "manifest.bin"
        with open(manifest_path, "wb") as f:
            f.write(manifest_data)

        # Also write schema.json for self-contained decompression
        schema_path = output_dir / "schema.json"
        save_schema(schema, schema_path)

        routing_path = manifest_path
    else:
        # No schema yet (training run): write full meta.json (v4 format)
        meta = {
            "version": 4,
            "codec": "telemetry",
            "source_lines": line_num,
            "types": meta_types,
            "line_order": compact_order,
        }
        meta_path = output_dir / "meta.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, separators=(",", ":"))
        routing_path = meta_path

    if verbose:
        total_tsv = sum(p.stat().st_size for p in tsv_paths)
        orig = input_file.stat().st_size
        routing_sz = routing_path.stat().st_size
        print(
            f"  [telemetry_codec] {len(meta_types)} types, "
            f"{line_num} records, "
            f"TSV total {total_tsv:,} bytes "
            f"({total_tsv / orig * 100:.1f}% of original), "
            f"routing {routing_sz:,} bytes"
        )

    return sorted(tsv_paths), routing_path, meta_types


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------

def decode(
    input_dir: Path,
    output_file: Path,
    verbose: bool = False,
) -> Path:
    """Decode optimized type-grouped TSVs back to original JSONL.

    Supports three formats:
      - v5: schema.json + manifest.bin (compact binary)
      - v4: meta.json with full type info + JSON line_order
      - v3: legacy meta.json (fallback)
    """
    schema_path = input_dir / "schema.json"
    manifest_path = input_dir / "manifest.bin"
    meta_path = input_dir / "meta.json"

    if schema_path.exists() and manifest_path.exists():
        return _decode_v5(input_dir, output_file, schema_path,
                          manifest_path, verbose)
    elif meta_path.exists():
        return _decode_v4(input_dir, output_file, meta_path, verbose)
    else:
        raise TelemetryCodecError(
            f"No schema.json+manifest.bin or meta.json found in {input_dir}")


def _decode_v5(
    input_dir: Path,
    output_file: Path,
    schema_path: Path,
    manifest_path: Path,
    verbose: bool,
) -> Path:
    """Decode using v5 schema + binary manifest."""
    schema = load_schema(schema_path)
    index_to_type = {i: t for t, i in schema["type_index"].items()}

    manifest_data = manifest_path.read_bytes()
    source_lines, record_counts, line_order = parse_manifest(
        manifest_data, index_to_type)

    type_schema = schema["types"]

    # Load TSV data for types that are present in this file
    type_data: Dict[str, List[List[str]]] = {}
    type_cols: Dict[str, List[str]] = {}

    for type_name, rc in record_counts.items():
        tinfo = type_schema[type_name]
        tsv_file = input_dir / tinfo["tsv_file"]
        if not tsv_file.exists():
            raise TelemetryCodecError(f"TSV file not found: {tsv_file}")

        columns = tinfo["columns"]
        type_cols[type_name] = columns
        records = []

        with open(tsv_file, "r", encoding="utf-8") as fh:
            fh.readline()  # skip header
            for line in fh:
                records.append(line.rstrip("\n").split("\t"))

        type_data[type_name] = records

    # Reconstruct JSONL
    _write_jsonl(output_file, line_order, type_schema, type_data,
                 type_cols, verbose)
    return output_file


def _decode_v4(
    input_dir: Path,
    output_file: Path,
    meta_path: Path,
    verbose: bool,
) -> Path:
    """Decode using v4 meta.json (full JSON format)."""
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    version = meta.get("version", 3)
    if version not in (3, 4):
        raise TelemetryCodecError(f"Unsupported meta version: {version}")

    type_meta = meta["types"]
    type_data: Dict[str, List[List[str]]] = {}
    type_cols: Dict[str, List[str]] = {}

    for type_name, tinfo in type_meta.items():
        tsv_file = input_dir / tinfo["tsv_file"]
        if not tsv_file.exists():
            raise TelemetryCodecError(f"TSV file not found: {tsv_file}")

        columns = tinfo["columns"]
        type_cols[type_name] = columns
        records = []

        with open(tsv_file, "r", encoding="utf-8") as fh:
            fh.readline()  # skip header
            for line in fh:
                records.append(line.rstrip("\n").split("\t"))

        type_data[type_name] = records

    line_order = _rle_decode(meta["line_order"])
    _write_jsonl(output_file, line_order, type_meta, type_data,
                 type_cols, verbose)
    return output_file


def _write_jsonl(
    output_file: Path,
    line_order: List[Tuple[str, int]],
    type_info_map: Dict[str, dict],
    type_data: Dict[str, List[List[str]]],
    type_cols: Dict[str, List[str]],
    verbose: bool,
) -> None:
    """Shared JSONL reconstruction logic for v4 and v5."""
    with open(output_file, "w", encoding="utf-8", newline="") as out:
        for record_type, orig_idx in line_order:
            if record_type == "__blank__":
                out.write("\n")
                continue

            tinfo = type_info_map[record_type]
            columns = type_cols[record_type]
            field_order = tinfo.get("field_order", [])

            row = type_data[record_type][orig_idx]

            # Column lookup
            cell: Dict[str, str] = {}
            for i, col in enumerate(columns):
                cell[col] = row[i] if i < len(row) else ""

            # A1: content key recovery
            if "content_schema" in tinfo:
                content_keys = (tinfo["content_schema"].split(_CKEYS_SEP)
                                if tinfo["content_schema"] else [])
            elif "content_schemas" in tinfo:
                skidx_str = cell.get("__skidx__", "0")
                skidx = int(skidx_str) if skidx_str else 0
                schema_str = tinfo["content_schemas"][skidx]
                content_keys = (schema_str.split(_CKEYS_SEP)
                                if schema_str else [])
            else:
                ckeys_raw = cell.get("__ckeys__", "")
                content_keys = (ckeys_raw.split(_CKEYS_SEP)
                                if ckeys_raw else [])

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
            f"  [telemetry_codec] Reconstructed {total} records "
            f"-> {output_file.name}"
        )


def _rebuild_content(
    cell: Dict[str, str],
    content_keys: List[str],
) -> str:
    pairs = []
    for fname in content_keys:
        val = cell.get(f"c.{fname}", "")
        if val:
            pairs.append(f'"{fname}":{val}')
    return "{" + ",".join(pairs) + "}"


# ---------------------------------------------------------------------------
# RLE utilities (same as jsonl_codec)
# ---------------------------------------------------------------------------

def _rle_encode(line_order: List[Tuple[str, int]]) -> List:
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
    result = []
    for record_type, start_idx, count in compact_order:
        for i in range(count):
            result.append((record_type, start_idx + i))
    return result
