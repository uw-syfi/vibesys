# Execution environment for the key-value store task: `uv run python` drives the
# checker and benchmark, the checker compares the candidate with a real
# `redis-server` oracle through the `redis` client, and the benchmark runs YCSB
# on Java. YCSB is baked in so a measurement never downloads it. Nothing from
# the task or the candidate is copied in; VibeSys mounts the project at run time.
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

ARG YCSB_VERSION=0.17.0
ARG YCSB_SHA256=353eb96c12a605c30c94928b85780ae4673578a21e2aa13782cd7f591991e484
ARG REDIS_PY_VERSION=5.2.1

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates curl \
        openjdk-17-jre-headless=17.0.* \
        redis-server=5:7.0.* \
    && rm -rf /var/lib/apt/lists/* \
    && (redis-cli shutdown nosave 2>/dev/null || true)

COPY --from=ghcr.io/astral-sh/uv:0.9.7@sha256:ba4857bf2a068e9bc0e64eed8563b065908a4cd6bfb66b531a9c424c8e25e142 /uv /uvx /usr/local/bin/

RUN pip install --no-cache-dir "redis==${REDIS_PY_VERSION}"

RUN curl --fail --location --silent --show-error \
        "https://github.com/brianfrankcooper/YCSB/releases/download/${YCSB_VERSION}/ycsb-redis-binding-${YCSB_VERSION}.tar.gz" \
        --output /tmp/ycsb.tar.gz \
    && echo "${YCSB_SHA256}  /tmp/ycsb.tar.gz" | sha256sum --check --strict \
    && mkdir /opt/ycsb \
    && tar -xzf /tmp/ycsb.tar.gz -C /opt/ycsb --strip-components=1 \
    && rm /tmp/ycsb.tar.gz \
    && chmod -R a+rX /opt/ycsb

# The benchmark reads YCSB from here instead of downloading it to ~/.cache.
ENV KV_STORE_YCSB_HOME=/opt/ycsb
WORKDIR /workspace

RUN python --version && uv --version && java -version 2>&1 | head -n 1 \
    && redis-server --version && python -c "import redis; print(redis.__version__)" \
    && test -x /opt/ycsb/bin/ycsb.sh
