# Train Ticket Kubernetes assets

This package deploys the six read-only Train Ticket services exercised by the
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

`generate_manifest.py` deterministically regenerates the namespace-neutral
manifest. The runtime injects its owned namespace and forwards all six service
endpoints for direct semantic checking and benchmarking.
