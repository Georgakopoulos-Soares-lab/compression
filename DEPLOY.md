# Deploying the Telemetry Compression Sidecar

Two scripts automate the deployment setup for the telemetry compression POC.

## Scripts

### `bundle-and-push.sh` (runs on your Mac)

Packages all sidecar artifacts into a tarball, pushes it to a remote VM,
and triggers the build/setup process.

```bash
./bundle-and-push.sh <terraform-dir> <target-vm-hostname>
```

**Arguments:**
- `terraform-dir` — Path to the terraform directory containing `ssh.config` and `id_rsa`
- `target-vm-hostname` — Hostname of the VM as defined in `ssh.config` (e.g., `amcx0`)

**What it does:**
1. Bundles Python source, C extension source, trained models, playbooks, and `setup-on-target.sh` into a tarball
2. SCPs the tarball to the target VM (using `ssh.config` for SSH key, port, and user)
3. SSHes in and runs `setup-on-target.sh`

**Example:**
```bash
./bundle-and-push.sh \
  ~/work/sst-template-migration/head/SB_load_192_dest/run/194/terraform \
  amcx0
```

### `setup-on-target.sh` (runs on the VM automatically)

Called by `bundle-and-push.sh` — you don't normally run this directly.

**What it does:**
1. Installs build prerequisites (`gcc-toolset-13`, `python3-devel`, `make`, `git`)
2. Builds `zli` from OpenZL source (clones from GitHub, pinned commit)
3. Builds `_telemetry_scanner.so` C extension
4. Stages all artifacts to `/tmp/telemetry-compress-stage/`
5. Stages Ansible playbooks to `/tmp/playbooks/`
6. Prints the exact `ansible-playbook` commands to run next

## After the scripts finish

SSH into the target VM and verify the build succeeded:

```bash
# Verify zli was built correctly
file /tmp/telemetry-compress-stage/zli
# Expected: ELF 64-bit LSB executable, x86-64

# Verify C extension was built
ls -la /tmp/telemetry-compress-stage/_telemetry_scanner.so
# Expected: file exists, non-zero size

# Verify all Python files are staged
ls /tmp/telemetry-compress-stage/*.py
# Expected: telemetry_sidecar.py telemetry_service.py telemetry_codec.py zljsonl.py

# Verify trained model is staged
ls /tmp/telemetry-compress-stage/models/lossless_telemetry/
# Expected: telemetry_csv.zl_compressor  telemetry_schema.json

# Verify playbooks are staged
ls /tmp/playbooks/*.yaml
# Expected: all 6 playbooks listed

# Quick round-trip test (optional — proves compression pipeline works on this machine)
cd /tmp/telemetry-compress-stage
echo '{"current-time":1234567890.123,"type":"cpu","creator":"test","node-id":"n1","content":{"usage":42}}' > /tmp/test_input.jsonl
python3 telemetry_service.py compress /tmp/test_input.jsonl /tmp/test_output.zljsonl
python3 telemetry_service.py decompress /tmp/test_output.zljsonl /tmp/test_roundtrip.jsonl
diff /tmp/test_input.jsonl /tmp/test_roundtrip.jsonl
# Expected: no output (files are identical)
```

Then become root and proceed with the playbooks:

```bash
sudo su
cd /tmp/playbooks
```

## A/B Comparison Setup

To measure compression impact, we use two dedicated Kafka topics:
- `nom-telemetry-poc-compressed` — receives compressed data from the sidecar
- `nom-telemetry-poc-raw` — receives uncompressed data directly from Telegraf

This isolates the comparison from other services writing to `nom-telemetry`.

### Step 0: Create POC Kafka topics (one per environment)

Each environment has its own Kafka cluster. Run the playbook once per environment,
targeting that environment's Kafka broker.

**`ems_broker_addr` must be the broker's PRIVATE IP** (from `sps_private_hosts`
or `inventory.yaml` in the terraform directory), not the hostname.

**SSL is off by default.** Zookeeper address defaults to `<broker_private_ip>:2182`
(derived from `ems_broker_addr`). Override with `-e ems_zookeeper_addr=...` if the
zookeeper is on a different host. Add `-e ems_ssl=true` for SSL-enabled environments
(uses `admin-client.properties` instead, no zookeeper needed).

