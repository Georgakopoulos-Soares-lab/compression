#!/usr/bin/env bash
# train_fasta_model.sh — train the ONE universal FASTA compressor that ships with
# the method. Runtime is compress-only: the end user never trains.
#
#   scripts/train_fasta_model.sh [output.zlc]
#
# Corpus: small whole genomes + size-capped samples of the large ones, spanning
# GC content, repeat/soft-mask density and IUPAC usage:
#   E. coli, S. cerevisiae, A. thaliana (whole / capped)
#   H. sapiens T2T, M. musculus GRCm39, T. aestivum (wheat)  -- ~30 MiB samples
# All are FAV5-encoded, then one `zli train --profile serial` over the lot.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZLI="$HERE/openzl/zli"
PRE="$HERE/tools/biocompress_preprocessor"
OUT="${1:-$HERE/artifacts/fasta_model.zlc}"
SMPL="${FASTA_TRAIN_SAMPLE_MIB:-30}"
MAXT="${FASTA_TRAIN_MAX_SECS:-2400}"

[ -x "$ZLI" ] && [ -x "$PRE" ] || { echo "build openzl/zli + tools first (scripts/build_all.sh)"; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/fasta_model.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/chunks"

# name:path:mode  (whole = use as-is; sample = cap to $SMPL MiB whole-records)
ENTRIES=(
  "ecoli:$HERE/data/fasta_train/ecoli_K12.fna:whole"
  "yeast:$HERE/data/fasta_train/yeast_S288C.fna:whole"
  "athal:$HERE/data/fasta_train/athaliana.fna:sample"
  "human:$HERE/data/GCA_009914755.4_T2T-CHM13v2.0_genomic.fna:sample"
  "mouse:$HERE/data/GCF_000001635.27_GRCm39_genomic.fna:sample"
  "wheat:$HERE/data/GCF_018294505.1_IWGSC_CS_RefSeq_v2.1_genomic.fna:sample"
)

n=0
for e in "${ENTRIES[@]}"; do
  name="${e%%:*}"; rest="${e#*:}"; path="${rest%:*}"; mode="${rest##*:}"
  [ -s "$path" ] || { echo "  skip (missing): $name -> $path"; continue; }
  fa="$WORK/$name.fna"
  if [ "$mode" = whole ]; then
    cp "$path" "$fa"
  else
    python3 "$HERE/scripts/make_train_sample.py" --in "$path" --out "$fa" --target-mib "$SMPL" >/dev/null
  fi
  cdir="$WORK/c_$name"; mkdir -p "$cdir"
  "$PRE" "$fa" "$cdir" 1 fasta_packed >/dev/null 2>&1
  for b in "$cdir"/*.fasta_packed.bin; do
    [ -s "$b" ] || continue
    cp "$b" "$WORK/chunks/${name}_$(basename "$b")"
    n=$((n+1))
  done
  rm -f "$fa"; rm -rf "$cdir"
  echo "  + $name  ($(du -h "$path" | cut -f1) source, mode=$mode)"
done

[ "$n" -gt 0 ] || { echo "no training chunks produced"; exit 1; }
echo "training on $n FAV5 chunks ($(du -sh "$WORK/chunks" | cut -f1)) -> $OUT"
mkdir -p "$(dirname "$OUT")"

# ---------------------------------------------------------------------------
# Train one candidate per profile and keep whichever actually compresses the
# validation chunks smallest (and still round-trips byte-exact).
#
#   sddl   : splits FAV5 into its component streams via schemas/fasta_packed_v5.sddl
#            so packed2bit / line_lens / the u64 RLE arrays each get their own codec
#   lz     : OpenZL 0.2.4's trainable LZ (searches windowLog + acceleration)
#   serial : the original single-opaque-stream baseline
# FASTA_PROFILES overrides the candidate list; FASTA_TRAINER picks the trainer.
# ---------------------------------------------------------------------------
SDDL="$HERE/schemas/fasta_packed_v5.sddl"
CANDIDATES="${FASTA_PROFILES:-sddl lz serial}"
TRAINER_ARGS=(); [ -n "${FASTA_TRAINER:-}" ] && TRAINER_ARGS=(--trainer "$FASTA_TRAINER")

# validation set: every training chunk (small enough, and it is the data we care about)
val_size() { # model -> total compressed bytes over the chunk set, or empty on failure
  local comp="$1" tot=0 c sz
  local -a cargs
  case "$comp" in
    sddl:*) cargs=(--profile sddl --profile-arg "${comp#sddl:}");;
    *)      cargs=(--compressor "$comp");;
  esac
  for c in "$WORK"/chunks/*; do
    "$ZLI" compress "$c" "${cargs[@]}" --output "$WORK/v.zl" --force >/dev/null 2>&1 || return 1
    sz=$(stat -c%s "$WORK/v.zl") || return 1
    # byte-exact check on the first chunk only (cheap but catches a broken graph)
    if [ "$tot" = 0 ]; then
      "$ZLI" decompress "$WORK/v.zl" --output "$WORK/v.dec" --force >/dev/null 2>&1 || return 1
      cmp -s "$c" "$WORK/v.dec" || return 1
    fi
    tot=$((tot + sz))
  done
  echo "$tot"
}

# A candidate failing to train is EXPECTED (that is why we try several), so
# errexit must be off here or the first failure kills the whole script.
set +e
best=""; bestSize=""; bestProfile=""
for p in $CANDIDATES; do
  cand=""
  if [ "$p" = sddl ]; then
    # OpenZL 0.2.5 can COMPRESS with an SDDL profile but fails to serialize a
    # TRAINED one (A1CBOR writeFailed), so evaluate the schema untrained. The
    # FAV5 stream split alone already beats the opaque `serial` blob.
    [ -s "$SDDL" ] || { echo "  skip sddl (no $SDDL)"; continue; }
    echo "  candidate: sddl (untrained profile, schema $(basename "$SDDL"))"
    cand="sddl:$SDDL"
  else
    cand="$WORK/model_$p.zlc"
    echo "  training candidate: $p"
    "$ZLI" train "$WORK/chunks" --profile "$p" "${TRAINER_ARGS[@]}" \
        --output "$cand" --force --threads "$(nproc)" --use-all-samples \
        --max-time-secs "$MAXT" >"$WORK/train_$p.log" 2>&1 || true
    if [ ! -s "$cand" ]; then
      echo "    $p: training produced nothing -- $(tail -n2 "$WORK/train_$p.log" 2>/dev/null | tr '\n' ' ')"
      continue
    fi
  fi
  sz="$(val_size "$cand")" || { echo "    $p: unusable (compress/round-trip failed)"; continue; }
  echo "    $p: $sz bytes over the validation chunks"
  if [ -z "$bestSize" ] || [ "$sz" -lt "$bestSize" ]; then
    bestSize="$sz"; best="$cand"; bestProfile="$p"
  fi
done
set -e

[ -n "$best" ] || { echo "FATAL: no usable FASTA model from candidates: $CANDIDATES"; exit 1; }
rm -f "$OUT"
case "$best" in
  sddl:*) # nothing to ship but the schema; the .profile file selects it
          printf '%s\n' "sddl" > "${OUT%.zlc}.profile"
          echo "OK  profile=sddl (schema $SDDL, no .zlc needed)  $bestSize bytes on validation";;
  *)      cp "$best" "$OUT"
          printf '%s\n' "$bestProfile" > "${OUT%.zlc}.profile"
          echo "OK  $OUT  profile=$bestProfile  ($(stat -c%s "$OUT") bytes, $bestSize bytes on validation)";;
esac
