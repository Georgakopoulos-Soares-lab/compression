"""ASTBIN v1 — AST-based binary container for JSON data.

Parses JSON into an abstract syntax tree, then serializes it into a
deterministic binary format suitable for schema-aware compression with OpenZL.

Binary layout (little-endian):
  Header: magic(4) version(u16) reserved(u16)
          n_tokens(u32) n_strings(u32) n_ints(u32)
          n_floats(u32) n_bools(u32) string_blob_len(u32)
  Body:   tokens(u8[n_tokens]) args(u32[n_tokens])
          string_offsets(u32[n_strings+1]) string_blob(u8[string_blob_len])
          ints(i64[n_ints]) floats(f64[n_floats]) bools(u8[n_bools])
"""

import json
import struct
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Token opcodes
BEGIN_OBJ = 1
END_OBJ = 2
BEGIN_ARR = 3
END_ARR = 4
KEY = 5
STR = 6
INT = 7
FLOAT = 8
BOOL = 9
NULL = 10

MAGIC = b"AXV1"
VERSION = 1

# int64 bounds
_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1


class ASTBINBuilder:
    """Accumulates tokens and value pools during AST traversal."""

    def __init__(self):
        self.tokens: List[int] = []
        self.args: List[int] = []
        self.strings: List[str] = []       # ordered unique strings
        self._string_map: Dict[str, int] = {}  # string -> id
        self.ints: List[int] = []
        self.floats: List[float] = []
        self.bools: List[int] = []          # 0 or 1

    def _intern_string(self, s: str) -> int:
        """Return string id, registering new strings as needed.

        Final IDs are reassigned after traversal (sorted lex order).
        During traversal we store by insertion order; remap at the end.
        """
        if s not in self._string_map:
            sid = len(self.strings)
            self._string_map[s] = sid
            self.strings.append(s)
        return self._string_map[s]

    def emit(self, opcode: int, arg: int = 0) -> None:
        self.tokens.append(opcode)
        self.args.append(arg)

    def add_string(self, s: str) -> int:
        return self._intern_string(s)

    def add_int(self, v: int) -> int:
        idx = len(self.ints)
        self.ints.append(v)
        return idx

    def add_float(self, v: float) -> int:
        idx = len(self.floats)
        self.floats.append(v)
        return idx

    def add_bool(self, v: bool) -> int:
        idx = len(self.bools)
        self.bools.append(1 if v else 0)
        return idx


def _traverse(node: Any, b: ASTBINBuilder) -> None:
    """Pre-order traversal of a parsed JSON value, emitting tokens."""
    if isinstance(node, dict):
        b.emit(BEGIN_OBJ)
        for key in sorted(node.keys()):
            sid = b.add_string(key)
            b.emit(KEY, sid)
            _traverse(node[key], b)
        b.emit(END_OBJ)
    elif isinstance(node, list):
        b.emit(BEGIN_ARR)
        for item in node:
            _traverse(item, b)
        b.emit(END_ARR)
    elif isinstance(node, str):
        sid = b.add_string(node)
        b.emit(STR, sid)
    elif isinstance(node, bool):
        # bool check before int because bool is subclass of int in Python
        idx = b.add_bool(node)
        b.emit(BOOL, idx)
    elif isinstance(node, int):
        if _I64_MIN <= node <= _I64_MAX:
            idx = b.add_int(node)
            b.emit(INT, idx)
        else:
            # Overflow: encode as string
            sid = b.add_string(str(node))
            b.emit(STR, sid)
    elif isinstance(node, float):
        idx = b.add_float(node)
        b.emit(FLOAT, idx)
    elif node is None:
        b.emit(NULL)
    else:
        raise ValueError(f"Unsupported JSON value type: {type(node)}")


def _finalize_string_table(b: ASTBINBuilder) -> Tuple[List[str], Dict[int, int]]:
    """Sort strings lexicographically and build old-id -> new-id mapping."""
    sorted_strings = sorted(b.strings)
    old_to_new = {}
    new_map: Dict[str, int] = {}
    for new_id, s in enumerate(sorted_strings):
        new_map[s] = new_id
    for old_id, s in enumerate(b.strings):
        old_to_new[old_id] = new_map[s]
    return sorted_strings, old_to_new


