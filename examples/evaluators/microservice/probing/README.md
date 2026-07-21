# Shared probing

This package validates and executes readiness and protocol preflight plans for
both benchmark and accuracy modes. It owns target-coverage checks, transport
gating, sequential execution, polling, and aggregate phase deadlines. It does
not own application endpoints or expected response semantics; adapters declare
those through `api.ReadinessProbe` values.
