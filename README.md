# Social Network: Read Timeline

Closes issues #48.

VibeServe use:

- `--ref examples/social-network-read-timeline/reference`
- `--acc-checker examples/social-network-read-timeline/accuracy_checker`
- `--bench examples/social-network-read-timeline/benchmark`

**Target:** DeathStarBench socialNetwork: Go microservices stack deployed via Docker Compose. 

**Workload:** Read-heavy, specifically for this issue. 50% user-timeline reads, 40% home-timeline reads, 10% compose to keep content fresh.

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

To build the checker and benchmark, run the following in the terminal: 

```bash
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j4
```

CMake automatically downloads `nlohmann/json` (v3.11.3) via `FetchContent` during the configure step. This might 10-15 seconds the first time it's run.

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

For the benchmark, three load levels are available and they can be run individually:

```bash
./benchmark --base-url http://localhost:8080 --load-level light
./benchmark --base-url http://localhost:8080 --load-level medium
./benchmark --base-url http://localhost:8080 --load-level heavy
```

On repeated runs, you can skip the benchmark user setup phase using this:

```bash
./benchmark --base-url http://localhost:8080 --load-level medium --skip-setup
```

Load level parameters:


| Level  | Target RPS | Duration | Warmup | Users |
| ------ | ---------- | -------- | ------ | ----- |
| light  | 100        | 60s      | 10s    | 20    |
| medium | 300        | 120s     | 20s    | 50    |
| heavy  | 600        | 180s     | 30s    | 100   |


### Output format

The benchmark emits one JSON line per metric, followed by a summary line:

```
{"metric":"p50_ms","value":7.82}
{"metric":"p95_ms","value":14.40}
{"metric":"p99_ms","value":21.55}
{"metric":"p999_ms","value":38.12}
{"metric":"user_timeline_p50_ms","value":6.31}
{"metric":"user_timeline_thrift_p50_ms","value":4.10}
{"metric":"home_timeline_p50_ms","value":6.09}
{"metric":"home_timeline_thrift_p50_ms","value":2.80}
...
{"metric":"throughput_rps","value":298.7}
{"metric":"success_rate","value":1.0}
{"metric":"cpu_percent","value":102.9}
{"metric":"memory_mb","value":812.3}
```

`p50_ms` is the primary metric VibeServe uses to evaluate candidates. `user_timeline_thrift_p50_ms` and `home_timeline_thrift_p50_ms` are intermediate latencies (nginx to first Thrift service hop).

## Rebuilding

To teardown the system, run the folllowing:

```bash
cd 3rd_party/deathstarbench/socialNetwork
docker compose down -v
```

This will remove MongoDB and Redis volumes, starting clean for the next run.