#!/usr/bin/env python3
"""Kafka-to-file dumper for DNS data capture.

Consumes from a Kafka topic and writes records to rotating files.
Each record is written as one line. Files rotate when the size
limit is reached.

Usage:
    # Capture raw DNS data (pre-nom-link):
    python3 kafka_to_file_dumper.py \
        --brokers 10.0.0.8:9093,10.0.0.9:9093,10.0.0.10:9093 \
        --topic nom-dns-base \
        --output-dir /tmp/dns-capture-base \
        --max-file-mb 50

    # Capture processed DNS data (post-nom-link, Vertica-ready):
    python3 kafka_to_file_dumper.py \
        --brokers 10.0.0.8:9093,10.0.0.9:9093,10.0.0.10:9093 \
        --topic nom-dns-vertica \
        --output-dir /tmp/dns-capture-vertica \
        --max-file-mb 50
"""

import argparse
import logging
import os
import signal
import sys
import time

logger = logging.getLogger("kafka-to-file-dumper")


class RotatingFileWriter:
    """Writes data to files that rotate at a max size."""

    def __init__(self, output_dir, prefix, max_bytes):
        self._dir = output_dir
        self._prefix = prefix
        self._max_bytes = max_bytes
        self._file_num = 1
        self._current_size = 0
        self._total_bytes = 0
        self._total_records = 0
        os.makedirs(output_dir, exist_ok=True)
        self._fh = self._open_next()

    def _open_next(self):
        path = os.path.join(
            self._dir, f"{self._prefix}_{self._file_num:04d}.jsonl"
        )
        self._current_size = 0
        logger.info("Writing to %s", path)
        return open(path, "wb")

    def write(self, data):
        """Write a record (bytes). Appends newline if not present."""
        if not data.endswith(b"\n"):
            data = data + b"\n"
        self._fh.write(data)
        self._current_size += len(data)
        self._total_bytes += len(data)
        self._total_records += 1
        if self._current_size >= self._max_bytes:
            self._fh.close()
            self._file_num += 1
            self._fh = self._open_next()

    def close(self):
        self._fh.close()

    @property
    def stats(self):
        return {
            "files": self._file_num,
            "total_bytes": self._total_bytes,
            "total_records": self._total_records,
        }


def main():
    parser = argparse.ArgumentParser(
        description="Consume from a Kafka topic and write to rotating files",
    )
    parser.add_argument(
        "--brokers", required=True,
        help="Kafka bootstrap servers (comma-separated)",
    )
    parser.add_argument(
        "--topic", required=True,
        help="Kafka topic to consume from",
    )
    parser.add_argument(
        "--output-dir", required=True,
        help="Directory to write captured files",
    )
    parser.add_argument(
        "--max-file-mb", type=int, default=50,
        help="Max file size in MB before rotation (default: %(default)s)",
    )
    parser.add_argument(
        "--group-id",
        help="Kafka consumer group ID (default: dumper-<topic>-<pid>)",
    )
    parser.add_argument(
        "--from-beginning", action="store_true",
        help="Start consuming from the beginning of the topic",
    )
    parser.add_argument(
        "--max-records", type=int, default=0,
        help="Stop after this many records (0 = unlimited)",
    )
    parser.add_argument(
        "--max-bytes", type=int, default=0,
        help="Stop after capturing this many bytes (0 = unlimited)",
    )
    parser.add_argument(
        "--no-tls", action="store_true", default=True,
        help="Disable TLS (default: TLS off)",
    )
    parser.add_argument(
        "--tls", action="store_true",
        help="Enable TLS for Kafka connections",
    )
    parser.add_argument(
        "--tls-ca", default="/var/nom/secrets/pki/ca-format-1.pem",
        help="TLS CA certificate path",
    )
    parser.add_argument(
        "--tls-cert",
        default="/var/nom/secrets/pki/cert-kafka-general-producer-format-1.pem",
        help="TLS client certificate path",
    )
    parser.add_argument(
        "--tls-key",
        default="/var/nom/secrets/pki/key-kafka-general-producer-format-1.key",
        help="TLS client key path",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: %(default)s)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    group_id = args.group_id or f"dumper-{args.topic}-{os.getpid()}"
    max_file_bytes = args.max_file_mb * 1024 * 1024
    prefix = args.topic.replace(".", "_")

    # Import kafka
    try:
        from kafka import KafkaConsumer
    except ImportError:
        logger.error("kafka-python-ng not installed. Run: pip3 install kafka-python-ng")
        sys.exit(1)

    # Build consumer config
    kwargs = dict(
        bootstrap_servers=args.brokers.split(","),
        group_id=group_id,
        auto_offset_reset="earliest" if args.from_beginning else "latest",
        enable_auto_commit=True,
        consumer_timeout_ms=5000,
        max_partition_fetch_bytes=10485760,
    )
    if args.tls:
        kwargs.update(
            security_protocol="SSL",
            ssl_cafile=args.tls_ca,
            ssl_certfile=args.tls_cert,
            ssl_keyfile=args.tls_key,
        )

    logger.info("Connecting to %s topic=%s group=%s", args.brokers, args.topic, group_id)
    consumer = KafkaConsumer(args.topic, **kwargs)

    writer = RotatingFileWriter(args.output_dir, prefix, max_file_bytes)

    # Graceful shutdown
    shutdown = False

    def _on_signal(signum, _frame):
        nonlocal shutdown
        logger.info("Received signal %d, stopping...", signum)
        shutdown = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    logger.info(
        "Dumping %s to %s (max %d MB per file)",
        args.topic, args.output_dir, args.max_file_mb,
    )

    last_status = time.monotonic()
    status_interval = 30  # log stats every 30 seconds

    try:
        while not shutdown:
            # Poll with timeout so we can check shutdown flag
            records = consumer.poll(timeout_ms=2000)
            for tp, messages in records.items():
                for msg in messages:
                    writer.write(msg.value)

                    # Check stop conditions
                    if args.max_records > 0 and writer.stats["total_records"] >= args.max_records:
                        logger.info("Reached max records (%d)", args.max_records)
                        shutdown = True
                        break
                    if args.max_bytes > 0 and writer.stats["total_bytes"] >= args.max_bytes:
                        logger.info("Reached max bytes (%d)", args.max_bytes)
                        shutdown = True
                        break
                if shutdown:
                    break

            # Periodic status
            now = time.monotonic()
            if now - last_status >= status_interval:
                s = writer.stats
                logger.info(
                    "Status: %d records, %s captured, %d files",
                    s["total_records"],
                    _fmt_bytes(s["total_bytes"]),
                    s["files"],
                )
                last_status = now

    finally:
        writer.close()
        consumer.close()
        s = writer.stats
        logger.info(
            "Done. %d records, %s captured across %d files in %s",
            s["total_records"],
            _fmt_bytes(s["total_bytes"]),
            s["files"],
            args.output_dir,
        )


def _fmt_bytes(n):
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
