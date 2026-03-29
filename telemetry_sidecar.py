#!/usr/bin/env python3
"""Telemetry compression sidecar daemon.

Sits between Telegraf and Kafka on each VM. Accepts JSONL metrics via Unix
domain socket, batches them, compresses using the telemetry pipeline, and
produces the compressed blob to Kafka.

Usage:
    # Production mode (with Kafka):
    python3 telemetry_sidecar.py \\
        --socket /var/run/nom-telemetry-sidecar/telemetry.sock \\
        --kafka-brokers broker1:9093,broker2:9093 \\
        --kafka-topic nom-telemetry \\
        --tls-ca /var/nom/secrets/pki/ca-format-1.pem \\
        --tls-cert /var/nom/secrets/pki/cert-kafka-general-producer-format-1.pem \\
        --tls-key /var/nom/secrets/pki/key-kafka-general-producer-format-1.key

    # Dry-run mode (no Kafka, writes compressed batches to directory):
    python3 telemetry_sidecar.py \\
        --socket /tmp/test.sock \\
        --output-dir /tmp/compressed-output \\
        --batch-time 5
"""

import argparse
import logging
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

from telemetry_service import compress

logger = logging.getLogger("telemetry-sidecar")


# ---------------------------------------------------------------------------
# Kafka producer abstraction
# ---------------------------------------------------------------------------

def _create_kafka_producer(brokers, tls_ca, tls_cert, tls_key):
    """Create a Kafka producer with TLS.

    Tries confluent-kafka first, falls back to kafka-python.
    """
    try:
        from confluent_kafka import Producer

        config = {
            "bootstrap.servers": brokers,
            "security.protocol": "SSL",
            "ssl.ca.location": tls_ca,
            "ssl.certificate.location": tls_cert,
            "ssl.key.location": tls_key,
            "compression.type": "none",
            "message.max.bytes": 10485760,  # 10 MB
        }
        producer = Producer(config)

        class ConfluentWrapper:
            def send(self, topic, value):
                producer.produce(topic, value=value)
                producer.flush(timeout=30)

            def close(self):
                producer.flush(timeout=30)

        logger.info("Using confluent-kafka producer")
        return ConfluentWrapper()
    except ImportError:
        pass

    try:
        from kafka import KafkaProducer

        producer = KafkaProducer(
            bootstrap_servers=brokers.split(","),
            security_protocol="SSL",
            ssl_cafile=tls_ca,
            ssl_certfile=tls_cert,
            ssl_keyfile=tls_key,
            compression_type=None,
            max_request_size=10485760,
        )

        class KafkaPythonWrapper:
            def send(self, topic, value):
                future = producer.send(topic, value=value)
                future.get(timeout=30)

            def close(self):
                producer.close(timeout=30)

        logger.info("Using kafka-python producer")
        return KafkaPythonWrapper()
    except ImportError:
        raise ImportError(
            "No Kafka library available. Install confluent-kafka or kafka-python."
        )


# ---------------------------------------------------------------------------
# Thread-safe metric buffer
# ---------------------------------------------------------------------------

class MetricBuffer:
    """Accumulates JSONL data and flushes on time or size threshold."""

    def __init__(self, max_bytes, max_seconds):
        self._lock = threading.Lock()
        self._data = bytearray()
        self._max_bytes = max_bytes
        self._max_seconds = max_seconds
        self._last_flush = time.monotonic()

    def append(self, chunk):
        """Append raw bytes (JSONL fragment) to the buffer."""
        with self._lock:
            self._data.extend(chunk)

    def should_flush(self):
        """Return True if a flush threshold has been reached."""
        with self._lock:
            if not self._data:
                return False
            if len(self._data) >= self._max_bytes:
                return True
            if time.monotonic() - self._last_flush >= self._max_seconds:
                return True
            return False

    def drain(self):
        """Atomically return all buffered data and reset the buffer."""
        with self._lock:
            data = bytes(self._data)
            self._data.clear()
            self._last_flush = time.monotonic()
            return data

    @property
    def size(self):
        with self._lock:
            return len(self._data)


# ---------------------------------------------------------------------------
# Unix socket listener
# ---------------------------------------------------------------------------

def _handle_connection(conn, buffer):
    """Read JSONL lines from a single Telegraf connection into the buffer."""
    try:
        with conn:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                buffer.append(data)
    except OSError:
        pass  # Connection closed or socket shut down
    except Exception:
        logger.exception("Error handling connection")


def _run_socket_listener(sock_path, buffer, shutdown_event):
    """Accept connections on the Unix socket and spawn handler threads."""
    sock_path = Path(sock_path)
    if sock_path.exists():
        sock_path.unlink()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(str(sock_path))
    server.listen(16)
    server.settimeout(1.0)

    os.chmod(str(sock_path), 0o777)
    logger.info("Listening on %s", sock_path)

    try:
        while not shutdown_event.is_set():
            try:
                conn, _ = server.accept()
                t = threading.Thread(
                    target=_handle_connection,
                    args=(conn, buffer),
                    daemon=True,
                )
                t.start()
            except socket.timeout:
                continue
    finally:
        server.close()
        try:
            sock_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Batch flush: compress + produce
