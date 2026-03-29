#!/bin/bash
# Decompression wrapper for Data Loader.
# Called by TelemetryDecompressor.java via ProcessBuilder.
#
# Usage: decompress.sh <input.zljsonl> <output.jsonl>

set -euo pipefail
cd /usr/local/nom/lib/telemetry-compress
exec python3 telemetry_service.py decompress "$1" "$2"