```bash
# Environment A — create the compressed topic (SSL off)
ansible-playbook create-poc-kafka-topics.yaml \
  -i "<KAFKA_BROKER_ENV_A>," \
  -e ems_hosts="<KAFKA_BROKER_ENV_A>" \
  -e ems_broker_addr="<BROKER_PRIVATE_IP>:9093" \
  -e ems_topic_name="nom-telemetry-poc-compressed" \
  -e ansible_user=centos --become

# Environment B — create the raw topic (SSL off)
ansible-playbook create-poc-kafka-topics.yaml \
  -i "<KAFKA_BROKER_ENV_B>," \
  -e ems_hosts="<KAFKA_BROKER_ENV_B>" \
  -e ems_broker_addr="<BROKER_PRIVATE_IP>:9093" \
  -e ems_topic_name="nom-telemetry-poc-raw" \
  -e ansible_user=centos --become
```

**Env A (194) — concrete example:**
```bash
ansible-playbook create-poc-kafka-topics.yaml \
  -i "dc1-kafka0," \
  -e ems_hosts="dc1-kafka0" \
  -e ems_broker_addr="10.0.0.X:9093" \
  -e ems_topic_name="nom-telemetry-poc-compressed" \
  -e ansible_user=centos --become
```

No per-topic ACLs are needed — both environments use cluster-level ACLs, so the
existing `kafka-general-producer` certificate can write to any topic.

**Verify (SSH to each Kafka broker):**
```bash
# Check the topic definition JSON was placed
cat /usr/local/nom/etc/kafka-topics/topics/poc-nom-telemetry-poc-compressed.json
# Expected: JSON with "topic": "nom-telemetry-poc-compressed"

# List topics and confirm ours appears
export NOM_KAFKA_ZOOKEEPER_OVERRIDE="<BROKER_PRIVATE_IP>:2182"
/usr/local/nom/sbin/kafka-topics --bootstrap-server <BROKER_PRIVATE_IP>:9093 --list | grep nom-telemetry-poc
# Expected: nom-telemetry-poc-compressed (or nom-telemetry-poc-raw)

# Check topic details
/usr/local/nom/sbin/kafka-topics --bootstrap-server <BROKER_PRIVATE_IP>:9093 --describe --topic nom-telemetry-poc-compressed
# Expected: shows partition count, replication factor, leader
```

### Step 1: Deploy decompressor to Data Loader VM (MUST BE FIRST)

```bash
ansible-playbook deploy-telemetry-decompressor.yaml \
  -i "<DATA_LOADER_VM>," \
  -e ems_hosts="<DATA_LOADER_VM>" \
  -e ems_sidecar_src_dir="/tmp/telemetry-compress-stage" \
  -e ansible_user=centos --become
```

**Env A (194) — concrete example:**
```bash
ansible-playbook deploy-telemetry-decompressor.yaml \
  -i "dc1-influx0," \
  -e ems_hosts="dc1-influx0" \
  -e ems_sidecar_src_dir="/tmp/telemetry-compress-stage" \
  -e ansible_user=centos --become
```

**Verify (SSH to Data Loader VM):**
```bash
# Check files were deployed
ls -la /usr/local/nom/lib/telemetry-compress/
# Expected: telemetry_service.py, telemetry_codec.py, zljsonl.py,
#           _telemetry_scanner.so, zli, decompress.sh

# Check zli binary works
/usr/local/nom/lib/telemetry-compress/zli --version
# Expected: zstrong-cli version 0.1

# Check Python can import the compression pipeline
python3 -c "import sys; sys.path.insert(0, '/usr/local/nom/lib/telemetry-compress'); import telemetry_service; print('OK')"
# Expected: OK

# Verify Data Loader is still running and processing uncompressed telemetry
systemctl status nom-data-loader
# Expected: active (running)
```

### Step 2a: Environment A — Deploy sidecar (compressed)

Deploy the sidecar to a VM. It produces compressed batches to the POC topic.