# ---------------------------------------------------------------------------

def _flush_batch(batch_data, batch_num, topic, producer, output_dir, models_dir):
    """Compress a batch and either produce to Kafka or write to output dir."""
    if not batch_data:
        return

    input_size = len(batch_data)
    tmpdir = Path(tempfile.mkdtemp(prefix="sidecar_"))
    try:
        input_file = tmpdir / f"batch_{batch_num:06d}.jsonl"
        output_file = tmpdir / f"batch_{batch_num:06d}.zljsonl"

        input_file.write_bytes(batch_data)
        compress(str(input_file), str(output_file), models_dir=models_dir)

        compressed_data = output_file.read_bytes()
        output_size = len(compressed_data)
        ratio = input_size / output_size if output_size > 0 else 0

        if output_dir:
            dest = Path(output_dir) / f"batch_{batch_num:06d}.zljsonl"
            dest.write_bytes(compressed_data)
            logger.info(
                "Batch %d: %s -> %s (%.1fx) -> %s",
                batch_num, _fmt_bytes(input_size), _fmt_bytes(output_size),
                ratio, dest,
            )
        else:
            producer.send(topic, value=compressed_data)
            logger.info(
                "Batch %d: %s -> %s (%.1fx) -> Kafka",
                batch_num, _fmt_bytes(input_size), _fmt_bytes(output_size),
                ratio,
            )
    except Exception:
        logger.exception("Failed to compress/produce batch %d", batch_num)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _fmt_bytes(n):
    """Format byte count for log messages."""
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    else:
        return f"{n / (1024 * 1024):.1f} MB"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Telemetry compression sidecar daemon",
    )
    parser.add_argument(
        "--socket",
        default="/var/run/nom-telemetry-sidecar/telemetry.sock",
        help="Unix socket path (default: %(default)s)",
    )
    parser.add_argument(
        "--kafka-brokers",
        help="Kafka bootstrap servers (comma-separated). "
             "Required unless --output-dir is set.",
    )
    parser.add_argument(
        "--kafka-topic", default="nom-telemetry",
        help="Kafka topic (default: %(default)s)",
    )
    parser.add_argument(
        "--tls-ca",
        default="/var/nom/secrets/pki/ca-format-1.pem",
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
        "--batch-time", type=int, default=60,
        help="Max seconds between flushes (default: %(default)s)",
    )
    parser.add_argument(
        "--batch-bytes", type=int, default=1048576,
        help="Max buffer bytes before flush (default: %(default)s = 1 MB)",
    )
    parser.add_argument(
        "--output-dir",
        help="Dry-run mode: write compressed batches here instead of Kafka.",
    )
    parser.add_argument(
        "--models-dir",
        help="Path to models directory (default: auto-detect relative to "
             "telemetry_service.py location)",
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

    if not args.output_dir and not args.kafka_brokers:
        parser.error("Either --kafka-brokers or --output-dir is required.")

    # Set up producer (or dry-run output directory)
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
        producer = None
        logger.info("Dry-run mode: writing to %s", args.output_dir)
    else:
        producer = _create_kafka_producer(
            args.kafka_brokers, args.tls_ca, args.tls_cert, args.tls_key,
        )
        logger.info(
            "Producing to %s topic=%s", args.kafka_brokers, args.kafka_topic,
        )

    buffer = MetricBuffer(
        max_bytes=args.batch_bytes, max_seconds=args.batch_time,
    )
    shutdown_event = threading.Event()
    batch_num = 0

    def _on_signal(signum, _frame):
        logger.info("Received signal %d, shutting down...", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    listener_thread = threading.Thread(
        target=_run_socket_listener,
        args=(args.socket, buffer, shutdown_event),
        daemon=True,
    )
    listener_thread.start()

    logger.info(
        "Sidecar started: batch_time=%ds, batch_bytes=%s",
        args.batch_time, _fmt_bytes(args.batch_bytes),
    )

    # Main flush loop — check every second, flush when thresholds met
    try:
        while not shutdown_event.is_set():
            shutdown_event.wait(timeout=1.0)
            if buffer.should_flush():
                batch_data = buffer.drain()
                if batch_data:
                    batch_num += 1
                    _flush_batch(
                        batch_data, batch_num, args.kafka_topic,
                        producer, args.output_dir, args.models_dir,
                    )
    finally:
        # Flush remaining data on shutdown
        batch_data = buffer.drain()
        if batch_data:
            batch_num += 1
            logger.info("Flushing final batch on shutdown...")
            _flush_batch(
                batch_data, batch_num, args.kafka_topic,
                producer, args.output_dir, args.models_dir,
            )
        if producer:
            producer.close()
        logger.info("Sidecar stopped. %d batches processed.", batch_num)


if __name__ == "__main__":
    main()
