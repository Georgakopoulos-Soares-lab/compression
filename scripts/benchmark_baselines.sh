#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
THREADS="${THREADS:-16}"
BGZIP_BIN="${BGZIP_BIN:-}"

# Used to keep outputs consistent across pipelines (fasta/fastq/vcf/...)
FORMAT_TAG="${FORMAT_TAG:-fasta}"

# Behavior knobs
PARALLEL="${PARALLEL:-1}"           # 1 => run enabled benchmarks concurrently
SKIP_EXISTING="${SKIP_EXISTING:-1}" # 1 => if output file exists, don't recompute

# Which benchmarks to run (defaults match the tools shown in the FASTA graph)
RUN_PIGZ9="${RUN_PIGZ9:-1}"
RUN_GZIP_STD="${RUN_GZIP_STD:-1}"
RUN_ZSTD_STD="${RUN_ZSTD_STD:-0}"
RUN_7Z_STD="${RUN_7Z_STD:-1}"
RUN_BGZIP_STD="${RUN_BGZIP_STD:-1}"
RUN_BGZIP2="${RUN_BGZIP2:-1}"      # interpreted as bgzip -l 2
RUN_XZ_STD="${RUN_XZ_STD:-1}"       # xz default preset, multithreaded

# Extra/optional knobs
RUN_ZSTD7="${RUN_ZSTD7:-1}"
RUN_BZIP2="${RUN_BZIP2:-0}"

INPUT_IN="${1:-}"
if [ -z "$INPUT_IN" ]; then
  echo "Usage: $0 <input_file>" >&2
  exit 2
fi
if [ ! -f "$INPUT_IN" ]; then
  echo "Error: input not found: $INPUT_IN" >&2
  exit 2
fi

OUT_DIR="${OUT_DIR:-$HERE/out/baselines}"
mkdir -p "$OUT_DIR" "$OUT_DIR/timing"

RESULTS_TSV="${RESULTS_TSV:-$OUT_DIR/results_${FORMAT_TAG}.tsv}"
OPENZL_TIME_FILE="${OPENZL_TIME_FILE:-$HERE/out/timing/openzl_comp_full.time}"
# Optional override (kept for FASTA packed point compatibility). If empty, ratio is computed.
OPENZL_RATIO_OVERRIDE="${OPENZL_RATIO_OVERRIDE:-}"
# If set and exists, compute OpenZL ratio from this output file.
OPENZL_OUT_FILE="${OPENZL_OUT_FILE:-}"
OPENZL_LABEL="${OPENZL_LABEL:-OpenZL}"

orig_bytes="$(stat -c%s "$INPUT_IN")"

have_cmd() { command -v "$1" >/dev/null 2>&1; }

resolve_bgzip() {
  if [ -n "$BGZIP_BIN" ] && [ -x "$BGZIP_BIN" ]; then
    echo "$BGZIP_BIN"
    return 0
  fi
  if have_cmd bgzip; then
    command -v bgzip
    return 0
  fi
  if [ -x "$HERE/third_party/htslib/bgzip" ]; then
    echo "$HERE/third_party/htslib/bgzip"
    return 0
  fi
  return 1
}

human_mib() {
  python3 - "$1" <<'PY'
import sys
n=int(sys.argv[1])
print(f"{n/1024/1024:.2f} MiB")
PY
}

run_one() {
  local name="$1"; shift
  local out_file="$1"; shift
  local time_file="$OUT_DIR/timing/${name}.time"

  if [ "$SKIP_EXISTING" = "1" ] && [ -f "$out_file" ]; then
    local out_bytes
    out_bytes="$(stat -c%s "$out_file")"
    echo "== $name (skip; output exists) =="
    python3 - "$name" "$orig_bytes" "$out_bytes" <<'PY'
import sys
name = sys.argv[1]
orig_b = int(sys.argv[2])
out_b = int(sys.argv[3])
ratio = (orig_b / out_b) if out_b else float('inf')
print(
    f"{name}: in={orig_b} bytes ({orig_b/1024/1024:.2f} MiB) "
    f"out={out_b} bytes ({out_b/1024/1024:.2f} MiB) ratio={ratio:.3f}x"
)
PY
    return 0
  fi

  echo "== $name =="
  /usr/bin/time -f "elapsed_sec=%e maxrss_kb=%M" -o "$time_file" "$@"

  if [ ! -f "$out_file" ]; then
    echo "Error: expected output missing: $out_file" >&2
    exit 1
  fi

  local out_bytes
  out_bytes="$(stat -c%s "$out_file")"
  python3 - "$name" "$orig_bytes" "$out_bytes" <<'PY'
import sys
name = sys.argv[1]
orig_b = int(sys.argv[2])
out_b = int(sys.argv[3])
ratio = (orig_b / out_b) if out_b else float('inf')
print(
    f"{name}: in={orig_b} bytes ({orig_b/1024/1024:.2f} MiB) "
    f"out={out_b} bytes ({out_b/1024/1024:.2f} MiB) ratio={ratio:.3f}x"
)
PY
}

