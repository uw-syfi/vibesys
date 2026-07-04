#!/usr/bin/env bash
set -euo pipefail

# Start a local Train Ticket cluster from the upstream prebuilt images.
#
# Defaults:
#   API gateway: http://localhost:18888
#   Direct services on their Train Ticket ports
#
# Override examples:
#   TT_TAG=0.0.4 examples/microservices/train-ticket/scripts/start-local-cluster.sh start
#   TT_GATEWAY_PORT=28888 examples/microservices/train-ticket/scripts/start-local-cluster.sh start
#   examples/microservices/train-ticket/scripts/start-local-cluster.sh build-source
#   TT_NAMESPACE=localtrain TT_TAG=source TT_GATEWAY_TAG=source TT_SKIP_PULL=1 examples/microservices/train-ticket/scripts/start-local-cluster.sh start

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INPUT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd -- "${INPUT_DIR}/../.." && pwd)"
SOURCE_COMPOSE="${REPO_ROOT}/3rd_party/train-ticket/deployment/docker-compose-manifests/quickstart-docker-compose.yml"

TT_NAMESPACE="${TT_NAMESPACE:-codewisdom}"
TT_TAG="${TT_TAG:-0.2.0}"
TT_GATEWAY_TAG="${TT_GATEWAY_TAG:-latest}"
TT_PROJECT="${TT_PROJECT:-train-ticket-local}"
TT_GATEWAY_PORT="${TT_GATEWAY_PORT:-18888}"
TT_COMPOSE_FILE="${TT_COMPOSE_FILE:-/tmp/${TT_PROJECT}-quickstart-docker-compose.yml}"
TT_SKIP_PULL="${TT_SKIP_PULL:-0}"
TT_SOURCE_DIR="${TT_SOURCE_DIR:-${REPO_ROOT}/3rd_party/train-ticket}"
TT_BUILD_REPO="${TT_BUILD_REPO:-localtrain}"
TT_BUILD_TAG="${TT_BUILD_TAG:-source}"

usage() {
  cat <<EOF
Usage: $0 <command>

Commands:
  start      Generate compose file, pull images, start the cluster, wait for core services
  stop       Stop and remove the cluster containers/network
  status     Show compose service status
  logs       Follow compose logs
  check      Run the Train Ticket checker against local direct service ports
  bench      Run a short benchmark against local direct service ports
  build-source
             Package Train Ticket from source and build local Docker images
  config     Generate and print the temporary compose file path

Environment:
  TT_NAMESPACE      Docker image namespace (default: ${TT_NAMESPACE})
  TT_TAG            Docker image tag (default: ${TT_TAG})
  TT_GATEWAY_TAG    Gateway image tag (default: ${TT_GATEWAY_TAG})
  TT_PROJECT        Docker Compose project name (default: ${TT_PROJECT})
  TT_GATEWAY_PORT   Host port for API gateway (default: ${TT_GATEWAY_PORT})
  TT_COMPOSE_FILE   Generated compose path (default: ${TT_COMPOSE_FILE})
  TT_SKIP_PULL       Set to 1 when using local source-built images
  TT_SOURCE_DIR      Train Ticket source checkout (default: ${TT_SOURCE_DIR})
  TT_BUILD_REPO      Local image namespace for build-source (default: ${TT_BUILD_REPO})
  TT_BUILD_TAG       Local image tag for build-source (default: ${TT_BUILD_TAG})

URLs after start:
  Gateway: http://localhost:${TT_GATEWAY_PORT}
  Direct service ports used by check/bench:
    config=15679 station=12345 train=14567 travel=12346 route=11178 price=16579
EOF
}

require_tools() {
  command -v docker >/dev/null || { echo "docker is required" >&2; exit 127; }
  docker compose version >/dev/null || { echo "docker compose v2 is required" >&2; exit 127; }
  command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 127; }
}

port_in_use() {
  local port="$1"
  ss -ltn | awk '{print $4}' | grep -Eq "(:|\\])${port}$"
}

check_ports() {
  local failed=0
  for port in \
    "${TT_GATEWAY_PORT}" \
    15679 12345 14567 12346 11178 16579
  do
    if port_in_use "${port}"; then
      echo "Port ${port} is already in use. Override TT_GATEWAY_PORT or stop the process using the port." >&2
      failed=1
    fi
  done
  if [[ "${failed}" != 0 ]]; then
    exit 1
  fi
}

