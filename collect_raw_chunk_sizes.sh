#!/usr/bin/env bash
# collect_raw_chunk_sizes.sh — Measure raw Kafka message sizes (Snappy-compressed GenericChunks).
#
# Consumes raw messages from a Kafka topic and logs the byte size of each message.
# This establishes the Snappy compression baseline — how large each chunk is on the wire.
#
# Also optionally saves the raw binary chunks to rotating files for offline analysis.
#
# Usage:
#   # Just measure sizes (no raw file capture):
#   ./collect_raw_chunk_sizes.sh \
#     --brokers 10.0.0.8:9093 \
#     --topic nom-dns-vertica \
#     --output-dir /tmp/dns-chunk-sizes \
#     --max-messages 1000
#
#   # Measure sizes AND save raw chunks:
#   ./collect_raw_chunk_sizes.sh \
#     --brokers 10.0.0.82:9093 \
#     --topic nom-dns-base \
#     --output-dir /tmp/dns-raw-chunks \
#     --save-raw \
#     --max-mb 500 \
#     --max-messages 1000

set -euo pipefail

BROKERS=""
TOPIC=""
OUTPUT_DIR=""
MAX_MESSAGES=0    # 0 = unlimited
SAVE_RAW=false
MAX_MB=500        # for raw file rotation
PREFIX=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --brokers)       BROKERS="$2"; shift 2 ;;
        --topic)         TOPIC="$2"; shift 2 ;;
        --output-dir)    OUTPUT_DIR="$2"; shift 2 ;;
        --max-messages)  MAX_MESSAGES="$2"; shift 2 ;;
        --save-raw)      SAVE_RAW=true; shift ;;
        --max-mb)        MAX_MB="$2"; shift 2 ;;
        --prefix)        PREFIX="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 --brokers <brokers> --topic <topic> --output-dir <dir> [--max-messages N] [--save-raw] [--max-mb N]"
            exit 0 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

if [[ -z "$BROKERS" || -z "$TOPIC" || -z "$OUTPUT_DIR" ]]; then
    echo "Error: --brokers, --topic, and --output-dir are required."
    exit 1
fi

if [[ -z "$PREFIX" ]]; then
    PREFIX="${TOPIC//./_}"
fi

mkdir -p "$OUTPUT_DIR"

SIZES_FILE="$OUTPUT_DIR/${PREFIX}_chunk_sizes.csv"
echo "message_num,bytes" > "$SIZES_FILE"

echo "$(date '+%Y-%m-%d %H:%M:%S') Measuring chunk sizes from $TOPIC" >&2
echo "$(date '+%Y-%m-%d %H:%M:%S') Sizes log: $SIZES_FILE" >&2
if [[ "$SAVE_RAW" == "true" ]]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') Also saving raw chunks to $OUTPUT_DIR" >&2
fi

# Export variables for the Python subprocess
export BROKERS TOPIC OUTPUT_DIR PREFIX MAX_MESSAGES SAVE_RAW MAX_MB

python3 << 'PYEOF'
import os, sys, signal, time, csv

brokers = os.environ["BROKERS"]
topic = os.environ["TOPIC"]
output_dir = os.environ["OUTPUT_DIR"]
prefix = os.environ["PREFIX"]
max_messages = int(os.environ["MAX_MESSAGES"])
save_raw = os.environ["SAVE_RAW"] == "true"
max_total_bytes = int(os.environ["MAX_MB"]) * 1024 * 1024

try:
    from kafka import KafkaConsumer
except ImportError:
    print("Error: kafka-python-ng not installed. Run: pip3 install kafka-python-ng", file=sys.stderr)
    sys.exit(1)

consumer = KafkaConsumer(
    topic,
    bootstrap_servers=brokers.split(","),
    group_id=f"chunk-sizer-{topic}-{os.getpid()}",
    auto_offset_reset="latest",
    enable_auto_commit=True,
    consumer_timeout_ms=10000,
    max_partition_fetch_bytes=10485760,
)

sizes_file = os.path.join(output_dir, f"{prefix}_chunk_sizes.csv")
sizes_fh = open(sizes_file, "w", newline="")
writer = csv.writer(sizes_fh)
writer.writerow(["message_num", "bytes"])

raw_fh = None
raw_file_num = 1
raw_file_size = 0
max_raw_file = 500 * 1024 * 1024  # 500 MB per raw file

if save_raw:
    raw_path = os.path.join(output_dir, f"{prefix}_raw_{raw_file_num:04d}.bin")
    raw_fh = open(raw_path, "wb")
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} Raw output: {raw_path}", file=sys.stderr)

shutdown = False
def on_signal(sig, frame):
    global shutdown
    shutdown = True
signal.signal(signal.SIGINT, on_signal)
signal.signal(signal.SIGTERM, on_signal)

msg_num = 0
total_bytes = 0
last_status = time.monotonic()

try:
    while not shutdown:
        records = consumer.poll(timeout_ms=5000)
        if not records:
            continue

        for tp, messages in records.items():
            for msg in messages:
                msg_num += 1
                msg_bytes = len(msg.value)
                total_bytes += msg_bytes
                writer.writerow([msg_num, msg_bytes])

                if save_raw and raw_fh:
                    raw_fh.write(msg.value)
                    raw_file_size += msg_bytes
                    if raw_file_size >= max_raw_file:
                        raw_fh.close()
                        raw_file_num += 1
                        raw_path = os.path.join(output_dir, f"{prefix}_raw_{raw_file_num:04d}.bin")
                        raw_fh = open(raw_path, "wb")
                        raw_file_size = 0
                        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} Raw output: {raw_path}", file=sys.stderr)

                if max_messages > 0 and msg_num >= max_messages:
                    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} Reached {max_messages} messages", file=sys.stderr)
                    shutdown = True
                    break

                if max_total_bytes > 0 and total_bytes >= max_total_bytes:
                    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} Reached size limit", file=sys.stderr)
                    shutdown = True
                    break

                now = time.monotonic()
                if now - last_status >= 30:
                    mb = total_bytes / (1024 * 1024)
                    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} Status: {msg_num} messages, {mb:.1f} MB raw", file=sys.stderr)
                    last_status = now

            if shutdown:
                break
finally:
    sizes_fh.close()
    if raw_fh:
        raw_fh.close()
    consumer.close()
    mb = total_bytes / (1024 * 1024)
    print(f"", file=sys.stderr)
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} Done. {msg_num} messages, {mb:.1f} MB total raw bytes", file=sys.stderr)
    print(f"  Sizes log: {sizes_file}", file=sys.stderr)
    if msg_num > 0:
        avg = total_bytes / msg_num
        print(f"  Avg chunk size: {avg/1024:.1f} KB", file=sys.stderr)
        print(f"  Min/Max: see {sizes_file}", file=sys.stderr)
PYEOF
