# Kubernetes evaluator runtime

`kubernetes-runtime` runs an evaluator command while it owns a fresh Kubernetes
namespace. It validates namespaced manifests, builds candidate images, waits for
rollouts and HTTP probes, exposes services through foreground port forwards, and
deletes only the namespace whose UID and ownership token it created.

```shell
kubernetes-runtime \
  --config runtime.yaml \
  --candidate-dir /workspace/candidate \
  -- evaluator --base-url '${BASE_URL}' --users '${ENDPOINT:users}'
```

The YAML config declares an explicit `context`, optional `kubeconfig`, a
`namespace_prefix`, `manifests`, `rollouts`, named `forwards`, a
`primary_endpoint`, and `http_probes`. Manifests resolve relative to the config
file, which keeps evaluator deployment policy outside the candidate. A path
starting with `${CANDIDATE_DIR}/` explicitly opts into a candidate manifest.
Only the built-in namespaced kind allowlist is accepted.

Optional `image_builds` run `docker build` from candidate-relative contexts. A
`kind_cluster` loads each image into kind. `image_overrides` may reference a
built image as `${IMAGE:name}`. Use `${NAMESPACE}` in image tags and manifest
scalar values to isolate runs. A config using only prebuilt application images
checks deployment behavior but does not evaluate candidate source changes.

Automatic candidate-image loading is supported for kind. For other clusters,
make images available to the cluster separately; this adapter does not push
images to a registry. The examples use VibeSys's local run environment, with
host Docker and kubeconfig access. Container/Modal evaluation and Kubernetes
profiling are not supported by these examples.

`restart_deployments` records each Deployment's replica count, scales it to
zero, waits for pods matching its explicit selector to disappear, restores the
replicas, recreates port forwards, and reruns readiness probes. Multiple named
forwards support evaluators that address services directly.

`timeout_seconds` bounds each individual kubectl, image-build, image-load, and
readiness operation. It is not a budget for the complete lifecycle.

An HTTP probe can include `json_contains`. Objects match by recursively checking
the declared keys, arrays match when every declared item appears in any order,
and scalar values match exactly. This lets a lifecycle wait for initialized
application state instead of only an open listener.

The process running this adapter must have `kubectl`, Docker when builds are
declared, and `kind` when image loading is declared. It also needs credentials
for the exact configured context. Apart from the namespace it owns, the adapter
does not create cluster-scoped objects itself, and it refuses to delete a
namespace if its UID or ownership label changed.

Limits: the manifest kind allowlist (Deployment, Service, ConfigMap) does not
constrain what a pod spec may request (for example privileged containers or host
mounts), so manifests must come from trusted task files, not a sandbox. The
control socket is a private Unix socket with no authentication and its path is
exported to the evaluator command, so any process in that command can request
stop, start, or cleanup. If the harness hard-kills this process (SIGKILL), the
owned namespace is not deleted and nothing sweeps leaked namespaces later; delete
namespaces with the `vibesys.dev/evaluator-owned` label manually.
