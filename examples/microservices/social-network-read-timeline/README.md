# Social Network: Read Timeline

Closes issues #48.

VibeSys use:

```bash
vibesys --input examples/microservices/social-network-read-timeline
```

**Target:** DeathStarBench socialNetwork: Go microservices stack deployed via Docker Compose. 

**Workload:** Read-heavy, specifically for this issue. 50% user-timeline reads,
40% home-timeline reads, and 10% stateful compose + user- and follower-home-
timeline read-your-write sequences to keep content fresh.

**Primary metric:** `p50_ms` (combined read latency).

 **Acceptance criteria:** `success_rate` must stay at 1.0 and all 11 correctness checks (C1–C11) must pass.

---

## Correctness Checks (C1-C11)

The following 11 checks are used for this service.


| Check | Property                                                                           |
| ----- | ---------------------------------------------------------------------------------- |
| C1    | User-timeline response shape compatibility                                         |
| C2    | Home-timeline response shape compatibility                                         |
| C3    | User-timeline ordering (descending timestamp)                                      |
| C4    | Home-timeline ordering (descending timestamp)                                      |
| C5    | Visibility rules: home-timeline only shows followed users                          |
| C6    | Write-then-read consistency for user-timeline                                      |
| C7    | Write-then-read consistency for home-timeline fan-out                              |
| C8    | Unfollow stops future fan-out                                                      |
| C9    | Failed reads do not mutate application state                                       |
| C10   | Pagination: disjoint non-overlapping pages                                         |
| C11   | Held-out sequences: burst-read consistency + cross-user visibility + page boundary |


---

## How to run

Below are instructions to build and run this system.

Clone this repo with the submodule:

DeathStarBench is tracked as a git submodule at `3rd_party/deathstarbench`. It must be initialised on first clone:

```bash
git clone --recurse-submodules <your-fork-url>
cd social-network-read-timeline
```

If you already cloned without `--recurse-submodules`:

```bash
git submodule update --init --recursive
```

To verify if the submodule is properly populated, run the following command: 

```bash
ls 3rd_party/deathstarbench/socialNetwork
```

You should see `docker-compose.yml`, `CMakeLists.txt`, `nginx-web-server/`, etc.

Then, to start DeathStarBench, run the following:

```bash
cd 3rd_party/deathstarbench/socialNetwork
docker compose up -d
```

Wait for about 30 seconds for all containers to start, then verify nginx is up using:

```bash
docker compose ps | grep nginx
```

You should see `socialnetwork-nginx-thrift-1` with status `Up` and port `0.0.0.0:8080->8080/tcp`.

Then to initialise the social graph (creates 962 users with follow relationships from the Reed98 dataset), use the following commands:

```bash
python3 scripts/init_social_graph.py --graph=socfb-Reed98 --ip=localhost --port=8080
```

This takes 1–2 minutes. To verify if the stack is responding appropriately, run as follows:

```bash
curl "http://localhost:8080/wrk2-api/user-timeline/read?user_id=1&start=0&stop=1"
```

Should return a JSON array.
Before the system is built with CMake, we need to install and make use of the nginx patches, both default and the new ones added. Our checker and benchmark require four additions to the running nginx container:

1. Three new `check-api` endpoints (for correctness checks via Thrift services)
2. Timing headers (`X-Compose-Thrift-Ms`, `X-UserTimeline-Thrift-Ms`, `X-HomeTimeline-Thrift-Ms`) for intermediate latency measurement

These are applied by a single script. From the repo root:

```bash
cd nginx_patches
chmod +x apply_patches.sh
./apply_patches.sh
```

Expected output ends with the following format:

```
Patches applied and nginx reloaded.
[] <- get_followees OK
```

**Important:** It should be noted that this step would have to be repeated every time the nginx container is recreated (i.e. after `docker compose down` + `docker compose up -d`).

To build the checker, run the following in the terminal:

```bash
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j4
```

CMake automatically downloads `nlohmann/json` (v3.11.3) via `FetchContent`
during the configure step. This may take 10–15 seconds the first time.

