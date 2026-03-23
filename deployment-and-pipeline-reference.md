# Deployment and Pipeline Reference

Comprehensive reference for the test environments, Kafka architecture,
data pipeline, message formats, and service interaction patterns.

---

## Test Environments

### Environment 194 (prousogl-194)

- **Terraform**: `~/work/sst-template-migration/head/SB_load_192_dest/run/194/terraform`
- **SSH**: `ssh -F <terraform-dir>/ssh.config -o StrictHostKeyChecking=no centos@<hostname>`
- **SSH port**: 9922 (external from Mac), 22 (inter-VM)
- **SSH key**: `<terraform-dir>/id_rsa`
- **SSH config**: `<terraform-dir>/ssh.config` (external) / `deployment.ssh.config` (inter-VM)
- **Region**: us-lax
- **Release**: 19.3
- **Security/SSL**: Disabled
- **Domain**: `prousogl-194.com`

### Environment 1940 (prousogl-1940)

- **Terraform**: `~/work/sst-template-migration/head/SB_load_192_dest/run/1940/terraform`
- **Domain**: `prousogl-1940.com`
- All other settings identical to 194

---

## VM Inventory (both environments share the same layout)

### Control and Infrastructure

| IP | Hostname | Role |
|---|---|---|
| 10.0.0.2 | amcx0 | Config Hub — orchestrator, Ansible control node |
| 10.0.0.93 | grafana0 | Grafana monitoring dashboards |
| 10.0.0.94 | damp0 | DAMP — DNS load testing tool |
| 10.0.0.95 | jmeter0 | JMeter load testing |

### POP Tier

| IP | Hostname | Role |
|---|---|---|
| 10.0.0.6 | cacheserve0 | DNS server (cache-serve) |
| 10.0.0.7 | proxy0 | Proxy |
| 10.0.0.82-84 | pop-kafka0/1/2 | POP Kafka brokers (port 9093) |
| 10.0.0.85-86 | pop-streamproc0/1 | POP stream processors |
| 10.0.0.87-89 | pop-link0/1/2 | POP nom-link instances |
| 10.0.0.90-92 | pop-kvs0/1/2 | POP KVS |

### DC1 (primary data center)

| IP | Hostname | Role |
|---|---|---|
| 10.0.0.3-5 | dc1-vkms0/1/2 | VKMS |
| 10.0.0.8-10 | dc1-kafka0/1/2 | DC1 Kafka brokers (port 9093) |
| 10.0.0.14-16 | dc1-kvs0/1/2 | KVS |
| 10.0.0.20-28 | dc1-vertica0-8 | Vertica cluster (9 nodes) |
| 10.0.0.38-40 | dc1-pgaas0/1/2 | PgaaS |
| 10.0.0.44 | dc1-influx0 | InfluxDB / Data Loader |
| 10.0.0.46-47 | dc1-levant0/1 | Levant |
| 10.0.0.50-51 | dc1-objectindex0/1 | Object index |
| 10.0.0.54-55 | dc1-streamproc0/1 | Stream processors |
| 10.0.0.58 | dc1-nsm0 | nom-service-monitor |
| 10.0.0.60-61 | dc1-link0/1 | nom-link |
| 10.0.0.64 | dc1-iptracker0 | IP tracker |
| 10.0.0.66 | dc1-apps0 | Apps Portal |
| 10.0.0.68-69 | dc1-ssm0/1 | SSM |
| 10.0.0.72-73 | dc1-sportal0/1 | Subscriber Portal |
| 10.0.0.76 | dc1-listmanager0 | List Manager |
| 10.0.0.78-79 | dc1-cas0/1 | CAS |

### DC2 (secondary data center)

Same service layout as DC1, IPs offset:

| IP | Hostname | Role |
|---|---|---|
| 10.0.0.11-13 | dc2-kafka0/1/2 | DC2 Kafka brokers (port **9193**) |
| 10.0.0.17-19 | dc2-kvs0/1/2 | KVS |
| 10.0.0.29-37 | dc2-vertica0-8 | Vertica cluster (9 nodes) |
| 10.0.0.41-43 | dc2-pgaas0/1/2 | PgaaS |
| 10.0.0.45 | dc2-influx0 | InfluxDB / Data Loader |
| 10.0.0.48-49 | dc2-levant0/1 | Levant |
| 10.0.0.52-53 | dc2-objectindex0/1 | Object index |
| 10.0.0.56-57 | dc2-streamproc0/1 | Stream processors |
| 10.0.0.59 | dc2-nsm0 | nom-service-monitor |
| 10.0.0.62-63 | dc2-link0/1 | nom-link |
| 10.0.0.65 | dc2-iptracker0 | IP tracker |
| 10.0.0.67 | dc2-apps0 | Apps Portal |
| 10.0.0.70-71 | dc2-ssm0/1 | SSM |
| 10.0.0.74-75 | dc2-sportal0/1 | Subscriber Portal |
| 10.0.0.77 | dc2-listmanager0 | List Manager |
| 10.0.0.80-81 | dc2-cas0/1 | CAS |

---

## Kafka Clusters

Three separate Kafka clusters per environment:

| Cluster | Brokers | Port | Zookeeper Port | Purpose |
|---|---|---|---|---|
| POP Kafka | pop-kafka0/1/2 (10.0.0.82-84) | 9093 | 2182 | Receives raw DNS from cache-serve/DAMP |
| DC1 Kafka | dc1-kafka0/1/2 (10.0.0.8-10) | 9093 | 2182 | Primary DC — processed data, telemetry, Vertica pipeline |
| DC2 Kafka | dc2-kafka0/1/2 (10.0.0.11-13) | 9193 | 2182 | Secondary DC — mirrored data |

### Kafka Management (SSL-disabled environments)

When SSL is off, `NOM_KAFKA_ZOOKEEPER_OVERRIDE` must be set for topic management:

```bash
export NOM_KAFKA_ZOOKEEPER_OVERRIDE="<BROKER_PRIVATE_IP>:2182"
```

**Topic creation** requires a JSON definition file placed at
`/usr/local/nom/etc/kafka-topics/topics/` on the broker, then:

```bash
/usr/local/nom/sbin/nom-kafka-configure-topics --brokers <PRIVATE_IP>:9093 --update --whitelist <topic-name>
```

Topic JSON definition format (example from `~/work/kafka-topics/head/topics/telemetry.json`):
```json
{
    "topics": [
        {
            "topic": "nom-telemetry",
            "partitions": 1,
            "tags": ["telemetry", "dc", "pop", "dc-pop"],
            "weight": 3,
            "mandatory": true,
            "producer-roles": ["kafka-general-producer"],
            "consumer-roles": ["kafka-general-consumer"]
        }
    ]
}
```

Admin client properties (for SSL-enabled commands): `/usr/local/nom/etc/nom-kafka/admin-client.properties`

### Kafka Data Directories

Topic data is stored at `/var/nom/nom-kafka/data/<topic-name>-<partition>/`.
Segment files are `.log` files within each partition directory, capped at 672 MB per segment.

---

## Kafka Topics

### DNS Topics

| Topic | Kafka Cluster | Per-Partition Size | Partitions | Format |
|---|---|---|---|---|
| `nom-dns-base` | POP Kafka | **~7.1 GB** | multiple | GenericChunk + TLV (Snappy) |
| `nom-dns-base` | DC1 Kafka | ~12 KB | 8+ | GenericChunk + TLV (Snappy) |
| `nom-dns-fqdn` | DC1 | varies | — | GenericChunk |
| `nom-dns-grouper` | DC1 | varies | — | GenericChunk |
| `nom-dns-res` | DC1 | varies | — | GenericChunk |
| `nom-dns-vertica` | DC1 Kafka | **~1.7 GB** | 20 | GenericChunk + TLV (Snappy) |
| `mirror.nom-dns-base` | DC1 | varies | — | GenericChunk + TLV (Snappy) |
| `mirror.nom-dns-fqdn` | DC1 | varies | — | GenericChunk |

### Telemetry Topics

| Topic | Kafka Cluster | Format |
|---|---|---|
| `nom-telemetry` | DC1 Kafka | Plain JSONL (not compressed) |
| `mirror.nom-telemetry` | DC2 Kafka | Plain JSONL |
| `merge.nom-telemetry` | DC1 Kafka | Plain JSONL (consumed by Data Loader for InfluxDB) |

### Topic Retention