declare -a PIDS=()

enqueue() {
  if [ "$PARALLEL" = "1" ]; then
    (
      set -euo pipefail
      run_one "$@"
    ) &
    PIDS+=("$!")
  else
    run_one "$@"
  fi
}

wait_all() {
  if [ "$PARALLEL" = "1" ]; then
    local fail=0
    for pid in "${PIDS[@]}"; do
      if ! wait "$pid"; then
        fail=1
      fi
    done
    if [ "$fail" = "1" ]; then
      echo "One or more benchmarks failed" >&2
      exit 1
    fi
  fi
}

# pigz -9 (requested)
if [ "$RUN_PIGZ9" = "1" ]; then
  if have_cmd pigz; then
    enqueue "pigz9_t${THREADS}" "$OUT_DIR/$(basename "$INPUT_IN").pigz9.gz" \
      bash -c 'pigz -9 -p "$1" -c "$2" > "$3"' _ "$THREADS" "$INPUT_IN" "$OUT_DIR/$(basename "$INPUT_IN").pigz9.gz"
  else
    echo "Skipping pigz (missing)"
  fi
fi

# gzip standard (requested)
if [ "$RUN_GZIP_STD" = "1" ]; then
  if have_cmd gzip; then
    enqueue "gzip_default" "$OUT_DIR/$(basename "$INPUT_IN").gzip.gz" \
      bash -c 'gzip -c "$1" > "$2"' _ "$INPUT_IN" "$OUT_DIR/$(basename "$INPUT_IN").gzip.gz"
  else
    echo "Skipping gzip (missing)"
  fi
fi

# zstd standard (requested)
if [ "$RUN_ZSTD_STD" = "1" ]; then
  if have_cmd zstd; then
    enqueue "zstd_default_t${THREADS}" "$OUT_DIR/$(basename "$INPUT_IN").zstd.zst" \
      bash -c 'zstd -T"$1" -q -c "$2" > "$3"' _ "$THREADS" "$INPUT_IN" "$OUT_DIR/$(basename "$INPUT_IN").zstd.zst"
  else
    echo "Skipping zstd (missing)"
  fi
fi

# zstd -7 (NOT requested; left off by default)
if [ "$RUN_ZSTD7" = "1" ]; then
  if have_cmd zstd; then
    enqueue "zstd7_t${THREADS}" "$OUT_DIR/$(basename "$INPUT_IN").zstd7.zst" \
      bash -c 'zstd -7 -T"$1" -q -c "$2" > "$3"' _ "$THREADS" "$INPUT_IN" "$OUT_DIR/$(basename "$INPUT_IN").zstd7.zst"
  else
    echo "Skipping zstd7 (missing zstd)"
  fi
fi

# xz standard (requested now): default preset, threaded
if [ "$RUN_XZ_STD" = "1" ]; then
  if have_cmd xz; then
    XZ_OUT="$OUT_DIR/$(basename "$INPUT_IN").xz"
    enqueue "xz_default_t${THREADS}" "$XZ_OUT" \
      bash -c 'xz -T"$1" -c "$2" > "$3"' _ "$THREADS" "$INPUT_IN" "$XZ_OUT"
  else
    echo "Skipping xz (missing)"
  fi
fi

# 7z standard (requested)
if [ "$RUN_7Z_STD" = "1" ]; then
  if have_cmd 7z; then
    ARCH_STD="$OUT_DIR/$(basename "$INPUT_IN").7z.7z"
    enqueue "7z_default_t${THREADS}" "$ARCH_STD" \
      bash -c '7z a -t7z -mmt="$1" "$2" "$3" >/dev/null' _ "$THREADS" "$ARCH_STD" "$INPUT_IN"
  else
    echo "Skipping 7z (missing)"
  fi
fi

# bgzip standard (requested)
if [ "$RUN_BGZIP_STD" = "1" ]; then
  if BGZIP_PATH="$(resolve_bgzip)"; then
    BGZ="$OUT_DIR/$(basename "$INPUT_IN").bgzip.bgz"
    enqueue "bgzip_default_t${THREADS}" "$BGZ" \
      bash -c '"$1" -@ "$2" -c "$3" > "$4"' _ "$BGZIP_PATH" "$THREADS" "$INPUT_IN" "$BGZ"
  else
    echo "Skipping bgzip (missing)"
  fi
fi

