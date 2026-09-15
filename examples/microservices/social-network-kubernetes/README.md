# Social Network on Kubernetes

This input deploys the pinned DeathStarBench Social Network source to an
existing Kubernetes context. Deployment resources are isolated in a fresh
evaluator-owned namespace and the nginx frontend is exposed by port-forward.

The source is pinned to commit
`867806e575e1f7fb24437ae969910ddb17a76121` from
`https://github.com/vibesys-playground/DeathStarBench.git`.

Prepare the `vibesys-k8s-social` kind cluster, then run:

```bash
vibesys --local --run-environment local --profiler none \
  --input examples/microservices/social-network-kubernetes
```

The accuracy gate runs the light semantically validated workload. The official
benchmark uses the full read-heavy workload and requires every operation type
to succeed. Both commands consume the packaged workload definition, outside
the mutable candidate checkout. Profiling remains disabled until Kubernetes
telemetry capture is available.

This input supports the local run environment. Its evaluator invokes host
`kubectl`, Docker, and kind and reads the host Kubernetes context. Container and
Modal evaluator environments and Kubernetes profiling are not supported.