generate_compose() {
  [[ -f "${SOURCE_COMPOSE}" ]] || {
    echo "Missing source compose file: ${SOURCE_COMPOSE}" >&2
    exit 1
  }
  mkdir -p "$(dirname -- "${TT_COMPOSE_FILE}")"
  python3 - "${SOURCE_COMPOSE}" "${TT_COMPOSE_FILE}" "${TT_GATEWAY_PORT}" "${TT_NAMESPACE}" "${TT_TAG}" "${TT_GATEWAY_TAG}" <<'PY'
from pathlib import Path
import sys

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
gateway_port = sys.argv[3]
namespace = sys.argv[4]
tag = sys.argv[5]
gateway_tag = sys.argv[6]

# Keep this explicit. The upstream quickstart compose references several
# optional images that are no longer published, omits Nacos, and leaves MySQL
# without initialization env. The dashboard image also expects many services
# that are outside this minimal read-only cluster. This compose is enough for
# the checker and benchmark workloads in examples/microservices/train-ticket.
service_ports = {
    "ts-config-service": 15679,
    "ts-station-service": 12345,
    "ts-train-service": 14567,
    "ts-travel-service": 12346,
    "ts-route-service": 11178,
    "ts-price-service": 16579,
}
mysql = {
    "ts-config-mysql": ("root", "ts-config-mysql"),
    "ts-station-mysql": ("Abcd1234#", "ts-station-mysql"),
    "ts-train-mysql": ("Abcd1234#", "ts-train-mysql"),
    "ts-travel-mysql": ("root", "ts-travel-mysql"),
    "ts-route-mysql": ("Abcd1234#", "ts"),
    "ts-price-mysql": ("Abcd1234#", "ts"),
}
mongo_services = [
    "ts-config-mongo",
    "ts-station-mongo",
    "ts-train-mongo",
    "ts-travel-mongo",
    "ts-route-mongo",
    "ts-price-mongo",
]
service_env = {
    "ts-config-service": {
        "NACOS_ADDRS": "nacos:8848",
        "CONFIG_MYSQL_HOST": "ts-config-mysql",
        "CONFIG_MYSQL_DATABASE": "ts-config-mysql",
        "CONFIG_MYSQL_PASSWORD": "root",
    },
    "ts-station-service": {
        "NACOS_ADDRS": "nacos:8848",
        "STATION_MYSQL_HOST": "ts-station-mysql",
        "STATION_MYSQL_DATABASE": "ts-station-mysql",
        "STATION_MYSQL_PASSWORD": "Abcd1234#",
    },
    "ts-train-service": {
        "NACOS_ADDRS": "nacos:8848",
        "TRAIN_MYSQL_HOST": "ts-train-mysql",
        "TRAIN_MYSQL_DATABASE": "ts-train-mysql",
        "TRAIN_MYSQL_PASSWORD": "Abcd1234#",
    },
    "ts-travel-service": {
        "NACOS_ADDRS": "nacos:8848",
        "TRAVEL_MYSQL_HOST": "ts-travel-mysql",
        "TRAVEL_MYSQL_DATABASE": "ts-travel-mysql",
        "TRAVEL_MYSQL_PASSWORD": "root",
        "TRAIN_SERVICE_HOST": "ts-train-service",
        "ROUTE_SERVICE_HOST": "ts-route-service",
    },
    "ts-route-service": {
        "NACOS_ADDRS": "nacos:8848",
        "ROUTE_MYSQL_HOST": "ts-route-mysql",
        "ROUTE_MYSQL_DATABASE": "ts",
        "ROUTE_MYSQL_PASSWORD": "Abcd1234#",
    },
    "ts-price-service": {
        "NACOS_ADDRS": "nacos:8848",
        "PRICE_MYSQL_HOST": "ts-price-mysql",
        "PRICE_MYSQL_DATABASE": "ts",
        "PRICE_MYSQL_PASSWORD": "Abcd1234#",
    },
}

lines = [
    "services:",
    "  nacos:",
    "    image: nacos/nacos-server:v2.0.3",
    "    environment:",
    "      MODE: standalone",
    "    networks:",
    "      - my-network",
    "",
    "  redis:",
    "    image: redis:latest",
    "    networks:",
    "      - my-network",
    "",
    "  ts-gateway-service:",
    f"    image: {namespace}/ts-gateway-service:{gateway_tag}",
    "    restart: always",
    "    environment:",
    "      NACOS_ADDRS: nacos:8848",
    "    ports:",
    f"      - {gateway_port}:18888",
    "    networks:",
    "      - my-network",
    "",
]

for name, (password, database) in mysql.items():
    lines.extend([
        f"  {name}:",
        "    image: mysql:5.7",
        "    restart: always",
        "    environment:",
        f"      MYSQL_ROOT_PASSWORD: {password!r}",
        f"      MYSQL_DATABASE: {database!r}",
        "    networks:",
        "      - my-network",
        "",
    ])

for name in mongo_services:
    lines.extend([
        f"  {name}:",
        "    image: mongo:3.4",
        "    restart: always",
        "    networks:",
        "      - my-network",
        "",
    ])

for name, port in service_ports.items():
    lines.extend([
        f"  {name}:",
        f"    image: {namespace}/{name}:{tag}",
        "    restart: always",
        "    environment:",
    ])
    for key, value in service_env[name].items():
        lines.append(f"      {key}: {value!r}")
    lines.extend([
        "    ports:",
        f"      - {port}:{port}",
        "    networks:",
        "      - my-network",
        "",
    ])

lines.extend([
    "networks:",
    "  my-network:",
    "    driver: bridge",
])

dst.write_text("\n".join(lines) + "\n")
PY
  echo "${TT_COMPOSE_FILE}"
}

