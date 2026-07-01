---
name: neuron-nki-profile-querying
description: |
  Query and analyze NKI kernel profile data from neuron-explorer parquet
  files. Supports SQL queries via the neuron-explorer API and Python on
  parquet for advanced analysis. Works locally on trainium with NEFF/NTFF
  files on disk.

  Querying: start neuron-explorer, ingest profiles, run SQL against
  tables (Summary, Instruction, DmaPacket, DmaPacketAggregated, etc.),
  explore schemas. Use when user says "query profile", "run SQL on profile",
  "start neuron-explorer", or has NEFF+NTFF files and wants to query them.

  Analysis: compute performance bounds, identify bottleneck engines,
  measure gaps (idle time, inefficiency, excess traffic, transposes),
  and run investigations to localize inefficiencies to NKI source lines.
  Use when user says "analyze profile", "what's the bottleneck",
  "compute bounds", "why is my kernel slow", or wants profile-guided
  optimization guidance.
argument-hint: "[neff-path] [ntff-path]"
---

# Profile Querying

Run SQL queries against NKI kernel profile data using `neuron-explorer view`.
This ingests NEFF+NTFF into parquet and exposes a DuckDB-backed API server
on localhost. No deployment, no remote service — just the CLI and curl.

For more advanced analysis, use python on parquet to compute performance
bounds and investigate precise inefficiencies within arbitrary execution
intervals. 

**What you need:** A compiled NEFF file and a captured NTFF trace file.
These come from `/neuron-nki-profiling` or from running a kernel with the right
env vars and `neuron-explorer capture`.

## Quick Start

```bash
# Ingest and start API server (no web UI)
neuron-explorer view \
  -n ./kernel.neff \
  -s ./profile.ntff \
  --data-path ~/.local/share/neuron-profile \
  --display-name my-kernel \
  --disable-ui &

# Wait for server
sleep 10

# Query
curl -s -X POST http://localhost:3002/api/v1/db/my-kernel/_search \
  -H 'Content-Type: application/json' \
  -d '{"type":"databaseExplorerQuery","tableName":"Summary","query":"SELECT total_time, mfu_estimated_percent, tensor_engine_active_time_percent, dma_active_time_percent FROM Summary"}'
```

That's it. Ingest, serve, query.

## Prerequisites

- `neuron-explorer` installed (comes with AL2023 DLAMI or `aws-neuronx-tools`)
- NEFF file (compiled kernel binary) + NTFF file (execution trace)

Check availability:
```bash
which neuron-explorer && neuron-explorer --version
```
If not found, check `/opt/aws/neuron/bin/neuron-explorer`.

---

## Step-by-Step Workflow

### Step 0: Check Profile Quality (Re-profile if Needed)

> **Note:** This step is specific to NKI kernel development. If you are querying
> a profile that was generated outside of an NKI workflow, skip to Step 1.

> **Disclaimer:** Query results are only as good as the profile. If the NEFF/NTFF
> were captured without the right env vars, key tables (DmaPacket,
> DmaPacketAggregated) may be empty and source-level attribution will be missing.

Check whether the profile has the data you need:
```bash
# After ingesting (Step 2), check for DMA packet data
curl -s -X POST http://localhost:3002/api/v1/db/${PROFILE_NAME}/_search \
  -H 'Content-Type: application/json' \
  -d '{"type":"databaseExplorerQuery","tableName":"DmaPacket","query":"SELECT COUNT(*) as cnt FROM DmaPacket"}'
```

If `cnt` is 0 or the table is missing, the profile was captured **without DGE
notifications**. If `bir_debug_info_source_location` is NULL on all Instruction
rows, the NEFF was compiled **without debug info**.

**To re-profile for best results**, set these env vars in the kernel script
before any neuron imports, then re-run and re-capture:

```python
import os
os.environ["XLA_IR_DEBUG"] = "1"
os.environ["XLA_HLO_DEBUG"] = "1"
os.environ["NEURON_FRAMEWORK_DEBUG"] = "1"
os.environ["NEURON_RT_VISIBLE_CORES"] = ... # Restrict available cores when running experiments in parallel.
os.environ["NEURON_RT_INSPECT_ENABLE"] = "1"
os.environ["NEURON_RT_INSPECT_DEVICE_PROFILE"] = "1"
os.environ["NEURON_RT_INSPECT_SYSTEM_PROFILE"] = "0"
os.environ["NEURON_RT_INSPECT_OUTPUT_DIR"] = ... # This is for the NEFF generation if needed. NTFF will go to the -s capture path in the next command. 
```