def json_to_astbin(data: Any) -> bytes:
    """Serialize a parsed JSON value to ASTBIN v1 bytes."""
    b = ASTBINBuilder()
    _traverse(data, b)

    # Finalize string table: sort and remap IDs
    sorted_strings, id_remap = _finalize_string_table(b)

    # Remap string references in args
    remapped_args = list(b.args)
    for i, opcode in enumerate(b.tokens):
        if opcode in (KEY, STR):
            remapped_args[i] = id_remap[b.args[i]]

    # Build string blob and offsets
    encoded_strings = [s.encode("utf-8") for s in sorted_strings]
    string_blob = b"".join(encoded_strings)
    string_offsets = []
    offset = 0
    for enc in encoded_strings:
        string_offsets.append(offset)
        offset += len(enc)
    string_offsets.append(offset)  # sentinel = string_blob_len

    n_tokens = len(b.tokens)
    n_strings = len(sorted_strings)
    n_ints = len(b.ints)
    n_floats = len(b.floats)
    n_bools = len(b.bools)
    string_blob_len = len(string_blob)

    # Pack header
    header = struct.pack(
        "<4sHHIIIIII",
        MAGIC,
        VERSION,
        0,  # reserved
        n_tokens,
        n_strings,
        n_ints,
        n_floats,
        n_bools,
        string_blob_len,
    )

    # Pack body
    tokens_bytes = struct.pack(f"<{n_tokens}B", *b.tokens)
    args_bytes = struct.pack(f"<{n_tokens}I", *remapped_args) if n_tokens else b""
    offsets_bytes = struct.pack(f"<{n_strings + 1}I", *string_offsets)
    ints_bytes = struct.pack(f"<{n_ints}q", *b.ints) if n_ints else b""
    floats_bytes = struct.pack(f"<{n_floats}d", *b.floats) if n_floats else b""
    bools_bytes = struct.pack(f"<{n_bools}B", *b.bools) if n_bools else b""

    return (
        header
        + tokens_bytes
        + args_bytes
        + offsets_bytes
        + string_blob
        + ints_bytes
        + floats_bytes
        + bools_bytes
    )


def json_to_astbin_file(input_json_path: str, out_path: str) -> Dict[str, Any]:
    """Read a JSON (or JSONL) file, convert to ASTBIN v1, write to out_path.

    For JSONL files (detected by .jsonl extension or content probing), all
    lines are collected into a single JSON array before serialization.

    Returns metadata dict with token/pool counts and sizes.
    """
    input_path = Path(input_json_path)
    data = _load_json_or_jsonl(input_path)

    payload = json_to_astbin(data)
    Path(out_path).write_bytes(payload)

    # Parse counts from the header for metadata
    (_, version, _, n_tokens, n_strings, n_ints, n_floats, n_bools,
     string_blob_len) = struct.unpack("<4sHHIIIIII", payload[:32])

    return {
        "version": version,
        "n_tokens": n_tokens,
        "n_strings": n_strings,
        "n_ints": n_ints,
        "n_floats": n_floats,
        "n_bools": n_bools,
        "string_blob_len": string_blob_len,
        "total_bytes": len(payload),
    }


def _load_json_or_jsonl(path: Path) -> Any:
    """Load a JSON or JSONL file, returning parsed data.

    JSONL is detected by .jsonl extension or by attempting line-by-line parse
    if standard JSON parsing fails.
    """
    raw = path.read_text(encoding="utf-8")

    if path.suffix.lower() == ".jsonl":
        return _parse_jsonl(raw)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Try JSONL as fallback
        try:
            return _parse_jsonl(raw)
        except Exception:
            raise


def _parse_jsonl(text: str) -> List[Any]:
    """Parse JSONL text into a list of JSON values."""
    results = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            results.append(json.loads(line))
    return results


def jsonl_to_astbin_file(jsonl_path: str, out_path: str) -> Dict[str, Any]:
    """Convert a JSONL shard file to ASTBIN v1.

    Reads the JSONL file line by line, collects all records into a single
    JSON array, then serializes to ASTBIN. This keeps memory usage bounded
    to one shard (~128 MiB of text plus parsed objects).

    Returns metadata dict.
    """
    records: List[Any] = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    payload = json_to_astbin(records)
    Path(out_path).write_bytes(payload)

    (_, version, _, n_tokens, n_strings, n_ints, n_floats, n_bools,
     string_blob_len) = struct.unpack("<4sHHIIIIII", payload[:32])

    return {
        "version": version,
        "n_tokens": n_tokens,
        "n_strings": n_strings,
        "n_ints": n_ints,
        "n_floats": n_floats,
        "n_bools": n_bools,
        "string_blob_len": string_blob_len,
        "total_bytes": len(payload),
        "record_count": len(records),
    }
