#!/usr/bin/env bash
# Builds OpenZL and the four NYX codecs, then installs the runtime tree under
# $PREFIX/share/nyx with `nyx` on the PATH. `nyx` finds its binaries relative to
# its own resolved location, so a symlink in bin/ is all it needs.
set -euo pipefail
export JOBS="${CPU_COUNT:-4}"
bash scripts/build_all.sh

dest="$PREFIX/share/nyx"
mkdir -p "$dest/tools" "$dest/openzl" "$dest/scripts" "$dest/schemas" "$PREFIX/bin"
cp nyx LICENSE.md "$dest/"
cp tools/nyx_vcf tools/nyx_bed tools/biocompress_preprocessor tools/fasta_postprocess "$dest/tools/"
cp openzl/zli openzl/nyxfqz_v2 "$dest/openzl/"
cp scripts/fastazl scripts/fasta_compress_chunks.sh "$dest/scripts/"
cp schemas/*.sddl "$dest/schemas/"
cp -a artifacts "$dest/"
ln -sf ../share/nyx/nyx "$PREFIX/bin/nyx"