Then re-capture with DGE notifications enabled:
```bash
NEFF_PATH=$(find ./output -name "*.neff" | head -1)
NEURON_RT_ENABLE_DGE_NOTIFICATIONS=1 neuron-explorer capture \
  -n "$NEFF_PATH" \
  -s profile.ntff \
  --profile-nth-exec=2
```

With `--profile-nth-exec=2`, the output file is `profile_exec_2.ntff` (not
`profile.ntff`), written to the directory specified by the `-s` flag.

| Env Var | What it enables |
|---------|----------------|
| `XLA_IR_DEBUG` / `XLA_HLO_DEBUG` | HLO-level debug info in NEFF |
| `NEURON_FRAMEWORK_DEBUG` | Framework-level source attribution |
| `NEURON_RT_ENABLE_DGE_NOTIFICATIONS` | DMA packet tables (DmaPacket, DmaPacketAggregated) |
| `NEURON_RT_INSPECT_DEVICE_PROFILE` | Device-level profiling in NEFF output |

If the existing profile has the data you need, skip this step entirely.

Another thing to look out for is running torch functions on device like randomnly generating
inputs. This will be fused into the kernel execution and obfuscate it's profile. Move those 
commands off device if you want to isolate kernel execution. 

### Step 1: Ingest and Start Server 

If you want to run SQL queries against the Neuron Explorer DuckDB engine, use the view
command with --disable-ui to start the server. 

Set variables:
```bash
NEFF_PATH=<resolved neff path>
NTFF_PATH=<resolved ntff path>
PROFILE_NAME=<descriptive name, e.g. "my-matmul">
NE_DATA_PATH=~/.local/share/neuron-profile
```

Check if the neuron-explorer server is already running: 
```bash
curl -s http://localhost:3002/api/v1/health
```
If the server is already running or if you are running python directly 
on the parquet, use --ingest-only in the following command instead of --disable-ui. 

```bash
neuron-explorer view \
  -n "$NEFF_PATH" \
  -s "$NTFF_PATH" \
  --data-path "$NE_DATA_PATH" \
  --display-name "$PROFILE_NAME" \
  --disable-ui \
  > /tmp/neuron-explorer-${PROFILE_NAME}.log 2>&1 &
NE_PID=$!
echo "neuron-explorer started (PID: $NE_PID), waiting for API..."
```
The command may fail on an conflicting port from the existing server but the ingestion 
may have still succeeded. If so, check for `Processing for ... is complete` before the error
message or rerun with --ingest-only.

Wait for API:
```bash
for i in $(seq 1 60); do
  if curl -s http://localhost:3002/api/v1/health 2>/dev/null | grep -q healthy; then
    echo "API server ready"
    break
  fi
  sleep 1
done
```

### Step 2: Read the Schema

Before writing any queries, check the table docs in `references/schema/` for
interpretive guidance on the most commonly used tables. For any table not
covered there, query its schema directly from neuron-explorer using the following commands:

```bash
curl -s -X POST http://localhost:3002/api/v1/db/${PROFILE_NAME}/_search \
  -H 'Content-Type: application/json' \
  -d '{"type":"tableSchema","tableName":"Instruction"}' | python3 -m json.tool
```

List all available tables:
```bash
curl -s -X POST http://localhost:3002/api/v1/db/${PROFILE_NAME}/_search \
  -H 'Content-Type: application/json' \
  -d '{"type": "listDbFiles"}' | python3 -m json.tool
```

### Step 3a: Execute SQL Queries 

Use `databaseExplorerQuery` for arbitrary SQL (SELECT only).

**Summary metrics — which engine is the bottleneck?**
```bash
curl -s -X POST http://localhost:3002/api/v1/db/${PROFILE_NAME}/_search \
  -H 'Content-Type: application/json' \
  -d '{"type":"databaseExplorerQuery","tableName":"Summary","query":"SELECT total_time, mfu_estimated_percent, tensor_engine_active_time_percent, vector_engine_active_time_percent, dma_active_time_percent, hbm_read_bytes, hbm_write_bytes FROM Summary"}' | python3 -m json.tool
```

**Instruction breakdown — what is each engine doing and waiting on?**
```bash
curl -s -X POST http://localhost:3002/api/v1/db/${PROFILE_NAME}/_search \
  -H 'Content-Type: application/json' \
  -d '{"type":"databaseExplorerQuery","tableName":"Instruction","query":"SELECT engine, opcode, COUNT(*) as cnt, SUM(duration_ns) as total_dur_ns, SUM(evt_wait_time_ns) as total_evt_wait_ns FROM Instruction GROUP BY engine, opcode ORDER BY total_dur_ns DESC"}' | python3 -m json.tool
```

