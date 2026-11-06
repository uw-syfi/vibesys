package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/k8s"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/k8s/k8stest"
)

const k8sManifest = `apiVersion: v1
kind: Service
metadata: {name: web}
spec: {selector: {app: web}, ports: [{port: 80}]}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: web}
spec:
  replicas: 2
  selector: {matchLabels: {app: web}}
  template:
    metadata: {labels: {app: web}}
    spec: {containers: [{name: web, image: "${WEB_IMAGE}"}]}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: cache}
spec:
  replicas: 1
  selector: {matchLabels: {app: cache}}
  template:
    metadata: {labels: {app: cache}}
    spec: {containers: [{name: cache, image: "memcached:1.6"}]}
`

type k8sFixture struct {
	t        *testing.T
	kube     *k8stest.Kube
	taskDir  string
	workDir  string
	app      map[string]any
	probe    string
	artifact string
}

func newK8sFixture(t *testing.T) *k8sFixture {
	t.Helper()
	root := t.TempDir()
	f := &k8sFixture{t: t, taskDir: filepath.Join(root, "task"), workDir: filepath.Join(root, "work"), artifact: filepath.Join(root, "art")}
	for path, text := range map[string]string{
		filepath.Join(f.taskDir, "k8s.yaml"):                "",
		filepath.Join(f.taskDir, "Dockerfile"):              "FROM scratch\n",
		filepath.Join(f.taskDir, "vibesys.input.toml"):      "[benchmark]\ntimeout_seconds = 600\n",
		filepath.Join(root, "project", "web", "Dockerfile"): "FROM scratch\n",
		filepath.Join(root, "probe.binpb"):                  "plan",
	} {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(f.taskDir, "k8s.yaml"), []byte(k8sManifest), 0o644); err != nil {
		t.Fatal(err)
	}
	f.probe = filepath.Join(root, "probe.binpb")
	f.app = map[string]any{
		"name": "web", "manifest": "k8s.yaml", "images": map[string]string{"WEB_IMAGE": "web"},
		"gateway": "http://web:80", "restartable": []string{"web"}, "caches": map[string]string{"cache": "K8S_CACHE_KIND_MEMCACHED"},
	}
	clock := k8s.NewFakeClock()
	f.kube = k8stest.New(clock)
	f.kube.Programs["loadgen"] = func([]string, map[string][]byte) k8stest.Outcome { return k8stest.Outcome{} }
	oldConnect, oldEnv := k8sConnect, k8sEnv
	t.Cleanup(func() { k8sConnect, k8sEnv = oldConnect, oldEnv })
	k8sConnect = func(env k8s.Environment, log func(string)) k8s.Cluster {
		return k8s.Cluster{Env: env, Kube: f.kube, Builder: &k8stest.Builder{}, Clock: clock, Log: log, Token: func() string { return "t1" }}
	}
	vars := map[string]string{"KUBECONFIG": "/kc", "VIBESYS_K8S_REGISTRY": "reg:5000", "VIBESYS_K8S_BUILDER": "tcp://b:1"}
	k8sEnv = func(k string) string { return vars[k] }
	_ = f.projectRoot(root)
	return f
}

func (f *k8sFixture) projectRoot(root string) string { return filepath.Join(root, "project") }

// call runs one verb through the real entry point and returns the exit code and the stdout JSON.
func (f *k8sFixture) call(verb string, request map[string]any) (int, map[string]any) {
	f.t.Helper()
	body, _ := json.Marshal(request)
	var out, errOut bytes.Buffer
	code := run([]string{"k8s", verb}, bytes.NewReader(body), &out, &errOut)
	var reply map[string]any
	if err := json.Unmarshal(out.Bytes(), &reply); err != nil {
		f.t.Fatalf("k8s %s: stdout %q: %v (stderr %s)", verb, out.String(), err, errOut.String())
	}
	return code, reply
}

func (f *k8sFixture) must(verb string, request map[string]any) map[string]any {
	f.t.Helper()
	code, reply := f.call(verb, request)
	if code != exitOK {
		f.t.Fatalf("k8s %s exited %d: %v", verb, code, reply)
	}
	return reply
}

