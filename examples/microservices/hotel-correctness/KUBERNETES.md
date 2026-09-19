# Kubernetes task

Run from the VibeSys checkout with Docker, Go, Python, `kubectl`, `kind`, and
Linux sandbox prerequisites available. Create a kind cluster named `vibesys-k8s`
and select context `kind-vibesys-k8s`. The runtime needs permission to build/load
images and create/delete namespaces in that cluster. Port 15000 must be free.

```bash
kind create cluster --name vibesys-k8s
vibesys --runs-dir /path/to/runs --local --run-environment local --profiler none \
  --input examples/microservices/hotel-correctness --task kubernetes
```

The task pins DeathStarBench commit
`867806e575e1f7fb24437ae969910ddb17a76121`. It builds the candidate Hotel image,
loads it into kind, deploys the task-owned namespace-scoped manifests, waits for
HTTP readiness, and forwards the frontend. Runtime configuration and manifests
live in `.vibesys/tasks/kubernetes/` (see its README for provenance).
Use a dedicated cluster and sufficient capacity for the application and MongoDB
pods. Concurrent evaluations must not reuse the configured local forward port.

Accuracy uses the same example-owned Go oracle as Compose. Its managed stop
command scales down application deployments and the reservation cache, waits
for pods and forwards to stop, then lets the oracle verify unavailability. Its
start command restores replicas, forwards, and readiness. MongoDB remains alive,
so recovery checks exercise persisted reservations after volatile cache loss.
The outer runtime owns namespace cleanup on success or failure.

The pinned implementation does not satisfy every stricter concurrency or
post-restart availability property. These remain opt-in oracle checks; durable
reservation capacity is mandatory. The Kubernetes benchmark measures the shared
workload through the forwarded HTTP endpoint. It retains the default application
tracing configuration but does not collect or validate trace topology. A passing
gate does not establish equivalence for every possible input or deployment.
