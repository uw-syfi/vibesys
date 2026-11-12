# The task image: the service checker (checker.py), the TraceLab adapter (tracelab/) and
# Request Factory's session_runner at the revision the adapter replays with. It has no GPU and
# reaches the candidate's server only over HTTP, at VIBESYS_SERVICE_URL. vibesys-cli builds it
# with this directory as the context. checker.py is a byte-for-byte copy of
# .vibesys/shared/accuracy_checker/checker.py, kept here so that it is part of the task's
# version and of this image, never read from a candidate's checkout. Every base is pinned by
# digest and every Python distribution by hash.

# session_runner, built from the pinned Request Factory revision with the time-boxed measurement
# patch applied (request-factory/windowed.patch: --warmup-secs / --duration-secs and the
# replay.window summary block). The patch is two commits on top of the pinned revision; its
# bytes are part of this task image's tag.
FROM rust:1.90-slim-bookworm@sha256:64232e656c058f4468e8d024e990acff04f0fd5a5c0a88a574dc37773d7325c9 AS request-factory
# req-frontend links OpenSSL through openssl-sys, which locates it with pkg-config; some of its
# dependencies compile C++.
RUN apt-get update \
 && apt-get install -y --no-install-recommends pkg-config libssl-dev g++ make git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ARG REQUEST_FACTORY_REV=89dce4a64ae12e7084fbe464ddda882fa0a4c482
RUN git init /src/request-factory \
 && git -C /src/request-factory fetch --depth 1 https://github.com/uw-syfi/request-factory "$REQUEST_FACTORY_REV" \
 && git -C /src/request-factory checkout --detach FETCH_HEAD \
 && test "$(git -C /src/request-factory rev-parse HEAD)" = "$REQUEST_FACTORY_REV"
COPY request-factory/windowed.patch /tmp/windowed.patch
RUN git -C /src/request-factory apply --whitespace=nowarn /tmp/windowed.patch \
 && cd /src/request-factory \
 && cargo install --locked --path . --root /opt/request-factory --bin session_runner

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
COPY requirements.txt /tmp/task-requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/task-requirements.txt \
 && rm /tmp/task-requirements.txt
COPY --from=request-factory /opt/request-factory/bin/session_runner /usr/local/bin/session_runner
RUN session_runner --help >/dev/null

COPY . /task
# A job runs as the invoking user, whose HOME may not exist. The operator's Hugging Face cache
# is mounted read-only as "hf" (the task's [environment] needs); the adapter reads the
# exact-revision tokenizer from it, offline.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp \
    HF_HOME=/vibesys/mounts/hf HF_HUB_OFFLINE=1