The sidecar always logs zstd compression ratio alongside OpenZL (`--compare-zstd`).
To also capture raw uncompressed JSONL for offline analysis, add
`-e capture_dir=/tmp/telemetry-capture` (files roll at `capture_max_mb`, default 1 MB).

```bash
ansible-playbook deploy-telemetry-sidecar.yaml \
  -i "<ENV_A_VM>," \
  -e ems_hosts="<ENV_A_VM>" \
  -e ems_sidecar_src_dir="/tmp/telemetry-compress-stage" \
  -e kafka_brokers="broker1:9093,broker2:9093" \
  -e kafka_topic="nom-telemetry-poc-compressed" \
  -e ansible_user=centos --become

# Optional: enable raw JSONL capture for analysis
#   -e capture_dir="/tmp/telemetry-capture"
#   -e capture_max_mb="1"
```

**Env A (194) — concrete example:**
```bash
ansible-playbook deploy-telemetry-sidecar.yaml \
  -i "dc1-nsm0," \
  -e ems_hosts="dc1-nsm0" \
  -e ems_sidecar_src_dir="/tmp/telemetry-compress-stage" \
  -e kafka_brokers="dc1-kafka0:9093,dc1-kafka1:9093,dc1-kafka2:9093" \
  -e kafka_topic="nom-telemetry-poc-compressed" \
  -e ansible_user=centos --become
```

**Verify (SSH to the sidecar VM):**
```bash
# Check systemd service is running
systemctl status nom-telemetry-sidecar
# Expected: active (running)

# Check Unix socket exists
ls -la /var/run/nom-telemetry-sidecar/telemetry.sock
# Expected: socket file exists (type 's')

# Check sidecar logs for errors
journalctl -u nom-telemetry-sidecar --no-pager -n 20
# Expected: "Sidecar started" message, no errors

# Check deployed files
ls /usr/local/nom/lib/telemetry-compress/telemetry_sidecar.py
# Expected: file exists
```

### Step 3a: Environment A — Switch Telegraf to sidecar

> **IMPORTANT: Exclude Kafka broker nodes.** Do NOT redirect Kafka brokers'
> Telegraf to the sidecar/POC topic. Kafka brokers run `topic-diskusage.sh`
> which produces `topic_disk_metrics` measurements. These metrics flow through
> Telegraf → Kafka `nom_kafka` output → Data Loader → InfluxDB → Grafana.
> If you redirect the Kafka brokers, their monitoring metrics stop reaching
> InfluxDB and Grafana shows stale data. Target only non-Kafka nodes.
>
> After deploying, restore Kafka brokers to `nom-telemetry`:
> ```
> ansible-playbook configure-telegraf-custom-topic.yaml \
>   -i "dc1-kafka0,dc1-kafka1,dc1-kafka2," \
>   -e ems_hosts="all" -e ems_topic="nom-telemetry" \
>   -e ansible_user=centos --become
> ```

```bash
ansible-playbook configure-telegraf-sidecar.yaml \
  -i "<ENV_A_VM>," \
  -e ems_hosts="<ENV_A_VM>" \
  -e ansible_user=centos --become
```

**Env A (194) — concrete example:**
```bash
ansible-playbook configure-telegraf-sidecar.yaml \
  -i "dc1-nsm0," \
  -e ems_hosts="dc1-nsm0" \
  -e ansible_user=centos --become
```

