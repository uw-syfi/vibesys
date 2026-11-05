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
# By default the package's wheel is built from ../python. A release installs the wheel
# it already built and verified instead, by replacing the stage `wheels` with a directory
# that holds exactly that one wheel, and stamps loadgen with the release version:
#   docker buildx build --build-context wheels=dist/evaluator \
#     --build-arg VIBESYS_EVALUATOR_VERSION=<version> -f ... sdk/vibesys-evaluator
# Without VIBESYS_EVALUATOR_VERSION, loadgen keeps its development version.
#
# Every base is pinned by digest and every download by checksum. Update them together,
# with a version bump of the package (one version for the package, the Go module and
# this image).

ARG KUBECTL_VERSION=v1.34.0
ARG KUBECTL_SHA256=cfda68cba5848bc3b6c6135ae2f20ba2c78de20059f68789c090166d6abc3e2c
ARG VIBESYS_EVALUATOR_VERSION=

FROM golang:1.25.9-bookworm@sha256:298734aec230b5f3e8cee450ce6d7eccc39f1797ba548ee90d57e9803030c6c3 AS go
ARG KUBECTL_VERSION
ARG KUBECTL_SHA256
ARG VIBESYS_EVALUATOR_VERSION
ENV CGO_ENABLED=0 GOFLAGS=-trimpath
WORKDIR /src/loadgen
COPY loadgen/go.mod loadgen/go.sum ./
RUN go mod download
COPY loadgen/ ./
RUN stamp="" \
 && if [ -n "${VIBESYS_EVALUATOR_VERSION}" ]; then \
      stamp="-X github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/version.Version=${VIBESYS_EVALUATOR_VERSION}"; \
    fi \
 && go build -ldflags "${stamp}" -o /out/ ./cmd/loadgen ./cmd/otelcapture
# kubectl, checked against its published sha256 (the version of the clusters' nodes).
RUN curl -fsSL -o /out/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
 && echo "${KUBECTL_SHA256}  /out/kubectl" | sha256sum -c - \
 && chmod 0755 /out/kubectl

FROM moby/buildkit:v0.22.0@sha256:832fa7aa1eb3deb56fa62ae933bfa42dad9a83ff4824dbbaf173b49c722b59d0 AS buildkit

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS wheel
COPY python/ /src/python/
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels /src/python

# The wheel to install: the one built above, or a release's via --build-context wheels=DIR.
FROM scratch AS wheels
COPY --from=wheel /wheels/ /

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
COPY --from=wheels / /tmp/wheels/
RUN set -- /tmp/wheels/*.whl \
 && if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then echo "expected exactly one wheel, found: $*" >&2; exit 1; fi \
 && pip install --no-cache-dir "$1" && rm -rf /tmp/wheels
COPY --from=go /out/loadgen /out/otelcapture /out/kubectl /usr/local/bin/
COPY --from=buildkit /usr/bin/buildctl /usr/local/bin/buildctl
# A job runs as the invoking user, whose HOME may not exist: kubectl and buildctl keep
# their caches under /tmp. Pods of the package write only below /vse (an emptyDir).
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp KUBECACHEDIR=/tmp/.kube/cache
RUN loadgen --capabilities >/dev/null && kubectl version --client >/dev/null && buildctl --version >/dev/null \
 && python -c "import vibesys_evaluator.deploy.k8s"