Topics have `retention.bytes` configured (typically ~8 GB). Once the on-disk size
reaches the cap, old segments are purged as new data arrives. This means the
on-disk size stabilizes at the retention limit.

---

## Message Formats

### GenericChunk Wire Format (DNS data)

All DNS topics (`nom-dns-base`, `nom-dns-vertica`, etc.) use the GenericChunk
binary container format. Each Kafka message is one GenericChunk.

```
┌──────────────────┬───────────────┬──────────────────┬─────────────────┬──────────────────┬──────────────┐
│ 4B overall-length│ 1B comp-type  │ 4B header-length │ protobuf header │ 4B data-length   │ payload data │
└──────────────────┴───────────────┴──────────────────┴─────────────────┴──────────────────┴──────────────┘
```

**Compression type byte**: 0 = NONE, 1 = ZLIB, 2 = SNAPPY (default for DNS)

**Protobuf header** (`ChunkHeader` defined in `~/work/link-protobuf/head/Chunk.proto`):

| Field | Type | Example |
|---|---|---|
| type | string | "dns" |
| source | string | "base", "vertica" |
| format | string | "tlv" |
| timeStamp | uint64 | seconds since epoch |
| nodeID | string | hostname/UUID |
| engineType | string | "cacheserve" |
| engineVersion | string | software version |
| hostname | string | VM hostname |
| anonymizerKeyHash | string | optional |
| kafkaKey | bytes | optional |
| kafkaHeaders | repeated | optional |

**Code references**:
- Wire format: `~/work/java-common/head/link-store/src/main/java/com/nominum/common/link/store/GenericChunk.java`
- Protobuf: `~/work/link-protobuf/head/Chunk.proto`
- Compression: `~/work/link-protobuf/head/Stream.proto` (CompressionType enum)
- Compressor factory: `~/work/java-common/head/link-store/src/main/java/com/nominum/common/link/store/compression/DefaultCompressorFactory.java`

### TLV Payload Format (inside GenericChunk)

The payload inside each GenericChunk is a batch of DNS records encoded in
Type-Length-Value format. The payload is Snappy-compressed by default.

**Per-record structure**:
```
[4B record-length][8B startTime][4B flags][2B clientPort][1B inetFamily][TLV fields...]
```

**Per-field structure**:
```
[1B type][2B length][data bytes]
```

**TLV field types** (from `~/work/java-common/head/link-store/src/main/java/com/nominum/common/link/store/queries/DnsQuery.java`):

| Type ID | Name | Data |
|---|---|---|
| 0 | ENDTIME | uint32 (delta from startTime) |
| 1 | DNS_MESSAGE | raw DNS wire-format bytes |
| 2 | CLIENT_ADDRESS | 4 bytes (IPv4) or 16 bytes (IPv6) |
| 3 | SERVER_ADDRESS | 4 or 16 bytes |
| 4 | SERVER_PORT | uint16 |
| 5 | VIEW | string (policy view name) |
| 6 | ZONE | DNS name in wire format |
| 7+ | ... | 23+ more types: policy, tags, device-id, annotations, etc. |

**Typical per-record size**: 200-500 bytes uncompressed.

### GenericChunk Size and Record Count

Each Kafka message (GenericChunk) contains a **batch of DNS records**:

- **~5,600 DNS records per GenericChunk**
- **~950 KB decoded text per chunk** (tab-delimited output from nom-kafka-dump)
- **2 GenericChunks = 11,371 records = 1.9 MB decoded text**

### Decoded DNS Record Format

`nom-kafka-dump` decodes GenericChunks into tab-delimited text, one DNS query per line:

```
Col 1:  timestamp (microseconds)     1774142400000000
Col 2:  query-uuid                   b606ace5-3d25-4e0e-840a-12d0189cd97e
Col 3:  client-id                    a0:07:31:01:20 (or UUID format)
Col 4:  query-name (domain)          google.com
Col 5:  (empty)
Col 6:  core-domain                  google.com
Col 7:  node-id                      a8793283-58bd-4355-aae7-b80bd6417e89
Col 8-14: (empty fields)
Col 15: qtype                        A, AAAA, PTR, MX
Col 16: rcode                        noerror, nxdomain, refused
Col 17: flags1                       0
Col 18: flags2                       0
Col 19: numeric-id                   683838194
Col 20: (empty)
Col 21: count/ttl                    1, 2, 3
```

