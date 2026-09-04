#!/usr/bin/env python3
"""Validate a harc_packed chunk directory against the original FASTA it was
built from: decode every chunk, canonicalize the original the same way the
4-bit packer does (A/C/G/T pass through, anything else -> N), and diff
per-record sequences keyed by header. This is the HARC-equivalent of the
existing pipeline's `cmp` check on FAV4 .bin round trips -- just done at the
sequence level since HARC's on-disk layout isn't a byte-identical passthrough
of the input the way FAV4's is.
"""
import subprocess
import sys
import glob
import os

_CANON_TABLE = str.maketrans(
    {c: ("N" if c.upper() not in "ACGT" else c.upper()) for c in map(chr, range(256))}
)


def canonicalize(seq: str) -> str:
    # A/C/G/T pass through (upper-cased); anything else -> N.
    # str.translate is O(n) with no per-char Python objects, so this stays
    # usable on multi-GB genomes where the old char-by-char loop OOM'd.
    return seq.translate(_CANON_TABLE)

def parse_fasta(path):
    recs = {}
    header = None
    buf = []
    with open(path, "r") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith(">"):
                if header is not None:
                    recs[header] = "".join(buf)
                header = line[1:]
                buf = []
            else:
                buf.append(line)
        if header is not None:
            recs[header] = "".join(buf)
    return recs

def main():
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} <original.fasta> <chunks_dir> <harc_decode_bin>", file=sys.stderr)
        return 1
    orig_path, chunks_dir, decode_bin = sys.argv[1], sys.argv[2], sys.argv[3]

    orig = {h: canonicalize(s) for h, s in parse_fasta(orig_path).items()}

    bins = sorted(glob.glob(os.path.join(chunks_dir, "*.harc_packed.bin")))
    if not bins:
        print(f"No .harc_packed.bin files found in {chunks_dir}", file=sys.stderr)
        return 1

    decoded = {}
    tmp_dir = os.path.join(chunks_dir, "_decoded_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    for b in bins:
        out_path = os.path.join(tmp_dir, os.path.basename(b) + ".decoded.fasta")
        r = subprocess.run([decode_bin, b, out_path], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"harc_decode FAILED on {b}: {r.stderr}", file=sys.stderr)
            return 1
        decoded.update(parse_fasta(out_path))

    missing = set(orig.keys()) - set(decoded.keys())
    extra = set(decoded.keys()) - set(orig.keys())
    mismatches = []
    for h in orig:
        if h in decoded and orig[h] != decoded[h]:
            mismatches.append(h)

    ok = not missing and not extra and not mismatches
    print(f"records: original={len(orig)} decoded={len(decoded)}")
    if missing:
        print(f"MISSING records ({len(missing)}): {sorted(missing)[:5]}{' ...' if len(missing)>5 else ''}")
    if extra:
        print(f"EXTRA records ({len(extra)}): {sorted(extra)[:5]}{' ...' if len(extra)>5 else ''}")
    if mismatches:
        for h in mismatches[:5]:
            o, d = orig[h], decoded[h]
            print(f"MISMATCH: {h!r} orig_len={len(o)} decoded_len={len(d)}")
            if len(o) == len(d):
                diffs = [i for i in range(len(o)) if o[i] != d[i]]
                print(f"  first diff positions: {diffs[:10]} (total diffs={len(diffs)})")
            else:
                print(f"  LENGTH MISMATCH")

    print("VALIDATION " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
