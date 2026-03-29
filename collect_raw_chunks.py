#!/usr/bin/env python3
"""Collect raw GenericChunk binary data from Kafka.

Saves complete Kafka messages (raw bytes, as-is) to rotating files.
Each message is length-prefixed so they can be split apart later.

File format:
    [4B uint32 message_length][message_bytes]
    [4B uint32 message_length][message_bytes]
    ...

Usage:
    python3 collect_raw_chunks.py \
        --brokers 10.0.0.82:9093 \
        --topic nom-dns-base \
        --output-dir /tmp/dns-raw \
        --max-file-mb 50 \
        --max-total-mb 500
"""

import argparse
import logging
import os
import signal
import struct
import sys
import time

logger = logging.getLogger("collect-raw-chunks")


class RotatingBinaryWriter:
    """Writes length-prefixed binary messages to rotating files."""

    def __init__(self, output_dir, prefix, max_bytes):
        self._dir = output_dir
        self._prefix = prefix
        self._max_bytes = max_bytes
        self._file_num = 1
        self._current_size = 0
        self._total_bytes = 0
        self._total_messages = 0
        os.makedirs(output_dir, exist_ok=True)
        self._fh = self._open_next()

    def _open_next(self):
        path = os.path.join(
            self._dir, f"{self._prefix}_{self._file_num:04d}.bin"
        )
        self._current_size = 0
        logger.info("Writing to %s", path)
        return open(path, "wb")

    def write(self, data):
        """Write a length-prefixed message. Rotates if file exceeds limit."""
        frame = struct.pack(">I", len(data)) + data
        if self._current_size + len(frame) > self._max_bytes and self._current_size > 0:
            self._fh.close()
            self._file_num += 1
            self._fh = self._open_next()
        self._fh.write(frame)
        self._current_size += len(frame)
        self._total_bytes += len(frame)
        self._total_messages += 1

    def close(self):
        self._fh.close()

    @property
    def stats(self):
        return {
            "files": self._file_num,
            "total_bytes": self._total_bytes,
            "total_messages": self._total_messages,
        }


def main():
    parser = argparse.ArgumentParser(
        description="Collect raw GenericChunk bytes from Kafka",
    )
    parser.add_argument("--brokers", required=True)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-file-mb", type=int, default=50)
    parser.add_argument("--max-total-mb", type=int, default=0,
                        help="0 = unlimited")
    parser.add_argument("--max-messages", type=int, default=0,
                        help="0 = unlimited")
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--tls-ca", default="/var/nom/secrets/pki/ca-format-1.pem")
    parser.add_argument("--tls-cert",
                        default="/var/nom/secrets/pki/cert-kafka-general-producer-format-1.pem")
    parser.add_argument("--tls-key",
                        default="/var/nom/secrets/pki/key-kafka-general-producer-format-1.key")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    try:
        from kafka import KafkaConsumer
    except ImportError:
        logger.error("kafka-python-ng not installed. Run: pip3 install kafka-python-ng")
        sys.exit(1)

    kwargs = dict(
        bootstrap_servers=args.brokers.split(","),
        group_id=f"raw-collect-{args.topic}-{os.getpid()}",
        auto_offset_reset="latest",
        enable_auto_commit=True,
        consumer_timeout_ms=10000,
        max_partition_fetch_bytes=10485760,
    )
    if args.tls:
        kwargs.update(
            security_protocol="SSL",
            ssl_cafile=args.tls_ca,
            ssl_certfile=args.tls_cert,
            ssl_keyfile=args.tls_key,
        )

    try:
        import snappy  # noqa
    except ImportError:
        logger.warning("python-snappy not installed — install if Kafka uses Snappy: pip3 install python-snappy")

    prefix = args.topic.replace(".", "_")
    max_file_bytes = args.max_file_mb * 1024 * 1024
    max_total_bytes = args.max_total_mb * 1024 * 1024 if args.max_total_mb > 0 else 0

    logger.info("Connecting to %s topic=%s", args.brokers, args.topic)
    consumer = KafkaConsumer(args.topic, **kwargs)
    writer = RotatingBinaryWriter(args.output_dir, prefix, max_file_bytes)

    shutdown = False
    def _on_signal(signum, _frame):
        nonlocal shutdown
        logger.info("Received signal %d, stopping...", signum)
        shutdown = True
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    logger.info("Collecting raw chunks to %s (%d MB per file)",
                args.output_dir, args.max_file_mb)

    last_status = time.monotonic()

    try:
        while not shutdown:
            records = consumer.poll(timeout_ms=5000)
            if not records:
                continue
            for tp, messages in records.items():
                for msg in messages:
                    writer.write(msg.value)

                    s = writer.stats
                    if args.max_messages > 0 and s["total_messages"] >= args.max_messages:
                        logger.info("Reached %d messages", args.max_messages)
                        shutdown = True
                        break
                    if max_total_bytes > 0 and s["total_bytes"] >= max_total_bytes:
                        logger.info("Reached %d MB", args.max_total_mb)
                        shutdown = True
                        break

                    now = time.monotonic()
                    if now - last_status >= 30:
                        logger.info("Status: %d messages, %s",
                                    s["total_messages"], _fmt(s["total_bytes"]))
                        last_status = now
                if shutdown:
                    break
    finally:
        writer.close()
        consumer.close()
        s = writer.stats
        logger.info("")
        logger.info("Done. %d messages, %s across %d files in %s",
                    s["total_messages"], _fmt(s["total_bytes"]),
                    s["files"], args.output_dir)


def _fmt(n):
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    elif n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    else:
        return f"{n / (1024 * 1024 * 1024):.2f} GB"


if __name__ == "__main__":
    main()
