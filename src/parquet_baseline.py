#!/usr/bin/env python3
"""Parquet + zstd as a BED baseline -- the control for "isn't this just columnar?"

Splitting a table into one stream per column is not new; Parquet has done it for
a decade, with dictionary encoding, run-length encoding, delta-encoded integers
and a zstd back end. If NYX's BED result came only from the column split, a
mature columnar format would match it. This baseline exists so that comparison
is on the table rather than asserted either way.

To keep the comparison honest it must be byte-exact like every other row, so
each column is typed only when the typing provably round-trips: a column becomes
int64 when every value's str() reproduces the original text, otherwise it stays
a string. That is what a competent engineer would do by hand, and it is exactly
the favourable case for Parquet.

  parquet_baseline.py compress   <in.bed>  <out.parquet>
  parquet_baseline.py decompress <in.parquet> <out.bed>
"""
import sys

import pyarrow as pa
import pyarrow.parquet as pq

HDR = ("track", "browser", "#")


def split_header(raw: bytes):
    """Leading track/browser/# lines are metadata, not rows."""
    head, i = [], 0
    while i < len(raw):
        nl = raw.find(b"\n", i)
        if nl < 0:
            break
        line = raw[i:nl]
        if not line.startswith(tuple(h.encode() for h in HDR)):
            break
        head.append(line)
        i = nl + 1
    return raw[:i], raw[i:]


def compress(src, dst):
    raw = open(src, "rb").read()
    header, body = split_header(raw)
    text = body.decode("utf-8", "surrogateescape")
    ends_nl = text.endswith("\n")
    lines = text.split("\n")
    if ends_nl:
        lines.pop()

    width = max(((lines.count, l.count("\t") + 1) for l in lines[:4096]),
                default=(0, 0))[1] if lines else 0
    if lines:
        from collections import Counter
        width = Counter(l.count("\t") + 1 for l in lines[:4096]).most_common(1)[0][0]

    cols = [[] for _ in range(width)]
    escapes = []          # (line index, text) for rows that are not `width` wide
    for i, l in enumerate(lines):
        f = l.split("\t")
        if len(f) != width:
            escapes.append((i, l))
            continue
        for j, v in enumerate(f):
            cols[j].append(v)

    arrays, names = [], []
    for j, c in enumerate(cols):
        # Type the column only if the typing is provably reversible.
        try:
            iv = [int(v) for v in c]
            if all(str(a) == b for a, b in zip(iv, c)):
                arrays.append(pa.array(iv, type=pa.int64()))
            else:
                arrays.append(pa.array(c, type=pa.string()))
        except (ValueError, OverflowError):
            arrays.append(pa.array(c, type=pa.string()))
        names.append(f"c{j}")

    meta = {
        b"header": header,
        b"ends_nl": b"1" if ends_nl else b"0",
        b"nlines": str(len(lines)).encode(),
        b"esc_idx": ",".join(str(i) for i, _ in escapes).encode(),
        b"esc_txt": "\n".join(t for _, t in escapes).encode("utf-8", "surrogateescape"),
    }
    tbl = pa.Table.from_arrays(arrays, names=names) if arrays else pa.table({})
    tbl = tbl.replace_schema_metadata(meta)
    pq.write_table(tbl, dst, compression="zstd", compression_level=19,
                   use_dictionary=True, write_statistics=False)


def decompress(src, dst):
    # Read through Python into a buffer, not by path. pyarrow's own file reader
    # uses pread with large requests, and on BeeGFS (TACC /scratch) those fail
    # with EFAULT -- "[Errno 14] Bad address" -- on most files above a few MB,
    # while the same file reads fine from /tmp. It looked like a round-trip
    # failure on 23 of 34 files until the stderr was kept.
    with open(src, "rb") as fh:
        tbl = pq.read_table(pa.BufferReader(fh.read()))
    m = tbl.schema.metadata or {}
    header = m.get(b"header", b"")
    ends_nl = m.get(b"ends_nl", b"1") == b"1"
    nlines = int(m.get(b"nlines", b"0"))
    ei = m.get(b"esc_idx", b"").decode()
    esc_idx = [int(x) for x in ei.split(",") if x]
    et = m.get(b"esc_txt", b"").decode("utf-8", "surrogateescape")
    esc_txt = et.split("\n") if et else []

    cols = [[str(v) for v in tbl.column(k).to_pylist()] for k in range(tbl.num_columns)]
    esc = dict(zip(esc_idx, esc_txt))
    out, row = [], 0
    for i in range(nlines):
        if i in esc:
            out.append(esc[i])
        else:
            out.append("\t".join(c[row] for c in cols))
            row += 1
    body = "\n".join(out)
    if ends_nl and nlines:
        body += "\n"
    with open(dst, "wb") as fh:
        fh.write(header)
        fh.write(body.encode("utf-8", "surrogateescape"))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    (compress if sys.argv[1] == "compress" else decompress)(sys.argv[2], sys.argv[3])
