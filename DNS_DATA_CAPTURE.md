# DNS Data Capture Guide

Capture DNS data from Kafka for offline compression analysis. Two capture targets:

1. **`nom-dns-base`** — Raw DNS request objects from cache-serve/DAMP (pre-nom-link)
2. **`nom-dns-vertica`** — Processed, Vertica-ready records (post-nom-link tovertica transform)

Both use the same `kafka_to_file_dumper.py` script with different `--topic` arguments.

## Prerequisites

### On the VM where you run the dumper

1. **Python 3.12** — already installed on all deployment VMs
2. **kafka-python-ng** — already installed on VMs with the sidecar. On other VMs:
   ```bash
   sudo pip3 install kafka-python-ng
   ```
3. **The dumper script** — copy it to the VM:
   ```bash
   scp -F <terraform-dir>/ssh.config kafka_to_file_dumper.py centos@<VM>:/tmp/
   ```
4. **Disk space** — the dumper writes raw JSONL. DNS data can be high volume. Ensure
   the target directory has enough free space (check with `df -h /tmp`).

### Which VM to run on

Run the dumper on **any VM that can reach the Kafka brokers**. Good candidates:
- **amcx0** (Config Hub node) — has SSH access to everything, low resource usage
- **A link node** (dc1-link0) — close to the data, has Kafka access
- **A Kafka broker** (dc1-kafka0) — direct access, no network hop

The dumper is a Kafka consumer — it doesn't need to be on the same VM as the
producer. It just needs network access to the broker IPs (10.0.0.8/9/10:9093).

### DAMP must be running

The DNS topics only have data when DAMP (or a real cache-serve) is generating
DNS traffic. If the topics are empty, start DAMP first.

## Running the Dumpers

### Capture 1: Raw DNS data (nom-dns-base)

```bash
python3 /tmp/kafka_to_file_dumper.py \
  --brokers 10.0.0.8:9093,10.0.0.9:9093,10.0.0.10:9093 \
  --topic nom-dns-base \
  --output-dir /tmp/dns-capture-base \
  --max-file-mb 50 \
  --from-beginning
```

### Capture 2: Processed DNS data (nom-dns-vertica)

```bash
python3 /tmp/kafka_to_file_dumper.py \
  --brokers 10.0.0.8:9093,10.0.0.9:9093,10.0.0.10:9093 \
  --topic nom-dns-vertica \
  --output-dir /tmp/dns-capture-vertica \
  --max-file-mb 50 \
  --from-beginning
```

### Run both in parallel (recommended)

Run each in a separate terminal or use `&`:

```bash
python3 /tmp/kafka_to_file_dumper.py \
  --brokers 10.0.0.8:9093,10.0.0.9:9093,10.0.0.10:9093 \
  --topic nom-dns-base \
  --output-dir /tmp/dns-capture-base \
  --max-file-mb 50 &

python3 /tmp/kafka_to_file_dumper.py \
  --brokers 10.0.0.8:9093,10.0.0.9:9093,10.0.0.10:9093 \
  --topic nom-dns-vertica \
  --output-dir /tmp/dns-capture-vertica \
  --max-file-mb 50 &
```

Stop either with `Ctrl+C` or `kill %1` / `kill %2`.

## Command Reference

| Argument | Required | Default | Description |
|----------|----------|---------|-------------|
| `--brokers` | Yes | — | Kafka bootstrap servers (comma-separated) |
| `--topic` | Yes | — | Kafka topic to consume |
| `--output-dir` | Yes | — | Directory for captured files |
| `--max-file-mb` | No | 50 | Max file size in MB before rotation |
| `--from-beginning` | No | latest | Start from beginning of topic |
| `--max-records` | No | 0 (unlimited) | Stop after N records |
| `--max-bytes` | No | 0 (unlimited) | Stop after N bytes captured |
| `--group-id` | No | auto-generated | Kafka consumer group ID |
| `--tls` | No | off | Enable TLS for Kafka |
| `--tls-ca` | No | /var/nom/secrets/pki/ca-format-1.pem | TLS CA cert |
| `--tls-cert` | No | (default path) | TLS client cert |
| `--tls-key` | No | (default path) | TLS client key |
| `--log-level` | No | INFO | DEBUG, INFO, WARNING, ERROR |

