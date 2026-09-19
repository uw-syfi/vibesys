# Train Ticket Kubernetes assets

This package deploys the six Train Ticket services exercised by the
ServiceBench workload, plus their MongoDB dependencies.
Every evaluation builds the six Java modules from the assembled candidate
checkout. The trusted multi-stage Dockerfile runs Maven inside the build and
does not consume pre-existing `target` artifacts.
Its Maven layer packages all six modules before the module-specific build
argument is declared, so Docker reuses one reactor build for the six runtime
images.

The config expects workspace source `train-ticket/`, kind cluster
`vibesys-k8s-train`, and context `kind-vibesys-k8s-train`. The first lifecycle
start builds and loads the images. Namespace resets in the same evaluator reuse
those exact images, while a new evaluator process rebuilds the candidate.

`generate_manifest.py` is the source of truth: it deterministically regenerates
the namespace-neutral `manifest.yaml`, and a unit test fails if the committed
file differs from its output. Edit the script, then rerun it. The runtime injects its owned namespace and forwards all six service
endpoints for direct semantic checking and benchmarking.

## Open problem: unvalidated against upstream

The manifest provisions `mongo:3.4` per service and injects no environment
variables. The Train Ticket revision checked out as this repository's
`3rd_party/train-ticket` submodule (`313886e99befb94be6cd45f085c98e0019f59829`)
is MySQL and Nacos based, and this manifest does not provide either. The task
pins a different revision (`350f62000e6658e0e543730580c599d8558253e7`), which
its README describes as MongoDB based; that was not re-verified here. This
configuration has not been run live and may target a different Train Ticket
revision than the one the rest of the repository uses. Do not treat it as
working until it is validated on a cluster.