func TestK8sDeployFaultsAndTeardown(t *testing.T) {
	f := newK8sFixture(t)
	project := filepath.Join(filepath.Dir(f.taskDir), "project")
	reply := f.must("deploy", map[string]any{
		"app": f.app, "task_dir": f.taskDir, "project_root": project, "work_dir": f.workDir,
		"artifacts_dir": f.artifact, "probe_plan_file": f.probe,
	})
	target := reply["target"].(map[string]any)
	if target["namespace"] != "hk8s-web-t1" || target["uid"] == "" || !strings.HasPrefix(target["task_image"].(string), "reg:5000/vse/web/task:") {
		t.Fatalf("target %v", target)
	}
	if _, err := os.Stat(filepath.Join(f.artifact, "manifest.rendered.yaml")); err != nil {
		t.Fatal(err)
	}

	// restart one pod without waiting, then wait for readiness
	restarted := f.must("restart", map[string]any{"target": target, "deployments": []string{"web"}, "no_wait": true})
	replaced := restarted["replaced"].([]any)
	if len(replaced) != 1 {
		t.Fatalf("replaced %v", restarted)
	}
	uid := replaced[0].(map[string]any)["uid"]
	f.must("wait", map[string]any{"target": target, "deployments": []string{"web"}, "replaced_uids": []any{uid}})
	// restart every pod and wait in one call
	all := f.must("restart", map[string]any{"target": target, "deployments": []string{"web"}, "every_pod": true})
	if got := len(all["replaced"].([]any)); got != 2 {
		t.Fatalf("replaced %d", got)
	}

	f.kube.Execs["echo"] = func(pod string, argv []string) k8s.ExecResult {
		return k8s.ExecResult{Output: strings.Join(argv[1:], " ")}
	}
	exec := f.must("exec", map[string]any{"target": target, "deployment": "web", "all_pods": true, "argv": []string{"echo", "hello"}})
	results := exec["results"].([]any)
	if len(results) != 2 || results[0].(map[string]any)["output"] != "hello" {
		t.Fatalf("exec %v", exec)
	}
	f.must("flush", map[string]any{"target": target, "cache": "cache"})
	if got := f.kube.Flushed("hk8s-web-t1"); len(got) != 1 {
		t.Fatalf("flushed %v", got)
	}

	// a pod of the package with an input file, and its outputs
	input := filepath.Join(filepath.Dir(f.workDir), "in.bin")
	if err := os.WriteFile(input, []byte("abc"), 0o644); err != nil {
		t.Fatal(err)
	}
	f.kube.Programs["vse-phase"] = func(_ []string, inputs map[string][]byte) k8stest.Outcome {
		return k8stest.Outcome{Log: "ran\n", Outputs: map[string][]byte{"out.bin": append(inputs["in.bin"], '!')}, Secs: 20 * time.Second}
	}
	outDir := filepath.Join(filepath.Dir(f.workDir), "out")
	pod := f.must("run-pod", map[string]any{
		"target": target, "role": "K8S_ROLE_PHASE", "timeout_secs": 300, "outputs_dir": outDir, "pod_name": "vse-phase-1",
		"command": map[string]any{"argv": []string{"vse-phase"}, "input_files": map[string]string{"in.bin": input}},
	})
	if pod["pod_name"] != "vse-phase-1" || pod["log"] != "ran\n" || pod["exit_code"] != nil {
		t.Fatalf("pod %v", pod)
	}
	if data, _ := os.ReadFile(filepath.Join(outDir, "out.bin")); string(data) != "abc!" {
		t.Fatalf("output %q", data)
	}

	usage := f.must("usage", map[string]any{"target": target, "label": "phase-0"})
	if len(usage["cpu_seconds"].(map[string]any)) == 0 {
		t.Fatalf("usage %v", usage)
	}

	// a refused target is a domain error, not a crash
	code, bad := f.call("restart", map[string]any{"target": target, "deployments": []string{"nosuch"}})
	if code != exitDomain || bad["error_kind"] != "deploy" || !strings.Contains(bad["message"].(string), "not in k8s.App(restartable)") {
		t.Fatalf("%d %v", code, bad)
	}

	f.must("stop", map[string]any{"target": target})
	f.must("start", map[string]any{"target": target})
	f.must("teardown", map[string]any{"target": target})
	if got := f.kube.LiveNamespaces(); len(got) != 0 {
		t.Fatalf("live %v", got)
	}
}

func TestK8sBadRequestsAreDomainErrors(t *testing.T) {
	f := newK8sFixture(t)
	for name, c := range map[string]struct {
		verb    string
		request map[string]any
		kind    string
	}{
		"unknown field": {"restart", map[string]any{"nosuch": 1}, "invalid"},
		"no namespace":  {"restart", map[string]any{"deployments": []string{"web"}}, "invalid"},
		"no role":       {"run-pod", map[string]any{"target": map[string]any{"namespace": "x"}}, "invalid"},
		"bad app":       {"deploy", map[string]any{"app": map[string]any{"name": "Bad"}, "task_dir": f.taskDir, "work_dir": f.workDir}, "invalid"},
	} {
		t.Run(name, func(t *testing.T) {
			code, reply := f.call(c.verb, c.request)
			if code != exitDomain || reply["error_kind"] != c.kind {
				t.Fatalf("%d %v", code, reply)
			}
		})
	}
}

func TestK8sBuildFailureIsDomainErrorOfKindBuild(t *testing.T) {
	f := newK8sFixture(t)
	k8sConnect = func(env k8s.Environment, log func(string)) k8s.Cluster {
		return k8s.Cluster{Env: env, Kube: f.kube, Builder: &k8stest.Builder{Fail: []string{"reg:5000/vse/web/web:"}}, Clock: k8s.NewFakeClock(), Log: log}
	}
	code, reply := f.call("deploy", map[string]any{
		"app": f.app, "task_dir": f.taskDir, "project_root": filepath.Join(filepath.Dir(f.taskDir), "project"),
		"work_dir": f.workDir, "probe_plan_file": f.probe,
	})
	if code != exitDomain || reply["error_kind"] != "build" {
		t.Fatal(code, fmt.Sprint(reply))
	}
}
