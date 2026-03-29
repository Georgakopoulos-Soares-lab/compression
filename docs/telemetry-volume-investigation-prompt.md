# Telemetry Volume Investigation — Agent Instructions

## Mission

We built a compression sidecar for telegraf JSONL telemetry flowing through Kafka. The problem: each VM only generates ~123 KB of telegraf data per 60 seconds — too small for our OpenZL compression pipeline to outperform generic zstd (2.9x vs 12.7x). Before concluding that OpenZL isn't viable for this data, we need to understand the **full picture of telemetry volume** across all services.

Telegraf is only one source of telemetry. Each microservice in the product also emits its own telemetry (health metrics, operational stats, performance counters) to the same Kafka topic. The key question: **how much telemetry data does each service emit, and is the combined volume large enough for OpenZL to show real benefits?**

If the per-service telemetry is significantly more frequent or voluminous than telegraf's system metrics, then the total data flowing through `nom-telemetry` per VM per minute might be much larger than the 123 KB we're seeing — and our compression pipeline might work well on the aggregated stream.

## What You Need To Understand

1. **What services exist in a deployment** — cache-serve, nom-link, Data Loader, Apps Portal, SSM, etc.
2. **How each service emits telemetry** — do they push to Kafka directly, or does Telegraf scrape them?
3. **What the telemetry format looks like per service** — field count, nesting depth, record size
4. **How frequently each service emits** — every 5s? 10s? 30s? On-event?
5. **What is the per-service volume** — bytes per minute per service instance
6. **What is the total volume per VM** — sum of all telemetry sources on a single VM over 60 seconds

The answer to these questions determines whether our compression approach is viable or whether we need a fundamentally different strategy.

## Files To Read

### 1. Project context and architecture
```
~/work/nom-config-hub/head/CLAUDE.md
```
Explains the product, all microservices, the telemetry pipeline, how Telegraf works, what data flows through `nom-telemetry`. Read this first.

### 2. Telegraf deep dive
```
~/work/nom-config-hub/head/docs/nom-telegraf-deep-dive.md
```
Detailed documentation on how Telegraf is configured, what inputs it collects, what outputs it sends to, and the JSONL format.

### 3. Deployment details
```
~/personal/compression/DEPLOY.md
```
Explains exactly what we deployed for the A/B compression test — the sidecar, the topics, the environments. Gives you context on the current state of the VMs.

### 4. Compression codebase
```
~/personal/compression/
```
Read `telemetry_service.py`, `telemetry_codec.py`, `models/lossless_telemetry/telemetry_schema.json` (the 35 metric types). The schema tells you what telegraf metric types exist — but there may be additional types emitted by services that aren't in this schema.

### 5. Service source code in Config Hub
Explore the service definitions in Config Hub to understand how each service emits telemetry:
```
~/work/nom-config-hub/head/src/main/java/com/nominum/confighub/services/sps/
```
Key files:
- `TelegrafService.java` — how Telegraf is deployed and configured
- `NomDataLoaderService.java` — Data Loader telemetry config
- `NomServiceMonitorService.java` — service monitor (consumes telemetry)
- Any other `*Service.java` files — each may configure its own telemetry emission

### 6. Telegraf configuration templates
Look for the actual Telegraf config files and input plugin configurations:
```
~/work/nom-config-hub/head/ansible/playbooks/
```
Search for telegraf-related configs, input plugins, and any service-specific telemetry collection.

### 7. Live environment access
SSH into the VMs to observe actual telemetry flow. The terraform directory for environment 194:
```
/Users/prousogl/work/sst-template-migration/head/SB_load_192_dest/run/194/terraform
```
Use `ssh.config` from this directory (user: centos, `StrictHostKeyChecking=no`).

Services run on different nodes:
- `dc1-nsm0` — nom-service-monitor
- `dc1-link0/1` — nom-link
- `dc1-cas0/1` — cache-serve
- `dc1-apps0` — Apps Portal
- `dc1-ssm0/1` — SSM
- `dc1-influx0` — Data Loader (InfluxDB)
- `dc1-kafka0/1/2` — Kafka brokers
- `dc1-vertica0-8` — Vertica nodes

