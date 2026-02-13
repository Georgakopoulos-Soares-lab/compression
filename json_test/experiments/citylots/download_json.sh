#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${DATA_DIR:-$HERE/data}"
mkdir -p "$DATA_DIR"

JSON_URL="${JSON_URL:-https://raw.githubusercontent.com/zemirco/sf-city-lots-json/master/citylots.json}"
JSON_OUT="${JSON_OUT:-$DATA_DIR/citylots.json}"

if [ ! -f "$JSON_OUT" ]; then
  echo "Downloading: $JSON_URL"
  if command -v curl >/dev/null; then
    curl -o "$JSON_OUT" "$JSON_URL"
  elif command -v wget >/dev/null; then
    wget -O "$JSON_OUT" "$JSON_URL"
  else
    echo "Error: neither curl nor wget found."
    exit 1
  fi
else
  echo "Already downloaded: $JSON_OUT"
fi

echo "JSON: $JSON_OUT"