# bgzip2: interpreted as bgzip -l 2 (requested)
if [ "$RUN_BGZIP2" = "1" ]; then
  if BGZIP_PATH="$(resolve_bgzip)"; then
    BGZ2="$OUT_DIR/$(basename "$INPUT_IN").bgzip2.bgz"
    enqueue "bgzip2_l2_t${THREADS}" "$BGZ2" \
      bash -c '"$1" -l 2 -@ "$2" -c "$3" > "$4"' _ "$BGZIP_PATH" "$THREADS" "$INPUT_IN" "$BGZ2"
  else
    echo "Skipping bgzip2 (missing bgzip)"
  fi
fi

# bzip2 (slow, single-thread; not requested)
if [ "$RUN_BZIP2" = "1" ]; then
  if have_cmd bzip2; then
    BZ2="$OUT_DIR/$(basename "$INPUT_IN").bzip2.bz2"
    enqueue "bzip2" "$BZ2" \
      bash -c 'bzip2 -c "$1" > "$2"' _ "$INPUT_IN" "$BZ2"
  else
    echo "Skipping bzip2 (missing)"
  fi
fi

wait_all

write_results_tsv() {
  local tmp
  tmp="${RESULTS_TSV}.tmp"
  echo -e "tool\tratio\tseconds\tout_bytes\tout_file" > "$tmp"

  python3 - "$INPUT_IN" "$OUT_DIR" "$THREADS" "$OPENZL_TIME_FILE" "${OPENZL_RATIO_OVERRIDE}" "$OPENZL_OUT_FILE" "$OPENZL_LABEL" <<'PY' >> "$tmp"
import sys
from pathlib import Path

inp = Path(sys.argv[1])
out_dir = Path(sys.argv[2])
threads = sys.argv[3]
openzl_time = Path(sys.argv[4])
openzl_ratio_override = sys.argv[5]
openzl_out_file = sys.argv[6]
openzl_label = sys.argv[7]

orig = inp.stat().st_size

def read_elapsed(p: Path):
  if not p.exists():
    return None
  txt = p.read_text(errors="replace")
  for token in txt.split():
    if token.startswith("elapsed_sec="):
      try:
        return float(token.split("=", 1)[1])
      except Exception:
        return None
  return None

def emit(label: str, ratio: float, secs: float, out_bytes: int | None, out_file: str | None):
  ob = "" if out_bytes is None else str(out_bytes)
  of = "" if out_file is None else out_file
  print(f"{label}\t{ratio:.6f}\t{secs:.6f}\t{ob}\t{of}")

def emit_from_files(label: str, out_path: Path, time_path: Path):
  if not out_path.exists() or not time_path.exists():
    return
  secs = read_elapsed(time_path)
  if secs is None:
    return
  out_bytes = out_path.stat().st_size
  ratio = orig / out_bytes if out_bytes else float("inf")
  emit(label, ratio, secs, out_bytes, str(out_path))

secs = read_elapsed(openzl_time)
if secs is not None:
  out_path = Path(openzl_out_file) if openzl_out_file else None
  if out_path is not None and out_path.exists():
    out_bytes = out_path.stat().st_size
    ratio = orig / out_bytes if out_bytes else float("inf")
    emit(openzl_label, ratio, secs, out_bytes, str(out_path))
  elif openzl_ratio_override:
    try:
      ratio = float(openzl_ratio_override)
      emit(openzl_label, ratio, secs, None, None)
    except Exception:
      pass

emit_from_files("zstd -7 (16t)", out_dir / (inp.name + ".zstd7.zst"), out_dir / "timing" / f"zstd7_t{threads}.time")
emit_from_files("pigz -9 (16t)", out_dir / (inp.name + ".pigz9.gz"), out_dir / "timing" / f"pigz9_t{threads}.time")
emit_from_files("gzip (default)", out_dir / (inp.name + ".gzip.gz"), out_dir / "timing" / "gzip_default.time")
emit_from_files("7z (default, 16t)", out_dir / (inp.name + ".7z.7z"), out_dir / "timing" / f"7z_default_t{threads}.time")
emit_from_files("bgzip (default, 16t)", out_dir / (inp.name + ".bgzip.bgz"), out_dir / "timing" / f"bgzip_default_t{threads}.time")
emit_from_files("bgzip -l2 (16t)", out_dir / (inp.name + ".bgzip2.bgz"), out_dir / "timing" / f"bgzip2_l2_t{threads}.time")
emit_from_files("xz (default, 16t)", out_dir / (inp.name + ".xz"), out_dir / "timing" / f"xz_default_t{threads}.time")
PY

  mv -f "$tmp" "$RESULTS_TSV"
  echo "Wrote results: $RESULTS_TSV"
}

write_results_tsv

echo "---"
echo "Timing files: $OUT_DIR/timing/*.time"
echo "Outputs:      $OUT_DIR"