To run the checker:

```bash
cd build
./checker --base-url http://localhost:8080
```

All 11 checks must pass before running the benchmark. Expected output should look as follows:

```
  PASS  C1 User-timeline response shape compatibility
  PASS  C2 Home-timeline response shape compatibility
  PASS  C3 User-timeline ordering is descending by timestamp
  PASS  C4 Home-timeline ordering is descending by timestamp
  PASS  C5 Visibility rules: home-timeline only shows followed users posts
  PASS  C6 Write-then-read consistency: compose reflects in user-timeline
  PASS  C7 Write-then-read consistency: compose fans out to follower home-timeline
  PASS  C8 Unfollow stops future fan-out to home-timeline
  PASS  C9 Failed reads do not mutate application state
  PASS  C10 Pagination: start/stop bounds return disjoint non-overlapping pages
  PASS  C11 Held-out sequences: burst-read consistency and cross-user visibility

Results: 11 passed, 0 failed out of 11 checks.
```

Optional flags which can be included with the checker:

```bash
./checker --base-url http://localhost:8080 --timeout-ms 10000 --poll-ms 200
```

Run the shared evaluator from the repository root. The checked-in workload uses
the original medium profile: 300 target logical operations/s, 20 seconds of
warmup, 120 seconds of measurement, 50 users, and 10 seed posts per user. Each
VibeSys invocation supplies a random fixture seed, which produces
collision-resistant user IDs, usernames, and post markers on a long-lived
candidate deployment. The load seed stays fixed, so every candidate receives
the same operation and user selection sequence.

```bash
go -C examples/evaluators/microservice run ./cmd/servicebench \
  --workload "$PWD/examples/microservices/social-network-read-timeline/benchmark/workload.toml" \
  --base-url http://localhost:8080 \
  --output-json /tmp/social-network.json \
  --output-raw /tmp/social-network.ndjson
```

The legacy light, medium, and heavy configurations remain available as named
profiles. A profile changes both load and fixture size:

| Profile | Target operations/s | Duration | Warmup | Users | Seed posts per user |
| --- | ---: | ---: | ---: | ---: | ---: |
| `light` | 100 | 60s | 10s | 20 | 5 |
| `medium` | 300 | 120s | 20s | 50 | 10 |
| `heavy` | 600 | 180s | 30s | 100 | 10 |

Select one with `--profile light`, `--profile medium`, or `--profile heavy`.

This stateful adapter intentionally rejects `--skip-prepare`: once measured
writes have run, their exact content cannot be reconstructed safely in a new
evaluator process. Use a fresh random fixture seed for each evaluation instead.

### Output format

The benchmark emits a versioned JSON summary and optionally one raw NDJSON
record per logical operation. The summary includes individual trials, latency and queue
distributions, offered-load diagnostics, constraints, and the trusted scalar:

```json
{
  "schema_version": 1,
  "primary_value": 3.25,
  "primary_metric": {
    "name": "p50_ms",
    "metric": "latency_ms.p50",
    "direction": "minimize",
    "unit": "ms",
    "tags": ["read"]
  },
  "valid": true,
  "trials": [],
  "aggregate": {}
}
```

`primary_value` is the median trial-level p50 scheduled-arrival latency for
successful operations tagged `read`. Timeline reads must return the exact
newest expected content window with the DeathStarBench post schema, descending
timestamps, stable post/request identity, and creator identity. A compose
sequence counts once and succeeds
only if the acknowledged post marker is immediately visible in the author's
timeline and the follower's home timeline.
A run is invalid unless its success rate is 1.0, every operation type was
sampled, and the load generator sustains at least 95% of the target rate. Thrift
timing headers remain available in raw operation records as intermediate timings.

## Rebuilding

To teardown the system, run the folllowing:

```bash
cd 3rd_party/deathstarbench/socialNetwork
docker compose down -v
```

This will remove MongoDB and Redis volumes, starting clean for the next run.
