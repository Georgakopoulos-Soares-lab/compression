"""TOON-like normalized text form for JSON data.

This is NOT a full TOON (Typed Object-Oriented Notation) implementation.
It produces a deterministic, human-readable text representation of JSON data
following strict normalization rules, labeled "TOON-like" throughout.

Deterministic rules:
  - Dict keys sorted lexicographically (Unicode code-point order)
  - Arrays keep original order
  - 2-space indentation
  - Strings: minimal JSON-compatible escaping (control chars, backslash, quote)
  - Numbers: integers as-is, floats in Python repr() form
  - Booleans: true / false (lowercase)
  - Null: null

The output is valid JSON with sorted keys and consistent formatting.
The point of this artifact is to provide a stable, normalized text form
for compression benchmarking — comparing how compressors handle a canonical
text representation vs. the binary ASTBIN format.
"""

import json
from pathlib import Path
from typing import Any


def json_to_toon(data: Any) -> str:
    """Convert parsed JSON data to TOON-like normalized text.

    Returns a deterministic UTF-8 string representation.
    """
    return _format_value(data, indent=0) + "\n"


def _format_value(node: Any, indent: int) -> str:
    """Recursively format a JSON value with stable serialization."""
    if isinstance(node, dict):
        return _format_object(node, indent)
    elif isinstance(node, list):
        return _format_array(node, indent)
    elif isinstance(node, str):
        return _format_string(node)
    elif isinstance(node, bool):
        return "true" if node else "false"
    elif isinstance(node, int):
        return str(node)
    elif isinstance(node, float):
        return _format_float(node)
    elif node is None:
        return "null"
    else:
        raise ValueError(f"Unsupported type: {type(node)}")


def _format_object(obj: dict, indent: int) -> str:
    if not obj:
        return "{}"
    lines = ["{"]
    keys = sorted(obj.keys())
    for i, key in enumerate(keys):
        comma = "," if i < len(keys) - 1 else ""
        val = _format_value(obj[key], indent + 1)
        prefix = "  " * (indent + 1)
        lines.append(f"{prefix}{_format_string(key)}: {val}{comma}")
    lines.append("  " * indent + "}")
    return "\n".join(lines)


def _format_array(arr: list, indent: int) -> str:
    if not arr:
        return "[]"
    lines = ["["]
    for i, item in enumerate(arr):
        comma = "," if i < len(arr) - 1 else ""
        val = _format_value(item, indent + 1)
        prefix = "  " * (indent + 1)
        lines.append(f"{prefix}{val}{comma}")
    lines.append("  " * indent + "]")
    return "\n".join(lines)


def _format_string(s: str) -> str:
    """Minimal JSON-compatible string escaping."""
    return json.dumps(s, ensure_ascii=False)


def _format_float(f: float) -> str:
    """Deterministic float formatting."""
    if f != f:  # NaN
        return "null"
    if f == float("inf"):
        return "1e+999"
    if f == float("-inf"):
        return "-1e+999"
    r = repr(f)
    # Ensure it looks like a float
    if "." not in r and "e" not in r and "E" not in r:
        r += ".0"
    return r


def json_to_toon_file(input_json_path: str, out_path: str) -> dict:
    """Read a JSON/JSONL file, convert to TOON-like text, write to out_path.

    Returns metadata dict.
    """
    input_p = Path(input_json_path)
    data = _load_json(input_p)
    text = json_to_toon(data)
    encoded = text.encode("utf-8")
    Path(out_path).write_bytes(encoded)
    return {
        "bytes": len(encoded),
        "lines": text.count("\n"),
    }


def _load_json(path: Path) -> Any:
    """Load JSON or JSONL file."""
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        return [json.loads(line) for line in raw.splitlines() if line.strip()]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        lines = [json.loads(l) for l in raw.splitlines() if l.strip()]
        return lines