compose() {
  NAMESPACE="${TT_NAMESPACE}" TAG="${TT_TAG}" docker compose -p "${TT_PROJECT}" -f "${TT_COMPOSE_FILE}" "$@"
}

build_source() {
  [[ -f "${TT_SOURCE_DIR}/pom.xml" ]] || {
    echo "Missing Train Ticket source checkout: ${TT_SOURCE_DIR}" >&2
    exit 1
  }

  # The upstream Dockerfiles still reference old floating base images. Keep the
  # source tree unchanged and provide compatible local tags instead.
  docker pull eclipse-temurin:8-jre
  docker tag eclipse-temurin:8-jre java:8-jre
  docker pull python:3.8-bullseye
  docker tag python:3.8-bullseye python:3

  docker run --rm \
    -v "${TT_SOURCE_DIR}:/workspace" \
    -v "${HOME}/.m2:/root/.m2" \
    -w /workspace \
    maven:3.8.8-eclipse-temurin-8 \
    mvn clean package -Dmaven.test.skip=true

  (cd "${TT_SOURCE_DIR}" && ./hack/build-image.sh "${TT_BUILD_REPO}" "${TT_BUILD_TAG}")
}

wait_for_core_services() {
  local deadline=$((SECONDS + 180))
  echo "Waiting for core direct services..."
  while (( SECONDS < deadline )); do
    if python3 - <<'PY'
from urllib.request import urlopen

checks = [
    ("config", "http://localhost:15679/api/v1/configservice/welcome", "Config Service"),
    ("station", "http://localhost:12345/api/v1/stationservice/welcome", "Station Service"),
    ("train", "http://localhost:14567/api/v1/trainservice/trains/welcome", "Train Service"),
    ("travel", "http://localhost:12346/api/v1/travelservice/welcome", "Travel Service"),
    ("route", "http://localhost:11178/api/v1/routeservice/welcome", "Route Service"),
    ("price", "http://localhost:16579/api/v1/priceservice/prices/welcome", "Price Service"),
]
for _, url, marker in checks:
    try:
        with urlopen(url, timeout=3) as response:
            body = response.read().decode("utf-8", errors="replace")
        if marker not in body:
            raise RuntimeError(f"missing marker {marker!r}")
    except Exception:
        raise SystemExit(1)
raise SystemExit(0)
PY
    then
      echo "Core direct services are responding."
      return 0
    fi
    sleep 5
  done
  echo "Core direct services did not become ready within 180s. Use '$0 logs' to inspect startup." >&2
  return 1
}

cmd="${1:-}"
case "${cmd}" in
  start)
    require_tools
    generate_compose >/dev/null
    if ! compose ps -q | grep -q .; then
      check_ports
    fi
    echo "Starting Train Ticket with images ${TT_NAMESPACE}/<service>:${TT_TAG}"
    echo "Generated compose: ${TT_COMPOSE_FILE}"
    if [[ "${TT_SKIP_PULL}" == "1" ]]; then
      echo "Skipping compose pull because TT_SKIP_PULL=1"
    else
      compose pull
    fi
    compose up -d --remove-orphans
    wait_for_core_services || true
    echo "Gateway: http://localhost:${TT_GATEWAY_PORT}"
    echo "Checker: $0 check"
    echo "Benchmark: TT_BENCH_RATE=10 TT_BENCH_DURATION=30 TT_BENCH_CONCURRENCY=32 $0 bench"
    ;;
  stop)
    require_tools
    generate_compose >/dev/null
    compose down --remove-orphans
    ;;
  status)
    require_tools
    generate_compose >/dev/null
    compose ps
    ;;
  logs)
    require_tools
    generate_compose >/dev/null
    compose logs -f --tail=200
    ;;
  build-source)
    require_tools
    build_source
    echo "Built local Train Ticket images as ${TT_BUILD_REPO}/<service>:${TT_BUILD_TAG}"
    echo "Start them with:"
    echo "  TT_NAMESPACE=${TT_BUILD_REPO} TT_TAG=${TT_BUILD_TAG} TT_GATEWAY_TAG=${TT_BUILD_TAG} TT_SKIP_PULL=1 $0 start"
    ;;
  check)
    python3 "${INPUT_DIR}/accuracy_checker/checker.py" \
      --base-url "http://localhost:${TT_GATEWAY_PORT}" \
      --direct-services \
      --allow-empty
    ;;
  bench)
    python3 "${INPUT_DIR}/benchmark/benchmark.py" \
      --base-url "http://localhost:${TT_GATEWAY_PORT}" \
      --direct-services \
      --rate "${TT_BENCH_RATE:-10}" \
      --duration "${TT_BENCH_DURATION:-30}" \
      --concurrency "${TT_BENCH_CONCURRENCY:-32}"
    ;;
  config)
    require_tools
    generate_compose
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