**Verify (SSH to the sidecar VM):**
```bash
# Check Telegraf config switched to socket writer
grep -r socket_writer /var/nom/nom-telegraf/telegraf.d/
# Expected: nom_socket_telemetry_output.conf with unix:///var/run/nom-telemetry-sidecar/telemetry.sock

# Check original Kafka output is disabled
ls /var/nom/nom-telegraf/telegraf.d/nom_kafka_telemetry_output.conf 2>/dev/null
# Expected: file NOT found (should be .disabled)
ls /var/nom/nom-telegraf/telegraf.d/nom_kafka_telemetry_output.conf.disabled
# Expected: file exists

# Check Telegraf is running
systemctl status nom-telegraf
# Expected: active (running)

# Watch sidecar logs — should see batches being compressed
journalctl -u nom-telemetry-sidecar -f
# Expected: "Batch N: X KB -> Y KB (Zx) -> Kafka" messages every ~60s

# Verify messages are arriving in the compressed topic (on Kafka broker)
export NOM_KAFKA_ZOOKEEPER_OVERRIDE="<BROKER_PRIVATE_IP>:2182"
/usr/local/nom/sbin/kafka-topics --bootstrap-server <BROKER_PRIVATE_IP>:9093 \
  --describe --topic nom-telemetry-poc-compressed
# Expected: partition count > 0, check that offsets are increasing

# Verify compressed messages have ZLJSONL magic bytes (on Kafka broker)
nom-kafka-dump --brokers <BROKER_PRIVATE_IP>:9093 nom-telemetry-poc-compressed --offset end --max-messages 1 | xxd | head -1
# Expected: first 8 bytes are 5a4c 4a53 4f4e 4c00 (ZLJSONL\0)
```

### Step 2b: Environment B — Redirect Telegraf to raw topic (no sidecar)

On a different VM, redirect Telegraf's Kafka output to the raw POC topic:

> **IMPORTANT: Exclude Kafka broker nodes.** Same issue as Step 3a — Kafka
> brokers must keep sending to `nom-telemetry` so their monitoring metrics
> (topic disk usage, cluster health) continue reaching InfluxDB/Grafana.
> Target only non-Kafka nodes. If you already redirected Kafka brokers,
> restore them:
> ```
> ansible-playbook configure-telegraf-custom-topic.yaml \
>   -i "dc1-kafka0,dc1-kafka1,dc1-kafka2," \
>   -e ems_hosts="all" -e ems_topic="nom-telemetry" \
>   -e ansible_user=centos --become
> ```

```bash
ansible-playbook configure-telegraf-custom-topic.yaml \
  -i "<ENV_B_VM>," \
  -e ems_hosts="<ENV_B_VM>" \
  -e ems_topic="nom-telemetry-poc-raw" \
  -e ansible_user=centos --become
```

**Verify (SSH to the Environment B VM):**
```bash
# Check Telegraf config has the new topic
grep topic /var/nom/nom-telegraf/telegraf.d/nom_kafka_telemetry_output.conf
# Expected: topic = "nom-telemetry-poc-raw"

# Check Telegraf is running
systemctl status nom-telegraf
# Expected: active (running)

# Verify messages are arriving in the raw topic (on Kafka broker)
export NOM_KAFKA_ZOOKEEPER_OVERRIDE="<BROKER_PRIVATE_IP>:2182"
/usr/local/nom/sbin/kafka-topics --bootstrap-server <BROKER_PRIVATE_IP>:9093 \
  --describe --topic nom-telemetry-poc-raw
# Expected: partition count > 0, check that offsets are increasing
```

### Comparing results

After both environments run for a period, compare topic sizes on the broker:

```bash
kafka-log-dirs --describe --bootstrap-server <broker>:9093 \
  --command-config /tmp/kafka-admin-ssl.properties \
  --topic-list nom-telemetry-poc-compressed,nom-telemetry-poc-raw
```

**Env A (194) — concrete example:**
```bash
kafka-log-dirs --describe --bootstrap-server dc1-kafka0:9093 \
  --command-config /tmp/kafka-admin-ssl.properties \
  --topic-list nom-telemetry-poc-compressed,nom-telemetry-poc-raw
```

### Rollback

```bash
# Environment A: restore Telegraf Kafka output, stop sidecar
ansible-playbook rollback-telegraf-sidecar.yaml \
  -i "<ENV_A_VM>," \
  -e ems_hosts="<ENV_A_VM>" \
  -e ansible_user=centos --become

# Environment B: restore original topic
ansible-playbook configure-telegraf-custom-topic.yaml \
  -i "<ENV_B_VM>," \
  -e ems_hosts="<ENV_B_VM>" \
  -e ems_topic="nom-telemetry" \
  -e ansible_user=centos --become
```