**Patterns in DNS data**:
- Timestamps are identical within a batch (all records in one chunk share the same second)
- node-id repeats on every row (same server generated the batch)
- qtype has low cardinality (A, AAAA, PTR, MX, and a few others)
- rcode is almost always "noerror" (with rare nxdomain, refused)
- Domains have moderate cardinality (google.com, apple.com, etc.)
- Many columns are consistently empty (consecutive tabs)

### Telemetry Message Format (Plain JSONL)

The `nom-telemetry` topic contains plain UTF-8 JSONL. Each Kafka message is one
or more newline-delimited JSON objects. Not wrapped in GenericChunk, not
pre-compressed.

Example record:
```json
{"current-time":1774142400,"type":"cpu","creator":"nom-telegraf","node-id":"8229b136-08ed-50e5-9421-e921957d91aa","host-name":"dc1-nsm0.prousogl-194.com","content":{"usage_idle":98.5,"usage_system":0.8,"usage_user":0.7}}
```

Telegraf emits system metrics (cpu, mem, disk, kernel, etc.) every 5 seconds
plus service-specific metrics from each service running on the VM.

---

## Data Pipeline Architecture

### DNS Pipeline (full flow)

```
DAMP (damp0)
    │
    ▼
cache-serve (cacheserve0)
    │  Generates DNS request objects in GenericChunk/TLV format
    │  Snappy-compressed before writing to Kafka
    ▼
POP Kafka: "nom-dns-base" (~7.1 GB/partition)
    │
    ▼
nom-link (pop-link0/1/2) — consumes via multiple consumer transforms:
    │  dns-fqdn-consumer-1/2/3
    │  dns-fqdn-mirrored-consumer-1/2/3
    │  dns-grouped-consumer-1/2/3
    │  dns-grouped-mirrored-consumer-1/2/3
    │
    ▼
dns-tovertica transform (4 threads)
    │  Prepares DNS data for Vertica ingestion
    │  Selects/restructures fields
    ▼
dns-tovertica-producer
    │
    ▼
DC1 Kafka: "nom-dns-vertica" (~1.7 GB/partition x 20 partitions)
    │
    ▼
Data Loader (dc1-influx0)
    │  Auto-detects GenericChunk format
    │  Decompresses Snappy, parses TLV
    │  Uses Vertica DnsParser() for COPY
    ▼
Vertica (dc1-vertica0-8)
```

### Telemetry Pipeline

```
All VMs (Telegraf agent, every 5s)
    │
    ▼
DC1 Kafka: "nom-telemetry" (plain JSONL)
    │
    ├──▶ nom-link mirror: "mirror.nom-telemetry" → DC2 Kafka
    │
    ▼
Merge process: "merge.nom-telemetry"
    │
    ▼
Data Loader (dc1-influx0) → InfluxDB
    │
    ▼
Grafana (grafana0)
```

### Mirroring and Merge

Data flows between DCs via MirrorMaker:
```
nom-telemetry (DC1) → mirror.nom-telemetry (DC2)
nom-dns-base (POP)  → mirror.nom-dns-base (DC1)
```

Merge processes combine local and mirrored data:
```
nom-telemetry + mirror.nom-telemetry → merge.nom-telemetry
```

Data Loader consumes from the `merge.*` topics.

### Data Loader Auto-Detection

Data Loader (`~/work/nom-data-loader/head`) auto-detects the message format by
reading the first chunk:
- If GenericChunk header parses and type is "dns" → **DNS_TLV** format → uses `DnsParser()`
- If GenericChunk header parses and type is "proxy-transaction" → **PROXY** format
- If header parsing fails → assumes **DNS_TEXT** (tab-delimited fallback)

Code: `VerticaLoadContext.java` `maybeAutoDetect()` method.

---

## nom-link Transform System

nom-link runs on link VMs and processes data through configurable transform
pipelines. Transforms are the building blocks of the data pipeline.

### Transform Types

| Kind | Purpose |
|---|---|
| `kafka-consumer` | Reads from a Kafka topic in a specific format |
| `kafka-producer` | Writes to a Kafka topic in a specific format |
| `tovertica` | Transforms DNS data for Vertica ingestion |
| Various others | Filtering, enrichment, routing transforms |

### Transform Anatomy

