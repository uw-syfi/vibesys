# Train Ticket Evaluator Inputs

Accuracy and workload inputs for a running Train Ticket deployment. The shared
Go service evaluator runs both modes; independent Train Ticket benchmark and
accuracy adapters retain separate application-specific semantic oracles.
Both modes can run without a full `vibesys --input` optimization run.

Expected target for a gateway/proxy deployment:

- UI proxy: `http://localhost:8080`
- Gateway directly: `http://localhost:18888`
- Kubernetes NodePort UI: `http://<node-ip>:32677`

The scripts call `/api/v1/...` endpoints through whichever base URL you pass.

```bash
go -C examples/evaluators/microservice run ./cmd/servicebench \
  --mode accuracy \
  --workload "$PWD/examples/microservices/train-ticket/benchmark/workload.toml" \
  --base-url http://localhost:8080 \
  --seed random
go -C examples/evaluators/microservice run ./cmd/servicebench \
  --workload "$PWD/examples/microservices/train-ticket/benchmark/workload.toml" \
  --base-url http://localhost:8080 \
	--duration 30 \
  --output-json /tmp/train_ticket_bench.json
```

Both modes require Go; dependencies are pinned by
`examples/evaluators/microservice/go.sum`.

For the prebuilt-image local Docker Compose helper below, use the helper
`check`/`bench` commands. The prebuilt
`codewisdom` 0.2.0 service images predate Nacos support and never register
with the discovery server, so the published gateway image starts but returns
`503` for every route — deterministically, not transiently. The gateway path
only works with source-built `localtrain:source` images (verified), which do
register with Nacos.

## Local Cluster

Use the helper script to start a minimal local Docker Compose cluster with
prebuilt images:

```bash
examples/microservices/train-ticket/scripts/start-local-cluster.sh start
examples/microservices/train-ticket/scripts/start-local-cluster.sh check
examples/microservices/train-ticket/scripts/start-local-cluster.sh bench
examples/microservices/train-ticket/scripts/start-local-cluster.sh stop
```

Defaults:

- Gateway: `http://localhost:18888`
- Images: `codewisdom/<service>:0.2.0` (gateway: `codewisdom/ts-gateway-service:latest`,
  the only tag published for it)

The script generates a temporary compose file under `/tmp` that exposes the
gateway and the core read-only service ports used by the checker. Override
with `TT_GATEWAY_PORT`, `TT_NAMESPACE`, `TT_TAG`, or `TT_GATEWAY_TAG` if
needed.

The local helper starts a minimal API cluster: Nacos, Redis, the gateway, and
the config/station/train/travel/route/price services with their local MongoDB
and MySQL dependencies. The 0.2.0 prebuilt images store data in MongoDB (the
MySQL containers and `*_MYSQL_*` env are used by source-built v1.0.0 images
instead; both datastores are started so either image set works). All services
self-seed reference data on startup, so `check` verifies the exact v0.2.0
catalog directly through the six service ports. Services outside that contract
are excluded from the checker and benchmark. The published
dashboard image is intentionally not started because its nginx config
references additional services outside this minimal read-only cluster.

`stop` removes the containers together with their anonymous data volumes;
each `start` reseeds from scratch.

## Build From Source

The source-build path is useful because the public prebuilt image set is stale:
some images referenced by the upstream compose file are no longer published
with the expected tags, including services such as `ts-food-delivery-service`
and `ts-station-food-service`, and the gateway is not published under the same
`0.2.0` tag as the other images.

Build all Train Ticket service images locally:

```bash
examples/microservices/train-ticket/scripts/start-local-cluster.sh build-source
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
examples/microservices/train-ticket/scripts/start-local-cluster.sh start
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
go -C examples/evaluators/microservice run ./cmd/servicebench \
  --mode accuracy \
  --workload "$PWD/examples/microservices/train-ticket/benchmark/workload.toml" \
  --seed random \
  --target config=http://localhost:15679 \
  --target station=http://localhost:12345 \
  --target train=http://localhost:14567 \
  --target travel=http://localhost:12346 \
  --target route=http://localhost:11178 \
  --target price=http://localhost:16579

go -C examples/evaluators/microservice run ./cmd/servicebench \
  --workload "$PWD/examples/microservices/train-ticket/benchmark/workload.toml" \
  --target config=http://localhost:15679 \
  --target station=http://localhost:12345 \
  --target train=http://localhost:14567 \
  --target travel=http://localhost:12346 \
  --target route=http://localhost:11178 \
  --target price=http://localhost:16579 \
	--duration 30 \
  --concurrency 32
```

Manual gateway runs after source-built deployment:

```bash
go -C examples/evaluators/microservice run ./cmd/servicebench \
  --mode accuracy \
  --workload "$PWD/examples/microservices/train-ticket/benchmark/workload.toml" \
  --base-url http://localhost:18888 \
  --seed random

go -C examples/evaluators/microservice run ./cmd/servicebench \
  --workload "$PWD/examples/microservices/train-ticket/benchmark/workload.toml" \
  --base-url http://localhost:18888 \
	--duration 30 \
  --concurrency 32
```

The checker requires the exact v0.2.0 seed catalog and does not silently accept
empty or structurally different startup data. The committed benchmark workload
runs three independent repetitions and uses their median as `primary_value` so
normal host variance does not steer optimization from a single trial.
