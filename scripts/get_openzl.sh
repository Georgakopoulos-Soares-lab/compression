#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENZL_DIR="${OPENZL_DIR:-$HERE/openzl}"
OPENZL_REPO="${OPENZL_REPO:-https://github.com/facebook/openzl}"
# Pinned to the commit currently used in this workspace.
# d262127 = OpenZL 0.2.5 (dev, 2026-08-28). All three format pipelines
# (FASTA / VCF / FASTQ) are aligned on this exact commit for the paper.
OPENZL_COMMIT="${OPENZL_COMMIT:-d26212728c46f3e27cd77c5c7b962cd6d375ff4d}"

if [ -d "$OPENZL_DIR/.git" ]; then
  echo "OpenZL already present: $OPENZL_DIR"
else
  echo "Cloning OpenZL into: $OPENZL_DIR"
  git clone "$OPENZL_REPO" "$OPENZL_DIR"
fi

cd "$OPENZL_DIR"
git fetch --all --tags -q

echo "Checking out pinned commit: $OPENZL_COMMIT"
git checkout -q "$OPENZL_COMMIT"

echo "Done. OpenZL HEAD is: $(git rev-parse HEAD)"