## Investigation Steps

### Step 1: Understand the telemetry sources

Read the Config Hub codebase to identify every service that emits telemetry. For each service, determine:
- Does it emit via Telegraf (Telegraf scrapes the service)?
- Does it emit directly to Kafka?
- What is the emission interval?
- What does the payload look like?

### Step 2: Measure actual volume per service

SSH into representative VMs and measure the real data. Approaches:

**Option A: Capture from the sidecar.** We already have raw JSONL capture files at `/tmp/capture/` on each node. Analyze these to see what `type` and `creator` fields appear and how much data each contributes:
```bash
# On a VM with capture files:
cat /tmp/capture/capture_0001.jsonl | python3 -c "
import sys, json, collections
types = collections.Counter()
creators = collections.Counter()
sizes = collections.defaultdict(int)
for line in sys.stdin:
    try:
        obj = json.loads(line)
        t = obj.get('type', 'unknown')
        c = obj.get('creator', 'unknown')
        types[t] += 1
        creators[c] += 1
        sizes[c + '/' + t] += len(line)
    except: pass
print('=== By creator ===')
for c, n in creators.most_common(): print(f'  {c}: {n} records')
print('=== By type ===')
for t, n in types.most_common(): print(f'  {t}: {n} records')
print('=== By creator/type (bytes) ===')
for k, s in sorted(sizes.items(), key=lambda x: -x[1])[:20]:
    print(f'  {k}: {s:,} bytes')
"
```

**Option B: Monitor the Kafka topic directly.** Consume from `nom-telemetry` for 60 seconds and analyze:
```bash
# On a Kafka broker:
timeout 60 /usr/local/nom/sbin/kafka-console-consumer \
  --bootstrap-server 10.0.0.8:9093 --topic nom-telemetry \
  --timeout-ms 60000 > /tmp/telemetry-sample-60s.jsonl
wc -l /tmp/telemetry-sample-60s.jsonl
du -h /tmp/telemetry-sample-60s.jsonl
```

**Option C: Check service-specific telemetry endpoints.** Some services expose stats via HTTP or Unix socket. Check service documentation.

### Step 3: Build a volume map

Create a table:

| Service | Creator | Types Emitted | Records/min | Bytes/min | Notes |
|---------|---------|---------------|-------------|-----------|-------|

### Step 4: Assess compression viability

With the volume map:
- What is the total bytes/min across ALL services on a single VM?
- What is the total bytes/min across ALL VMs in a DC?
- If we aggregate across multiple VMs or longer time windows, at what point does the volume become large enough for OpenZL to outperform zstd?
- Are there specific high-volume services where compression would have the most impact?

### Step 5: Recommendations

Based on your findings, recommend:
1. Should we compress per-VM or aggregate across VMs?
2. Should we increase the batch window (5 min? 15 min?) to accumulate more data?
3. Are there specific services whose telemetry should be prioritized for compression?
4. Is the current approach (compress everything from one VM) the right granularity, or should we rethink the architecture?

## Workflow Rules

### 1. Plan Node Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately — don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One tack per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update tasks/lessons.md with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes — don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
When given a bug report: just fix it. Don't ask for hand-holding.
- Point at logs, errors, failing tests — then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

## Task Management
- **Plan First**: Write plan to tasks/todo.md with checkable items
- **Verify Plan**: Check in before starting implementation
- **Track Progress**: Mark items complete as you go
- **Explain Changes**: High-level summary at each step
- **Document Results**: Add review section to tasks/todo.md
- **Capture Lessons**: Update tasks/lessons.md after corrections

## Core Principles
- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.
- **Don't Assume, Ask**: If you are unsure about something, ask the user. Never assume.

## Deliverable

A detailed report answering:
1. How much telemetry data does each service emit per minute?
2. What is the total volume per VM and per DC?
3. Where is the volume concentrated (which services, which types)?
4. Is the volume sufficient for OpenZL to be viable, and under what batching strategy?
5. Concrete recommendation for next steps
