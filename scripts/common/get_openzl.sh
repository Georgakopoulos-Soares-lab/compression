#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -L)"
OPENZL_DIR="${OPENZL_DIR:-$HERE/openzl}"
OPENZL_REPO="${OPENZL_REPO:-https://github.com/facebook/openzl}"
# Pinned to the commit currently used in this workspace.
OPENZL_COMMIT="${OPENZL_COMMIT:-e40fe9f314283047147d573d113fa7d17eabf7ac}"

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