Each transform has these fields:
- **name**: unique identifier (e.g., `dns-tovertica-producer`)
- **kind**: type + configuration (e.g., kafka-consumer with format, topic, brokers)
- **destination**: next transform in the chain
- **pipeline**: full ordered list of transforms from source to sink
- **threads**: parallelism (for processing transforms)
- **format**: data serialization format (e.g., `nom-dns-base`, `nom-dns-vertica`, `nom-telemetry`)
- **brokers**: Kafka broker addresses (uses `#` as port separator, e.g., `10.0.0.8#9093`)

### Inspecting Transforms

```bash
# SSH to a link VM first
ssh centos@dc1-link0

# List all transforms
nom-tell nom-link transform.mget layer=*

# Inspect a specific transform
nom-tell nom-link transform.get layer=* name='dns-tovertica-producer'
```

### Key DNS Pipeline Transforms

**Consumers** (read from Kafka):
- `dns-fqdn-consumer-1/2/3` → consume from DNS FQDN topics
- `dns-fqdn-mirrored-consumer-1/2/3` → consume from mirrored DNS FQDN topics
- `dns-grouped-consumer-1/2/3` → consume from DNS grouped topics
- `dns-grouped-mirrored-consumer-1/2/3` → consume from mirrored DNS grouped topics

**Processing**:
- `dns-tovertica` → transforms DNS records for Vertica (4 threads)

**Producers** (write to Kafka):
- `dns-tovertica-producer` → produces to `nom-dns-vertica` topic (format: `nom-dns-vertica`, brokers: DC1 Kafka)

**Telemetry mirror pipeline**:
- `telemetry-mirror-to-dc-test-1-consumer` → consumes `nom-telemetry` from DC1 Kafka
- `telemetry-mirror-to-dc-test-1-producer` → produces `mirror.nom-telemetry` to DC2 Kafka

### nom-link Code

Source: `~/work/nom-link/head`

Transform kind definitions and available fields:
`~/work/nom-link/head/schema/transform-kinds.schema` (tovertica-params at lines 488-569)

---

## Monitoring

### Topic Disk Usage

Each Kafka broker runs a Telegraf `exec` input (`/var/nom/nom-telegraf/telegraf.d/topic-diskusage.conf`)
that calls `/var/nom/nom-telegraf/topic-diskusage.sh` every 2 minutes.

The script:
1. Runs `kafka-topics --list` to enumerate all topics
2. Runs `du` on `/var/nom/nom-kafka/data/<topic>*` for each topic
3. Outputs InfluxDB line protocol: `topic_disk_metrics,host=...,topic=... size=...`

This data flows through Telegraf → `nom-telemetry` Kafka topic → Data Loader → InfluxDB → Grafana.

### Kafka Cluster Health

`/usr/local/nom/sbin/nom-kafka-cluster-health` outputs JSON with broker status,
partition counts, replication state. Collected by Telegraf via
`/var/nom/nom-telegraf/telegraf.d/kafka_cluster_health_check.conf`.

### Telegraf Health Check

Each VM's Telegraf exposes a health endpoint at `http://localhost:8083/`.
Readiness is checked via the `telegraf-ready.yaml` Ansible playbook.

---

## Service Command Channel (nom-tell)

All services support an interactive command channel for inspection and management:

```bash
# Interactive mode
nom-tell <service-name>

# One-shot command
nom-tell <service-name> <command> <arguments>
```

### Examples

```bash
# List all nom-link transforms
nom-tell nom-link transform.mget layer=*

# Inspect a specific transform
nom-tell nom-link transform.get layer=* name='dns-tovertica-producer'

# General pattern for inspecting service state
nom-tell <service> <object-type>.mget <filters>
nom-tell <service> <object-type>.get <key=value>
```

---

## Tools Reference

| Tool | Path | Purpose |
|---|---|---|
| `nom-kafka-dump` | `/usr/local/nom/sbin/` | Decode and display Kafka messages (understands GenericChunk) |
| `kafka-topics` | `/usr/local/nom/sbin/` | Topic management (list, describe, create) |
| `nom-kafka-configure-topics` | `/usr/local/nom/sbin/` | Create/update topics from JSON definitions |
| `kafka-console-consumer` | `/usr/local/nom/sbin/` | Raw byte Kafka consumer |
| `nom-kafka-cluster-health` | `/usr/local/nom/sbin/` | Cluster health JSON |
| `nom-tell` | system path | Service command channel |
| `kafka-log-dirs` | `/usr/local/nom/sbin/` | Per-topic on-disk size reporting |
| `kafka-consumer-groups` | `/usr/local/nom/sbin/` | Consumer group management and lag |