**NKI source line hotspots — which lines of the kernel are slowest?**
```bash
curl -s -X POST http://localhost:3002/api/v1/db/${PROFILE_NAME}/_search \
  -H 'Content-Type: application/json' \
  -d '{"type":"databaseExplorerQuery","tableName":"Instruction","query":"SELECT bir_debug_info_source_location, engine, opcode, COUNT(*) as cnt, SUM(duration_ns) as total_dur_ns FROM Instruction WHERE bir_debug_info_source_location IS NOT NULL GROUP BY bir_debug_info_source_location, engine, opcode ORDER BY total_dur_ns DESC LIMIT 10"}' | python3 -m json.tool
```

### Step 3b: Python DuckDB on Parquet

For SQL queries without the API server, use DuckDB's Python bindings
directly on the parquet files. After ingestion (Step 1), the data lives at:

```
<data-path>/profiles/global/<display-name>@latest/<Table>.parquet
```

```python
import duckdb

NE_DATA_PATH = "~/.local/share/neuron-profile"
PROFILE = "my-kernel"
PARQUET_DIR = f"{NE_DATA_PATH}/profiles/global/{PROFILE}@latest"

con = duckdb.connect()

# Load tables directly from parquet
con.execute(f"CREATE VIEW Instruction AS SELECT * FROM '{PARQUET_DIR}/Instruction.parquet'")
con.execute(f"CREATE VIEW DmaPacket AS SELECT * FROM '{PARQUET_DIR}/DmaPacket.parquet'")

# Example: measure LDWEIGHTS/MATMUL temporal overlap
result = con.execute("""
    SELECT
        CASE WHEN lw.end_ts <= mm.start_ts THEN 'lw_before'
             WHEN lw.start_ts >= mm.end_ts THEN 'lw_after'
             ELSE 'overlap' END as rel,
        COUNT(*) as cnt
    FROM Instruction mm
    JOIN Instruction lw ON mm.bir_id = lw.bir_id
    WHERE mm.opcode = 'MATMUL' AND lw.opcode = 'LDWEIGHTS'
      AND mm.tensor_instruction_type = 'REGULAR'
      AND lw.tensor_instruction_type = 'REGULAR'
    GROUP BY rel
""").fetchdf()
print(result)
```

### Step 3c: Pandas on Parquet

For analyses that require Python computation — interval merges, custom
metrics, numpy operations, or cross-table joins with arbitrary logic —
load the parquet files directly with pandas.

```python
import pandas as pd, numpy as np, os

NE = os.path.expanduser("~/.local/share/neuron-profile/profiles/global")
profile = "my-kernel"
d = f"{NE}/{profile}@latest"

# Load tables
summary  = pd.read_parquet(f"{d}/Summary.parquet").iloc[0]
inst     = pd.read_parquet(f"{d}/Instruction.parquet")
active   = pd.read_parquet(f"{d}/ActiveTime.parquet")
metadata = pd.read_parquet(f"{d}/Metadata.parquet").iloc[0]
dma_pkts = pd.read_parquet(f"{d}/DmaPacket.parquet")
dma_agg  = pd.read_parquet(f"{d}/DmaPacketAggregated.parquet")
tensors  = pd.read_parquet(f"{d}/TensorInfo.parquet")
flow     = pd.read_parquet(f"{d}/Flow.parquet")
```

This is the approach used by the performance bounds computation and all
investigations in the Profile Analysis workflow.

### Step 4: Interpret Results

**Only claim what the data shows.** Profile data is precise but narrow —
it tells you what happened, not always why.

- **Don't diagnose from single metrics.** A query result is a measurement,
  not a conclusion. Low utilization, high wait times, or large byte counts
  need context from other tables before they mean anything.
- **Don't assume field names mean what they sound like.** Some fields are
  unpopulated or misleading for NKI kernels. Check `references/schema/`
  before building conclusions on a field you haven't validated.
- **Don't compare engines without interval merging.** Instructions overlap
  within an engine (pipelining) and across engines (parallelism). Raw sums
  from the Instruction table overstate wall-clock time. Use `ActiveTime`
  for wall-clock comparisons.
- **Don't skip the data quality check.** If `DmaPacketAggregated` is missing
  or `bir_debug_info_source_location` is mostly NULL, the query results are
  incomplete — re-profile before interpreting.


### Step 8: Cleanup 

```bash
kill $NE_PID 2>/dev/null
```

## Profile Analysis

If you are asked for analysis of the profile, follow this workflow.
All logic — bound definitions, gap interpretation, and investigation
selection — lives in [performance-bounds.md](references/performance-bounds.md).

