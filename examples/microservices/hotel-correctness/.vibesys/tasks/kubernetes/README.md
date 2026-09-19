# Hotel Reservation Kubernetes assets

`manifest.yaml` holds 19 Deployments, 19 Services, and 8 ConfigMaps (one per
application service). `runtime.yaml` drives the evaluator lifecycle and
resolves `manifest.yaml` relative to its own directory.

## Provenance

Generated with

```shell
helm template hotel hotelReservation/helm-chart/hotelreservation
```

from DeathStarBench commit `867806e575e1f7fb24437ae969910ddb17a76121`, then
edited by hand:

- The release suffix was stripped from resource names.
- Service-config ConfigMaps use `${NAMESPACE}` placeholders in addresses
  (for example `consul.${NAMESPACE}.svc.cluster.local:8500`); the runtime
  substitutes its owned namespace.
- Each of the 8 application containers sets `command` to `/go/bin/<service>`.
- Each of the 8 application ConfigMaps is mounted at `/workspace/config.json`.

Not changed from the stock chart: no readiness or liveness probes and no
resource requests or limits (readiness is checked by the runtime over HTTP).
Image tags are floating (`latest` for the application, consul, jaeger, and
memcached; `mongo:5.0` for MongoDB), so the source pin does not pin image
digests. The runtime overrides the application image with the candidate build.

Known leftover: 8 ConfigMaps still carry the label
`hotelreservation/service: <service>--hotel-hotelres`, and resources keep the
Helm `managed-by` and `instance` labels. They have no functional effect.