### nom-kafka-dump Usage

```bash
nom-kafka-dump [OPTIONS] <topic>

Options:
  --brokers <brokers>      Kafka broker addresses
  --format <format>        Data format (usually auto-detected)
  --limit <N>              Number of Kafka messages (GenericChunks) to read
  --one-line               One record per line (tab-delimited)
  -e                       Exit at end of topic
  --offset <offset>        Start offset
  --partition <N>          Specific partition
  -H                       Hex dump on error
```

**Important**: `--limit N` means N **Kafka messages** (GenericChunks), NOT N records.
Each GenericChunk contains ~5,600 DNS records. So `--limit 2` produces ~11,000 records.

The format is usually auto-detected from the GenericChunk header. Specifying
`--format nom-dns-vertica` may cause "Unknown topic format" errors — omit it
and let auto-detection work.

---

## Python Environments on VMs

Two Python interpreters exist on all VMs:

| Interpreter | Version | Path | Used By |
|---|---|---|---|
| platform-python | 3.6 | `/usr/libexec/platform-python` | Ansible (auto-detected) |
| python3 | 3.12 | `/usr/bin/python3` | Applications, pip packages |

When installing Python packages via Ansible, always specify `executable: /usr/bin/pip3`
to install into Python 3.12. Otherwise Ansible's `pip` module installs into
Python 3.6's site-packages.

---

## Code Repositories

| Repo | Path | Purpose |
|---|---|---|
| Config Hub | `~/work/nom-config-hub/head` | Orchestrator, Ansible playbooks, service definitions |
| Data Loader | `~/work/nom-data-loader/head` | Kafka → Vertica/InfluxDB loader (Java 11, Maven) |
| nom-link | `~/work/nom-link/head` | Data transform pipeline |
| cache-serve | `~/work/cacheserve/head` | DNS server |
| DAMP | `~/work/damp/head` | DNS load testing tool |
| java-common | `~/work/java-common/head` | GenericChunk, TLV parser (link-store module) |
| link-protobuf | `~/work/link-protobuf/head` | Chunk.proto, Stream.proto definitions |
| kafka-topics | `~/work/kafka-topics/head` | Topic JSON definitions |
| sps-migration-tools | `~/work/sps-migration-tools/head` | Ansible roles (topic creation, migration) |
| sst-template-migration | `~/work/sst-template-migration/head` | Terraform, environment configs |

---

## Ansible Patterns (Config Hub)

### Playbook Location

`~/work/nom-config-hub/head/ansible/playbooks/`

### Conventions

- `hosts: "{{ ems_hosts }}"` — target specified via extra-vars
- `gather_facts: no` — always (unless facts are needed)
- Variable prefix: `ems_*` for externally-passed variables
- Templates in `ansible/playbooks/templates/` (.j2 files)
- Static files in `ansible/playbooks/files/`
- No roles in this repo — roles imported from external repos

### Service Control

```yaml
# Start/stop/restart via service-common.yaml pattern
- service:
    name: "{{ ems_service_name }}"
    state: "{{ ems_service_state }}"
  register: result
  retries: 3
  delay: 5
  until: result is success
```

### Telegraf Configuration

Telegraf configs live at `/var/nom/nom-telegraf/telegraf.d/` on each VM.
The main config is at `/var/nom/nom-telegraf/telegraf.conf`.
Telegraf service name: `nom-telegraf`.

Key config files:
- `nom_kafka_telemetry_output.conf` — Kafka output (topic, brokers)
- `default_inputs.conf` — system metrics (cpu, mem, disk, etc.)
- `health-check.conf` — health endpoint at port 8083
- `topic-diskusage.conf` — topic size monitoring (Kafka brokers only)
- `topic-lag.conf` — consumer lag monitoring (Kafka brokers only)

Telegraf output plugin is `nom_kafka` (custom plugin in the `nom-telegraf` RPM),
using `data_format = "nom_json"` (custom serializer).
