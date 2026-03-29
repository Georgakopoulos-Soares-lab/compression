#!/usr/bin/env bash
# collect_dns_data.sh — Collect decoded DNS data from Kafka into rotating files.
#
# Pipes nom-kafka-dump output through a rotating file writer.
# Each file is capped at --max-mb. Files are named <prefix>_0001.tsv, _0002.tsv, etc.
#
# Usage:
#   # Collect from nom-dns-base (POP Kafka):
#   ./collect_dns_data.sh \
#     --brokers 10.0.0.82:9093 \
#     --topic nom-dns-base \
#     --output-dir /tmp/dns-capture-base \
#     --max-mb 50
#
#   # Collect from nom-dns-vertica (DC1 Kafka):
#   ./collect_dns_data.sh \
#     --brokers 10.0.0.8:9093 \
#     --topic nom-dns-vertica \
#     --output-dir /tmp/dns-capture-vertica \
#     --max-mb 50
#
#   # Stop after 1 GB total:
#   ./collect_dns_data.sh \
#     --brokers 10.0.0.8:9093 \
#     --topic nom-dns-vertica \
#     --output-dir /tmp/dns-capture-vertica \
#     --max-mb 50 \
#     --max-total-mb 1024
#
# Stop anytime with Ctrl+C. Partial files are kept.

set -euo pipefail

# ---- Defaults ----------------------------------------------------------------
BROKERS=""
TOPIC=""
OUTPUT_DIR=""
MAX_MB=50
MAX_TOTAL_MB=0  # 0 = unlimited
PREFIX=""
PARTITION=""

# ---- Parse arguments ---------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --brokers)     BROKERS="$2"; shift 2 ;;
        --topic)       TOPIC="$2"; shift 2 ;;
        --output-dir)  OUTPUT_DIR="$2"; shift 2 ;;
        --max-mb)      MAX_MB="$2"; shift 2 ;;
        --max-total-mb) MAX_TOTAL_MB="$2"; shift 2 ;;
        --prefix)      PREFIX="$2"; shift 2 ;;
        --partition)   PARTITION="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 --brokers <brokers> --topic <topic> --output-dir <dir> [--max-mb N] [--max-total-mb N] [--prefix name]"
            exit 0 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

if [[ -z "$BROKERS" || -z "$TOPIC" || -z "$OUTPUT_DIR" ]]; then
    echo "Error: --brokers, --topic, and --output-dir are required."
    echo "Run with --help for usage."
    exit 1
fi

# Default prefix from topic name (dots → underscores), include partition if set
if [[ -z "$PREFIX" ]]; then
    if [[ -n "$PARTITION" ]]; then
        PREFIX="${TOPIC//./_}_p${PARTITION}"
    else
        PREFIX="${TOPIC//./_}"
    fi
fi

# Build partition flag for nom-kafka-dump
PARTITION_FLAG=""
if [[ -n "$PARTITION" ]]; then
    PARTITION_FLAG="--partition $PARTITION"
fi

MAX_BYTES=$(( MAX_MB * 1024 * 1024 ))
MAX_TOTAL_BYTES=$(( MAX_TOTAL_MB * 1024 * 1024 ))

mkdir -p "$OUTPUT_DIR"

# ---- State -------------------------------------------------------------------
FILE_NUM=1
CURRENT_SIZE=0
TOTAL_BYTES=0
TOTAL_RECORDS=0
CURRENT_FILE=""

new_file() {
    CURRENT_FILE="$OUTPUT_DIR/${PREFIX}_$(printf '%04d' $FILE_NUM).tsv"
    FILE_NUM=$(( FILE_NUM + 1 ))
    CURRENT_SIZE=0
    echo "$(date '+%Y-%m-%d %H:%M:%S') Writing to $CURRENT_FILE" >&2
}

# ---- Signal handling ---------------------------------------------------------
RUNNING=true
trap 'RUNNING=false; echo ""; echo "$(date "+%Y-%m-%d %H:%M:%S") Stopping..." >&2' INT TERM

# ---- Status reporting --------------------------------------------------------
LAST_STATUS=$(date +%s)
STATUS_INTERVAL=30

print_status() {
    local now
    now=$(date +%s)
    if (( now - LAST_STATUS >= STATUS_INTERVAL )); then
        local total_h
        if (( TOTAL_BYTES < 1048576 )); then
            total_h="$(( TOTAL_BYTES / 1024 )) KB"
        elif (( TOTAL_BYTES < 1073741824 )); then
            total_h="$(( TOTAL_BYTES / 1048576 )) MB"
        else
            total_h="$(echo "scale=2; $TOTAL_BYTES / 1073741824" | bc) GB"
        fi
        echo "$(date '+%Y-%m-%d %H:%M:%S') Status: $TOTAL_RECORDS records, $total_h captured, $(( FILE_NUM - 1 )) files" >&2
        LAST_STATUS=$now
    fi
}

# ---- Main loop ---------------------------------------------------------------
echo "$(date '+%Y-%m-%d %H:%M:%S') Collecting from $TOPIC via $BROKERS" >&2
echo "$(date '+%Y-%m-%d %H:%M:%S') Output: $OUTPUT_DIR/${PREFIX}_NNNN.tsv (max ${MAX_MB} MB per file)" >&2
if (( MAX_TOTAL_MB > 0 )); then
    echo "$(date '+%Y-%m-%d %H:%M:%S') Will stop after ${MAX_TOTAL_MB} MB total" >&2
fi
echo "" >&2

new_file

# Stream nom-kafka-dump output and write to rotating files
nom-kafka-dump --brokers "$BROKERS" "$TOPIC" --one-line $PARTITION_FLAG 2>/dev/null | while IFS= read -r line; do
    if [[ "$RUNNING" != "true" ]]; then
        break
    fi

    LINE_BYTES=${#line}

    # Rotate if current file exceeds limit
    if (( CURRENT_SIZE + LINE_BYTES + 1 > MAX_BYTES && CURRENT_SIZE > 0 )); then
        new_file
    fi

    echo "$line" >> "$CURRENT_FILE"
    CURRENT_SIZE=$(( CURRENT_SIZE + LINE_BYTES + 1 ))
    TOTAL_BYTES=$(( TOTAL_BYTES + LINE_BYTES + 1 ))
    TOTAL_RECORDS=$(( TOTAL_RECORDS + 1 ))

    # Check total limit
    if (( MAX_TOTAL_MB > 0 && TOTAL_BYTES >= MAX_TOTAL_BYTES )); then
        echo "$(date '+%Y-%m-%d %H:%M:%S') Reached total limit (${MAX_TOTAL_MB} MB)" >&2
        break
    fi

    print_status
done

# Final status
if (( TOTAL_BYTES < 1048576 )); then
    TOTAL_H="$(( TOTAL_BYTES / 1024 )) KB"
elif (( TOTAL_BYTES < 1073741824 )); then
    TOTAL_H="$(( TOTAL_BYTES / 1048576 )) MB"
else
    TOTAL_H="$(echo "scale=2; $TOTAL_BYTES / 1073741824" | bc) GB"
fi

echo "" >&2
echo "$(date '+%Y-%m-%d %H:%M:%S') Done. $TOTAL_RECORDS records, $TOTAL_H captured across $(( FILE_NUM - 1 )) files in $OUTPUT_DIR" >&2
ls -lh "$OUTPUT_DIR"/${PREFIX}_*.tsv 2>/dev/null | awk '{print "  " $NF " (" $5 ")"}' >&2
