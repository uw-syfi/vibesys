# Social Network Kubernetes assets

`manifest.yaml` is generated from the pinned DeathStarBench
`socialNetwork/helm-chart/socialnetwork` chart with its default values. Its two
asset-loader init containers are adjusted to fetch the pinned VibeSys source
revision instead of an unpinned fork branch. The
runtime builds `socialNetwork/Dockerfile` from the current candidate and
overrides every C++ application Deployment with that run-specific image before
rollout readiness. MongoDB, Redis, Memcached, Jaeger, and the two OpenResty
frontends retain the chart dependency image tags. Some tags are floating, so
the source revision pin does not pin every container image digest. The media upload frontend is
excluded because ServiceBench exercises user registration, follows, post
composition, and user/home timeline reads through `nginx-thrift`; post
composition still exercises the media service itself.

The local kind manifest pins `nginx-thrift` to one worker and raises its memory
limit to 512 MiB. This matches its 100 millicore request and avoids worker and
memory oversubscription during fixture setup on a constrained local cluster.

Regenerate the manifest from the pinned source checkout with:

```shell
helm template social \
  deathstarbench/socialNetwork/helm-chart/socialnetwork \
  > resources/evaluators/microservice/kubernetes_examples/social-network/manifest.yaml
```

After regeneration, reapply the pinned asset-loader revision and the local
`nginx-thrift` worker and memory settings described above.

The expected kind cluster and context are `vibesys-k8s-social` and
`kind-vibesys-k8s-social`. The evaluator forwards `service/nginx-thrift` to
`127.0.0.1:18080`; ServiceBench performs its own seeded fixture preparation
through that gateway before measuring timeline operations.

The Kubernetes workload omits the optional Thrift timing-header captures from
the Compose task. The pinned upstream gateway does not emit those diagnostic
headers. HTTP status, response schema, fixture state, and read-after-write
validation remain required by the Social Network application adapter.
