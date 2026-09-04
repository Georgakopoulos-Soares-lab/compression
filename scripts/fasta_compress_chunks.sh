#!/bin/bash
# Compress every FAV5 chunk in a directory with a hard byte-exact guarantee.
#
#   fasta_compress_chunks.sh <zli> <chunk_dir> <threads> <model> <fallback_count_file>
#
# <model> is one of:
#   <path.zlc>      a trained compressor
#   sddl:<schema>   the untrained SDDL profile with that schema (OpenZL 0.2.5
#                   can COMPRESS with an SDDL profile but cannot serialize a
#                   trained one, so this is how the FAV5 stream split is used)
#   -               no model; use the generic serial profile
# On ANY failure a chunk is retried with `zli compress --profile serial`, so the
# round trip is byte-exact regardless of which model is in play.
# Appends one line to <fallback_count_file> per chunk that needed the fallback.
set -uo pipefail

ZLI="$1"; DIR="$2"; P="$3"; COMP="$4"; FB="$5"
# schema for the SDDL candidate; empty disables it
SDDL_SCHEMA="${SDDL_SCHEMA:-$(cd "$(dirname "$0")/.." && pwd)/schemas/fasta_packed_v5.sddl}"
case "$COMP" in sddl:*) SDDL_SCHEMA="${COMP#sddl:}";; esac

export ZLI COMP FB SDDL_SCHEMA

# Try every shipped configuration on this chunk and keep the SMALLEST result.
# This is codec selection, not training: both configurations ship with the tool
# and neither is derived from the user's file. It matters because the trained
# model wins on mammalian genomes while the SDDL stream split wins on highly
# repetitive ones (wheat: 4.465x -> 4.790x), and compression is cheap enough
# (seconds, vs minutes for xz) that trying both costs nothing.
one() {
  local f="$1" best="" bsz="" sz
  # candidate 1: the shipped trained model
  if [ "$COMP" != "-" ] && [ "${COMP#sddl:}" = "$COMP" ] && [ -s "$COMP" ]; then
    if "$ZLI" compress "$f" --compressor "$COMP" --output "$f.zl.m" --force >/dev/null 2>&1; then
      bsz=$(stat -c%s "$f.zl.m"); best="$f.zl.m"
    fi
  fi
  # candidate 2: the SDDL stream split (FAV5 schema)
  if [ -n "$SDDL_SCHEMA" ] && [ -s "$SDDL_SCHEMA" ]; then
    if "$ZLI" compress "$f" --profile sddl --profile-arg "$SDDL_SCHEMA" \
         --output "$f.zl.s" --force >/dev/null 2>&1; then
      sz=$(stat -c%s "$f.zl.s")
      if [ -z "$bsz" ] || [ "$sz" -lt "$bsz" ]; then
        rm -f "$best"; bsz=$sz; best="$f.zl.s"
      else
        rm -f "$f.zl.s"
      fi
    fi
  fi
  if [ -n "$best" ]; then mv -f "$best" "$f.zl"; rm -f "$f.zl.m" "$f.zl.s"; return 0; fi
  rm -f "$f.zl.m" "$f.zl.s"
  if "$ZLI" compress "$f" --profile serial --output "$f.zl" --force >/dev/null 2>&1; then
    echo x >> "$FB"
    return 0
  fi
  echo "FAIL $f" >&2
  return 1
}
export -f one

find "$DIR" -maxdepth 1 -type f -name '*.fasta_packed.bin' -print0 \
  | xargs -0 -P "$P" -I{} bash -c 'one "$@"' _ {}
