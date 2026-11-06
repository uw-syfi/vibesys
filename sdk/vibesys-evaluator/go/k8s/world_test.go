package k8s_test

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/k8s"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/k8s/k8stest"
)

// manifest is a small task: a data tier (db), two app Deployments, a cache, a gateway.
const manifest = `apiVersion: v1
kind: ConfigMap
metadata:
  name: config
data:
  db: db.${NAMESPACE}.svc.cluster.local:27017
---
apiVersion: v1
kind: Service
metadata: {name: db}
spec: {selector: {app: db}, ports: [{port: 27017}]}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: db, labels: {tier: data}}
spec:
  replicas: 1
  selector: {matchLabels: {app: db}}
  template:
    metadata: {labels: {app: db}}
    spec: {containers: [{name: db, image: mongo:4.4}]}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: cache, labels: {tier: app}}
spec:
  replicas: 1
  selector: {matchLabels: {app: cache}}
  template:
    metadata: {labels: {app: cache}}
    spec: {containers: [{name: cache, image: memcached:1.6}]}
---
apiVersion: v1
kind: Service
metadata: {name: frontend, namespace: "${NAMESPACE}"}
spec: {selector: {app: frontend}, ports: [{port: 5000}]}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: frontend, labels: {tier: app}}
spec:
  replicas: 2
  selector: {matchLabels: {app: frontend}}
  template:
    metadata: {labels: {app: frontend}}
    spec: {containers: [{name: frontend, image: "${APP_IMAGE}"}]}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: backend, labels: {tier: app}}
spec:
  replicas: 2
  selector: {matchLabels: {app: backend}}
  template:
    metadata: {labels: {app: backend}}
    spec: {containers: [{name: backend, image: "${APP_IMAGE}"}]}
`

var (
	seedCommand = k8s.PodCommand{
		Argv:   []string{"python", "/task/bench.py", "pod", "seed"},
		Inputs: map[string][]byte{"dataset.pickle": []byte("data")},
	}
	probePlan = []byte("probe-plan")
)

func makeApp(mutate ...func(*k8s.App)) k8s.App {
	app := k8s.App{
		Name: "shop", Manifest: "k8s/manifest.yaml", Images: map[string]string{"APP_IMAGE": "app"},
		Gateway: "http://frontend:5000", DataTier: "tier=data", HasSeed: true,
		Restartable: []string{"frontend", "backend"}, Caches: map[string]k8s.CacheKind{"cache": k8s.Memcached},
	}
	for _, m := range mutate {
		m(&app)
	}
	return app
}

// world is the fake cluster, builder and clock, a task directory and a project root.
type world struct {
	t         *testing.T
	kube      *k8stest.Kube
	builder   *k8stest.Builder
	clock     *k8s.FakeClock
	env       k8s.Environment
	taskDir   string
	project   string
	artifacts string
	lines     []string
}

func (w *world) log(line string) { w.lines = append(w.lines, line) }

func (w *world) cluster() k8s.Cluster {
	return k8s.Cluster{Env: w.env, Kube: w.kube, Builder: w.builder, Clock: w.clock, Log: w.log, Token: func() string { return "t0" }}
}

func (w *world) source(app k8s.App) k8s.AppSource {
	return k8s.AppSource{App: app, TaskDir: w.taskDir, ProjectRoot: w.project, ProbePlan: probePlan}
}

func (w *world) up(ctx context.Context, mutate ...func(*k8s.App)) (*k8s.Deployment, error) {
	app := makeApp(mutate...)
	var seed *k8s.PodCommand
	if app.HasSeed {
		seed = &seedCommand
	}
	return k8s.Deploy(ctx, w.source(app), seed, w.cluster())
}

func (w *world) mustUp(mutate ...func(*k8s.App)) *k8s.Deployment {
	w.t.Helper()
	d, err := w.up(context.Background(), mutate...)
	if err != nil {
		w.t.Fatalf("deploy: %v", err)
	}
	w.t.Cleanup(func() { _ = d.Close(context.Background(), k8s.CloseOptions{}) })
	return d
}

func write(t *testing.T, path, text string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
}

func newWorld(t *testing.T) *world {
	t.Helper()
	root := t.TempDir()
	clock := k8s.NewFakeClock()
	kube := k8stest.New(clock)
	w := &world{t: t, kube: kube, builder: &k8stest.Builder{}, clock: clock,
		taskDir: filepath.Join(root, "task"), project: filepath.Join(root, "project"),
		artifacts: filepath.Join(root, "artifacts", "job-7", "benchmark-0")}
	write(t, filepath.Join(w.taskDir, "k8s", "manifest.yaml"), manifest)
	write(t, filepath.Join(w.taskDir, "Dockerfile"), "FROM vibesys-evaluator\nCOPY . /task\n")
	write(t, filepath.Join(w.project, "app", "Dockerfile"), "FROM scratch\n")
	if err := os.MkdirAll(w.artifacts, 0o755); err != nil {
		t.Fatal(err)
	}
	env := map[string]string{
		"KUBECONFIG": "/vibesys/kubeconfig", "VIBESYS_K8S_REGISTRY": "registry:5000",
		"VIBESYS_K8S_BUILDER": "tcp://builder:1234", "VIBESYS_JOB_TIMEOUT_SECONDS": "3600",
	}
	var err error
	w.env, err = k8s.ParseEnvironment(func(k string) string { return env[k] }, filepath.Join(root, "work"), w.artifacts)
	if err != nil {
		t.Fatal(err)
	}
	kube.Programs["loadgen"] = func([]string, map[string][]byte) k8stest.Outcome { return k8stest.Outcome{Log: "probe ok\n"} }
	kube.Programs["python"] = func([]string, map[string][]byte) k8stest.Outcome {
		return k8stest.Outcome{Log: "seeded\n", Secs: 20 * time.Second}
	}
	return w
}

func contains(t *testing.T, err error, parts ...string) {
	t.Helper()
	if err == nil {
		t.Fatalf("want an error containing %q, got none", parts)
	}
	for _, p := range parts {
		if !strings.Contains(err.Error(), p) {
			t.Fatalf("error %q does not contain %q", err, p)
		}
	}
}
