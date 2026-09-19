# Social Network Kubernetes assets

`manifest.yaml` is generated from the pinned DeathStarBench
`socialNetwork/helm-chart/socialnetwork` chart with its default values. Its
asset-loader init container (`alpine-container` on `nginx-thrift`) is adjusted
to fetch the pinned VibeSys source revision instead of an unpinned fork branch;
it needs network access at pod start. The
runtime builds `socialNetwork/Dockerfile` from the current candidate and
overrides every C++ application Deployment with that run-specific image before
rollout readiness. MongoDB, Redis, Memcached, Jaeger, and the OpenResty
gateway retain the chart dependency image tags. `alpine/git`,
`jaegertracing/all-in-one`, and the application image are `:latest`; the rest
are version tags without digests, so the source revision pin does not pin
image digests. The media upload frontend is
excluded because ServiceBench exercises user registration, follows, post
composition, and user/home timeline reads through `nginx-thrift`; post
composition still exercises the media service itself.

The local kind manifest pins `nginx-thrift` to one worker and raises its memory
limit to 512 MiB. This matches its 100 millicore request and avoids worker and
memory oversubscription during fixture setup on a constrained local cluster.

## Edits to the generated chart output

- `readinessProbe` (`tcpSocket` port 9090, `initialDelaySeconds` 2,
  `periodSeconds` 2, `timeoutSeconds` 1, `failureThreshold` 90) is added to the
  11 C++ service Deployments.
- Container CPU limit is `"1"` on 25 of 26 Deployments; `jaeger` keeps `100m`.
  CPU requests stay `100m` everywhere.
- Memory limit is `512Mi` on the 6 MongoDB Deployments (plus `nginx-thrift`,
  described above); other Deployments keep `128Mi`.
- The `media-frontend` subchart was dropped, but the `service-config.json` in
  16 ConfigMaps still lists `media-frontend` and other upstream entries that
  have no workload here (for example `write-home-timeline-service`,
  `compose-post-redis`, `redis-primary`). They are unused by the exercised
  workload.

Regenerate the manifest from the pinned source checkout with:

```shell
helm template social \
  deathstarbench/socialNetwork/helm-chart/socialnetwork \
  > .vibesys/tasks/kubernetes/manifest.yaml
```

After regeneration, reapply the pinned asset-loader revision and the edits
listed above.

The expected kind cluster and context are `vibesys-k8s-social` and
`kind-vibesys-k8s-social`. The evaluator forwards `service/nginx-thrift` to
`127.0.0.1:18080`; ServiceBench performs its own seeded fixture preparation
through that gateway before measuring timeline operations.

The Kubernetes workload omits the optional Thrift timing-header captures from
the Compose task. The pinned upstream gateway does not emit those diagnostic
headers. HTTP status, response schema, fixture state, and read-after-write
validation remain required by the Social Network application adapter.
