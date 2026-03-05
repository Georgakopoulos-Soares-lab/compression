#!/usr/bin/env python3
"""Split telemetry JSONL files by creator into 500 MB chunked outputs."""

import json
import os
import shutil
import sys
import glob

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHUNK_SIZE = 500 * 1024 * 1024  # 500 MB

CATEGORIES = {"nom-telegraf", "nom-link"}


class ChunkedWriter:
    """Writes JSONL lines to sequentially numbered files, rotating at CHUNK_SIZE."""

    def __init__(self, out_dir):
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self.part = 0
        self.current_bytes = 0
        self.total_bytes = 0
        self.total_records = 0
        self.fh = None
        self._open_next()

    def _open_next(self):
        if self.fh:
            self.fh.close()
        self.part += 1
        path = os.path.join(self.out_dir, f"part-{self.part:03d}.jsonl")
        self.fh = open(path, "w")
        self.current_bytes = 0

    def write(self, line):
        data = line + "\n"
        n = len(data.encode("utf-8"))
        self.fh.write(data)
        self.current_bytes += n
        self.total_bytes += n
        self.total_records += 1
        if self.current_bytes >= CHUNK_SIZE:
            self._open_next()

    def close(self):
        if self.fh:
            self.fh.close()
            self.fh = None


def collect_source_files():
    """Gather all .jsonl source files from root and 1GB/ subfolder."""
    sources = []
    for f in sorted(glob.glob(os.path.join(BASE_DIR, "*.jsonl"))):
        sources.append(f)
    subdir = os.path.join(BASE_DIR, "1GB")
    if os.path.isdir(subdir):
        for f in sorted(glob.glob(os.path.join(subdir, "*.jsonl"))):
            sources.append(f)
    return sources


def main():
    source_files = collect_source_files()
    print(f"Found {len(source_files)} source JSONL files", file=sys.stderr)

    writers = {cat: ChunkedWriter(os.path.join(BASE_DIR, cat)) for cat in CATEGORIES}

    processed = 0
    skipped_blank = 0
    discarded = 0

    for fi, fpath in enumerate(source_files):
        fname = os.path.relpath(fpath, BASE_DIR)
        print(f"  [{fi+1}/{len(source_files)}] {fname}", file=sys.stderr, flush=True)
        with open(fpath, "r") as f:
            for line in f:
                raw = line.rstrip("\n")
                if not raw:
                    skipped_blank += 1
                    continue

                processed += 1
                if processed % 1_000_000 == 0:
                    print(f"    ... {processed:,} records processed", file=sys.stderr, flush=True)

                # Fast extraction: find "creator":"<value>" without full JSON parse
                idx = raw.find('"creator":"')
                if idx == -1:
                    discarded += 1
                    continue
                start = idx + 11  # len('"creator":"')
                end = raw.index('"', start)
                creator = raw[start:end]

                if creator in CATEGORIES:
                    writers[creator].write(raw)
                else:
                    discarded += 1

    for w in writers.values():
        w.close()

    # --- Summary ---
    print("\n" + "=" * 60, file=sys.stderr)
    print("SPLIT COMPLETE", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(f"Source files processed:  {len(source_files)}", file=sys.stderr)
    print(f"Total records:          {processed:,}", file=sys.stderr)
    print(f"Blank lines skipped:    {skipped_blank:,}", file=sys.stderr)
    print(f"Discarded (other):      {discarded:,}", file=sys.stderr)
    for cat in sorted(CATEGORIES):
        w = writers[cat]
        print(
            f"  {cat:20s}  {w.total_records:>12,} records  "
            f"{w.total_bytes/1024**3:.2f} GiB  {w.part} file(s)",
            file=sys.stderr,
        )

    # --- Delete originals ---
    print("\nDeleting original files ...", file=sys.stderr, flush=True)
    deleted = 0
    for fpath in source_files:
        os.remove(fpath)
        deleted += 1

    # Clean up 1GB/ subfolder (compressed variants + directory)
    subdir = os.path.join(BASE_DIR, "1GB")
    if os.path.isdir(subdir):
        for f in os.listdir(subdir):
            fpath = os.path.join(subdir, f)
            if os.path.isfile(fpath):
                os.remove(fpath)
                deleted += 1
        try:
            os.rmdir(subdir)
        except OSError:
            pass

    print(f"Deleted {deleted} original files.", file=sys.stderr)
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