## Output Format

Files are named `<topic>_NNNN.jsonl` (topic with dots replaced by underscores):

```
/tmp/dns-capture-base/
├── nom-dns-base_0001.jsonl     # ~50 MB
├── nom-dns-base_0002.jsonl     # ~50 MB
└── nom-dns-base_0003.jsonl     # still writing

/tmp/dns-capture-vertica/
├── nom-dns-vertica_0001.jsonl  # ~50 MB
├── nom-dns-vertica_0002.jsonl  # ~50 MB
└── nom-dns-vertica_0003.jsonl  # still writing
```

Each file contains one record per line (JSONL format), exactly as received from Kafka.

## Stopping Conditions

The dumper runs until you stop it (`Ctrl+C` / `SIGTERM`) or until a limit is hit:

```bash
# Stop after 1 GB captured:
python3 /tmp/kafka_to_file_dumper.py \
  --brokers 10.0.0.8:9093 \
  --topic nom-dns-vertica \
  --output-dir /tmp/dns-capture-vertica \
  --max-file-mb 50 \
  --max-bytes 1073741824

# Stop after 1 million records:
python3 /tmp/kafka_to_file_dumper.py \
  --brokers 10.0.0.8:9093 \
  --topic nom-dns-base \
  --output-dir /tmp/dns-capture-base \
  --max-file-mb 50 \
  --max-records 1000000
```

## Fetching Captured Files

After capture, SCP the files to your Mac:

```bash
scp -F <terraform-dir>/ssh.config \
  centos@<VM>:/tmp/dns-capture-base/*.jsonl \
  ~/Desktop/dns-samples-base/

scp -F <terraform-dir>/ssh.config \
  centos@<VM>:/tmp/dns-capture-vertica/*.jsonl \
  ~/Desktop/dns-samples-vertica/
```

## Quick Sanity Check After Capture

```bash
# On the VM, check what was captured:
echo "=== nom-dns-base ==="
ls -lh /tmp/dns-capture-base/
wc -l /tmp/dns-capture-base/*.jsonl | tail -1
head -1 /tmp/dns-capture-base/nom-dns-base_0001.jsonl | python3 -m json.tool | head -20

echo "=== nom-dns-vertica ==="
ls -lh /tmp/dns-capture-vertica/
wc -l /tmp/dns-capture-vertica/*.jsonl | tail -1
head -1 /tmp/dns-capture-vertica/nom-dns-vertica_0001.jsonl | python3 -m json.tool | head -20
```

## SSL Environments

For environments with SSL enabled, add `--tls`:

```bash
python3 /tmp/kafka_to_file_dumper.py \
  --brokers 10.0.0.8:9093 \
  --topic nom-dns-vertica \
  --output-dir /tmp/dns-capture-vertica \
  --max-file-mb 50 \
  --tls
```

## Important Notes

- **Consumer group isolation**: Each run auto-generates a unique group ID
  (`dumper-<topic>-<pid>`). This means each run starts fresh and doesn't
  interfere with production consumers (Data Loader, nom-link, etc.).

- **No impact on production pipeline**: The dumper is a read-only consumer.
  It does not modify, acknowledge, or remove messages. The production
  Data Loader and nom-link continue operating normally.

- **Disk space**: DNS data at high DAMP rates can generate hundreds of MB per
  minute. Monitor disk space (`df -h`) and use `--max-bytes` to set a cap.

- **Binary vs JSON records**: The `nom-dns-base` topic may contain binary
  (non-JSON) records depending on the cache-serve serialization format.
  If records are binary, the `.jsonl` file extension is misleading but the
  content is still valid for compression analysis.