**Env A (194) — concrete example:**
```bash
ansible-playbook rollback-telegraf-sidecar.yaml \
  -i "dc1-nsm0," \
  -e ems_hosts="dc1-nsm0" \
  -e ansible_user=centos --become
```

**Verify rollback (SSH to each VM):**
```bash
# Environment A VM — sidecar stopped, Telegraf back on Kafka
systemctl status nom-telemetry-sidecar
# Expected: inactive (dead) or not found

ls /var/nom/nom-telegraf/telegraf.d/nom_kafka_telemetry_output.conf
# Expected: file exists (restored from .disabled)

ls /var/nom/nom-telegraf/telegraf.d/nom_socket_telemetry_output.conf 2>/dev/null
# Expected: file NOT found (removed)

systemctl status nom-telegraf
# Expected: active (running)

# Environment B VM — topic restored to nom-telemetry
grep topic /var/nom/nom-telegraf/telegraf.d/nom_kafka_telemetry_output.conf
# Expected: topic = "nom-telemetry"
```

Both rollbacks are config-only — no code redeploy needed.

## Variable reference

| Variable | Used in | Description |
|----------|---------|-------------|
| `ems_hosts` | All playbooks | Target VM hostname or IP |
| `ems_sidecar_src_dir` | deploy-sidecar, deploy-decompressor | Path to staged files on the Ansible control node |
| `kafka_brokers` | deploy-sidecar | Comma-separated broker:port list |
| `kafka_topic` | deploy-sidecar | Kafka topic for sidecar output |
| `kafka_ssl` | deploy-sidecar | `true` for TLS-enabled Kafka (default: `false`) |
| `compare_zstd` | deploy-sidecar | Log zstd ratio alongside OpenZL (default: `true`) |
| `capture_dir` | deploy-sidecar | `true` to enable raw JSONL capture (default: `false`) |
| `capture_path` | deploy-sidecar | Directory for capture files (default: `/tmp/capture`) |
| `capture_max_mb` | deploy-sidecar | Max size per capture file in MB (default: `1`) |
| `ems_topic` | configure-telegraf-custom-topic | Kafka topic for Telegraf direct output |
| `ems_broker_addr` | create-poc-kafka-topics | Kafka broker **private IP** and port (e.g., `10.0.0.5:9093`) |
| `ems_zookeeper_addr` | create-poc-kafka-topics | Zookeeper address (default: `<broker_ip>:2182`, override if different host) |
| `ems_ssl` | create-poc-kafka-topics | `true` for SSL environments (default: `false`) |
| `ems_topic_name` | create-poc-kafka-topics | Topic name to create (required — no default) |

## Playbook inventory

| Playbook | Purpose |
|----------|---------|
| `create-poc-kafka-topics.yaml` | Create the two A/B comparison topics |
| `deploy-telemetry-decompressor.yaml` | Deploy decompression toolchain to Data Loader VMs |
| `deploy-telemetry-sidecar.yaml` | Deploy sidecar files + start service |
| `configure-telegraf-sidecar.yaml` | Switch Telegraf from Kafka to Unix socket (Env A) |
| `configure-telegraf-custom-topic.yaml` | Redirect Telegraf to a custom Kafka topic (Env B) |
| `rollback-telegraf-sidecar.yaml` | Restore Telegraf Kafka output, stop sidecar |

## Staged file layout

After `setup-on-target.sh` completes:

```
/tmp/telemetry-compress-stage/
├── telemetry_sidecar.py
├── telemetry_service.py
├── telemetry_codec.py
├── zljsonl.py
├── _telemetry_scanner.so    # built from C source
├── zli                       # built from OpenZL source
├── decompress.sh
└── models/lossless_telemetry/
    ├── telemetry_csv.zl_compressor
    └── telemetry_schema.json

/tmp/playbooks/
├── create-poc-kafka-topics.yaml
├── deploy-telemetry-sidecar.yaml
├── configure-telegraf-sidecar.yaml
├── configure-telegraf-custom-topic.yaml
├── deploy-telemetry-decompressor.yaml
├── rollback-telegraf-sidecar.yaml
└── templates/
    └── nom-telemetry-sidecar.service.j2
```
