# The image vibesys-evaluator:<version>, which task images build FROM:
#
#   FROM vibesys-evaluator:<version>@sha256:...
#   COPY . /task
#
# It holds Python with the vibesys_evaluator package, loadgen and otelcapture (the Go
# module in ../loadgen), kubectl and buildctl. The orchestrator (python /task/bench.py)
# runs in it in the container vibesys-cli starts, and its phase, seed and probe pods run
# the task image in the cluster, so everything a phase needs is in this one image.
#
# Build with sdk/vibesys-evaluator as the context:
#   docker build -f sdk/vibesys-evaluator/image/Dockerfile sdk/vibesys-evaluator
#
# Every base is pinned by digest and every download by checksum. Update them together,
# with a version bump of the package (one version for the package, the Go module and
# this image).

ARG KUBECTL_VERSION=v1.34.0
ARG KUBECTL_SHA256=cfda68cba5848bc3b6c6135ae2f20ba2c78de20059f68789c090166d6abc3e2c

FROM golang:1.24-bookworm@sha256:1a6d4452c65dea36aac2e2d606b01b4a029ec90cc1ae53890540ce6173ea77ac AS go
ARG KUBECTL_VERSION
ARG KUBECTL_SHA256
ENV CGO_ENABLED=0 GOFLAGS=-trimpath
WORKDIR /src/loadgen
COPY loadgen/go.mod loadgen/go.sum ./
RUN go mod download
COPY loadgen/ ./
RUN go build -o /out/ ./cmd/loadgen ./cmd/otelcapture
# kubectl, checked against its published sha256 (the version of the clusters' nodes).
RUN curl -fsSL -o /out/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
 && echo "${KUBECTL_SHA256}  /out/kubectl" | sha256sum -c - \
 && chmod 0755 /out/kubectl

FROM moby/buildkit:v0.22.0@sha256:832fa7aa1eb3deb56fa62ae933bfa42dad9a83ff4824dbbaf173b49c722b59d0 AS buildkit

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS wheel
COPY python/ /src/python/
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels /src/python

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
COPY --from=wheel /wheels/ /tmp/wheels/
RUN pip install --no-cache-dir /tmp/wheels/*.whl && rm -rf /tmp/wheels
COPY --from=go /out/loadgen /out/otelcapture /out/kubectl /usr/local/bin/
COPY --from=buildkit /usr/bin/buildctl /usr/local/bin/buildctl
# A job runs as the invoking user, whose HOME may not exist: kubectl and buildctl keep
# their caches under /tmp. Pods of the package write only below /vse (an emptyDir).
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp KUBECACHEDIR=/tmp/.kube/cache
RUN loadgen --capabilities >/dev/null && kubectl version --client >/dev/null && buildctl --version >/dev/null \
 && python -c "import vibesys_evaluator.deploy.k8s"