### 1. Calculate bounds

Follow the **"The bounds"** section of performance-bounds.md to compute all
three families (memory, compute, pipeline). These require Python on parquet
(Step 3c). 

### 2. Identify the dominant gaps

Follow **"Reading the gaps"** in performance-bounds.md. Compute each
consecutive-pair gap within the memory and compute families, plus the
pipeline gap. Report all gaps and their sizes relative to `total_time`.

### 3. Run investigations

Follow **"From bounds to investigations"** in performance-bounds.md. Use
the bottleneck engine and gap sizes to select which investigation groups
to run. Each investigation has a Step 1 (detect and quantify) and Step 2
(localize to NKI source lines). Run all relevant investigations — a kernel
typically has multiple active inefficiencies.

### 4. Report

Present a single summary:

- **Bounds table**: all bounds with values and the gap between each pair. 
Also report each engine's total time pointing out the largest one(s) as 
the bottleneck(s). If neither DMA nor Tensor Engine is the bottleneck, 
explain which engine is the bottleneck and that supporting it is still WIP.  
- **Per-investigation findings**: gap size, source lines responsible, and
  their contributions. Include investigations that found nothing so the
  analysis is visibly complete.

Order the presented inefficiencies and investigation findings according
 to it's relevance to the bottlenecks and the measured gaps. 

### 4. Follow up (After an optimization step/attempt)

After an optimization step or attempt, investigate the new profile to 
identify exactly what improved or regressed. Follow the full process and
present a side by side report of all of the bounds and engine times as well 
as the new investigation findings. Highlight changes but do not over-interpret, 
only relay what the evidence shows. Static code analysis is faulty, you will be
tempted to over-intepret the causes and effects, DON'T (unless EXPLICITELY) asked 
to. 

### Worked Examples

For end-to-end examples of profile-guided optimization, see:

| Investigation | What it covers |
|--------------|----------------|
| [Optimizing-Matmul](references/example-bounds-analysis.md) | End-to-end bounds analysis of a 4096x4096 bf16 matmul across three versions: V0 (naive tiling, DMA-bound), V1 (free-dimension blocking, reduces reloads, flips bottleneck to TE), V2 (row loads, near-peak TE utilization). Shows bounds tables, gap analysis, and investigation results at each step. |
---

## Multi-Kernel Querying

All profiles sharing the same `--data-path` are served by one server. Each
profile is queried by its `--display-name`.

```bash
NE_DATA_PATH=~/.local/share/neuron-profile

neuron-explorer view -n $NEFF_A -s $NTFF_A --data-path "$NE_DATA_PATH" --display-name kernel-a --disable-ui &
neuron-explorer view -n $NEFF_B -s $NTFF_B --data-path "$NE_DATA_PATH" --display-name kernel-b --disable-ui &

# Query either through same server
curl localhost:3002/api/v1/db/kernel-a/_search ...
curl localhost:3002/api/v1/db/kernel-b/_search ...
```

For batch ingestion without a server, use `--ingest-only` instead of
`--disable-ui`. It writes parquet and exits. Any future server on the same
data-path discovers the ingested profiles.

Parquet lands at `<data-path>/profiles/global/<display-name>@latest/`.

---

## Port Conflicts

If port 3002 is already in use, ingestion still succeeds — parquet is written
to disk before the server attempts to bind.

```bash
lsof -i :3002 | head -5
```

- If it's neuron-explorer on the same data-path: reuse it — it discovers
  newly ingested profiles automatically.
- If it's something else: use `--api-server-port 4002` (or any free port).

## Important Notes

- **Use `neuron-explorer` not `neuron-profile`** for all capture and view commands.
- **DGE notifications are required** for DMA packet-level tables (DmaPacket,
  DmaPacketAggregated). Set `NEURON_RT_ENABLE_DGE_NOTIFICATIONS=1` in the
  environment before `neuron-explorer capture`. Do NOT rely on the CLI flag —
  use the env var directly.
- Always pass `--data-path` explicitly.
- The API server binds to localhost only.
- Only SELECT queries are supported via the API.
- `--display-name` becomes the profile identifier in API URLs.
- `--disable-ui` skips the web UI (port 3001) but starts the API server (port 3002).
- `--ingest-only` writes parquet and exits — no server at all.

## Related Skills

| Skill | Purpose |
|-------|---------|
| `/neuron-nki-profiling` | Capture NEFF/NTFF on hardware |
| `/neuron-nki-writing` | Write NKI kernels |
| `/neuron-nki-debugging` | Debug compilation errors |
| `/neuron-nki-docs` | Look up API documentation |