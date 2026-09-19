# Train Ticket on Kubernetes

This input deploys the six-service Train Ticket workload used by the existing
accuracy checker and benchmark. It uses the pinned upstream source at commit
`350f62000e6658e0e543730580c599d8558253e7` from
`https://github.com/FudanSELab/train-ticket.git` and local source-built images.
This revision matches the packaged oracle's MongoDB API contract. Later JPA
revisions use different entity identifiers and require a different oracle.

The minimal topology contains six MongoDB instances and the config, station,
train, travel, route, and price services. The evaluator accesses those six
application services through direct port-forwards. The
gateway, Redis, and other Train Ticket services are outside this workload's
contract.

Prepare kind cluster `vibesys-k8s-train` with context
`kind-vibesys-k8s-train`, then run:

```bash
vibesys --local --run-environment local --profiler none \
  --input examples/microservices/train-ticket-kubernetes --task kubernetes
```

The accuracy and benchmark commands address the six application services
through evaluator-owned port-forwards and consume a task-owned workload. Each
invocation creates and removes its own namespace. Profiling remains disabled
until Kubernetes telemetry capture is available.

This input supports the local run environment. Its evaluator invokes host
`kubectl`, Docker, and kind and reads the host Kubernetes context. Container and
Modal evaluator environments and Kubernetes profiling are not supported.

These assets have configuration and packaging tests. This port has not yet run
a live Kubernetes accuracy or benchmark campaign for this scenario. Dependency
image tags are not all pinned by digest.
