# Train Ticket Scripts

Standalone accuracy and benchmark scripts for a running Train Ticket deployment.
These scripts do not depend on the `vibe-serve --ref` input contract.

Expected target for a gateway/proxy deployment:

- UI proxy: `http://localhost:8080`
- Gateway directly: `http://localhost:18888`
- Kubernetes NodePort UI: `http://<node-ip>:32677`

The scripts call `/api/v1/...` endpoints through whichever base URL you pass.

```bash
python inputs/train-ticket/accuracy_checker/checker.py --base-url http://localhost:8080
python inputs/train-ticket/benchmark/benchmark.py --base-url http://localhost:8080 --rate 20 --duration 30 --output-json /tmp/train_ticket_bench.json
```

Both scripts use only the Python standard library.

For the prebuilt-image local Docker Compose helper below, use
`--direct-services` or the helper `check`/`bench` commands. The currently
published gateway image starts, but in the minimal local compose it can return
`503` while the individual service ports are healthy. With source-built
`localtrain:source` images, the gateway path was verified successfully too.

## Local Cluster

Use the helper script to start a minimal local Docker Compose cluster with
prebuilt images:

```bash
inputs/train-ticket/scripts/start-local-cluster.sh start
inputs/train-ticket/scripts/start-local-cluster.sh check
inputs/train-ticket/scripts/start-local-cluster.sh bench
inputs/train-ticket/scripts/start-local-cluster.sh stop
```

Defaults:

- Gateway: `http://localhost:18888`
- Images: `codewisdom/<service>:0.2.0`

The script generates a temporary compose file under `/tmp` that exposes the
gateway and the core read-only service ports used by the checker. Override
with `TT_GATEWAY_PORT`, `TT_NAMESPACE`, or `TT_TAG` if needed.

The local helper starts a minimal API cluster: Nacos, Redis, the gateway, and
the config/station/train/travel/route/price services with their local MongoDB
and MySQL dependencies. Auth-protected services such as contacts are excluded
from the default no-auth checker and benchmark. The published dashboard image
is intentionally not started because its nginx config references additional
services outside this minimal read-only cluster.

## Build From Source

The source-build path is useful because the public prebuilt image set is stale:
some images referenced by the upstream compose file are no longer published
with the expected tags, including services such as `ts-food-delivery-service`
and `ts-station-food-service`, and the gateway is not published under the same
`0.2.0` tag as the other images.

Build all Train Ticket service images locally:

```bash
inputs/train-ticket/scripts/start-local-cluster.sh build-source
```

That command packages the Java services with a Dockerized Maven/JDK 8 build and
then runs the upstream `hack/build-image.sh` script with local image names:

- image namespace: `localtrain`
- image tag: `source`

It also creates local compatibility tags for old upstream Dockerfiles:

- `java:8-jre` -> `eclipse-temurin:8-jre`
- `python:3` -> `python:3.8-bullseye`

Those tags are needed because the source Dockerfiles still use old floating
base images. `java:8-jre` is no longer available from Docker Hub, and the
current `python:3` base no longer has the `libgl1-mesa-glx` package expected by
`ts-avatar-service`.

Deploy the local source-built images:

```bash
TT_NAMESPACE=localtrain \
TT_TAG=source \
TT_GATEWAY_TAG=source \
TT_SKIP_PULL=1 \
inputs/train-ticket/scripts/start-local-cluster.sh start
```

`TT_SKIP_PULL=1` is required for local source-built images; otherwise Docker
Compose tries to pull `localtrain/...` from Docker Hub.

`start` waits for these direct service endpoints:

- `http://localhost:15679/api/v1/configservice/welcome`
- `http://localhost:12345/api/v1/stationservice/welcome`
- `http://localhost:14567/api/v1/trainservice/trains/welcome`
- `http://localhost:12346/api/v1/travelservice/welcome`
- `http://localhost:11178/api/v1/routeservice/welcome`
- `http://localhost:16579/api/v1/priceservice/prices/welcome`

Manual direct-service runs:

```bash
python inputs/train-ticket/accuracy_checker/checker.py \
  --base-url http://localhost:18888 \
  --direct-services \
  --allow-empty

python inputs/train-ticket/benchmark/benchmark.py \
  --base-url http://localhost:18888 \
  --direct-services \
  --rate 10 \
  --duration 30 \
  --concurrency 32
```

Manual gateway runs after source-built deployment:

```bash
python inputs/train-ticket/accuracy_checker/checker.py \
  --base-url http://localhost:18888 \
  --allow-empty

python inputs/train-ticket/benchmark/benchmark.py \
  --base-url http://localhost:18888 \
  --rate 10 \
  --duration 30 \
  --concurrency 32
```
