package k8s_test

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/k8s"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/k8s/k8stest"
)

func workloadNames(kube *k8stest.Kube, namespace string) []string {
	var names []string
	for key := range kube.NamespaceObjects(namespace) {
		if key[0] == "Deployment" {
			names = append(names, key[1])
		}
	}
	slices.Sort(names)
	return names
}

func podsOf(t *testing.T, kube *k8stest.Kube, ns, app string) []k8s.Pod {
	t.Helper()
	pods, err := kube.Pods(context.Background(), ns, map[string]string{"app": app})
	if err != nil {
		t.Fatal(err)
	}
	return pods
}

func uids(pods []k8s.Pod) map[string]bool {
	set := map[string]bool{}
	for _, p := range pods {
		set[p.UID] = true
	}
	return set
}

func TestSeedRunsAfterTheDataTierServesAndBeforeTheApplicationExists(t *testing.T) {
	w := newWorld(t)
	var seenWorkloads []string
	var seenArgv []string
	seenInputs := map[string][]byte{}
	dbReady := false
	w.kube.Programs["python"] = func(argv []string, inputs map[string][]byte) k8stest.Outcome {
		seenArgv, seenInputs = argv, inputs
		seenWorkloads = workloadNames(w.kube, "hk8s-shop-t0")
		pods, _ := w.kube.Pods(context.Background(), "hk8s-shop-t0", map[string]string{"app": "db"})
		dbReady = len(pods) == 1 && pods[0].Ready
		return k8stest.Outcome{Secs: 30 * time.Second}
	}
	var probes [][]string
	w.kube.Programs["loadgen"] = func([]string, map[string][]byte) k8stest.Outcome {
		probes = append(probes, workloadNames(w.kube, "hk8s-shop-t0"))
		return k8stest.Outcome{}
	}
	d, err := w.up(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if d.Namespace != "hk8s-shop-t0" {
		t.Fatalf("namespace %q", d.Namespace)
	}
	if !slices.Equal(seenWorkloads, []string{"db"}) || !dbReady {
		t.Fatalf("seed saw workloads %v, db ready %v", seenWorkloads, dbReady)
	}
	if !slices.Equal(seenArgv, seedCommand.Argv) || string(seenInputs["dataset.pickle"]) != "data" {
		t.Fatalf("seed got %v %v", seenArgv, seenInputs)
	}
	// The data tier's probe does not exist: the one probe runs once the whole app is there.
	if len(probes) != 1 || !slices.Equal(probes[0], []string{"backend", "cache", "db", "frontend"}) {
		t.Fatalf("probes %v", probes)
	}
	if err := d.Close(context.Background(), k8s.CloseOptions{}); err != nil {
		t.Fatal(err)
	}
	if got := w.kube.AllNamespaces(); len(got) != 0 {
		t.Fatalf("namespaces left: %v", got)
	}
}

func TestWithoutADataTierTheSeedRunsOnceTheWholeAppServes(t *testing.T) {
	w := newWorld(t)
	var order []string
	w.kube.Programs["python"] = func([]string, map[string][]byte) k8stest.Outcome {
		order = append(order, "seed")
		return k8stest.Outcome{}
	}
	w.kube.Programs["loadgen"] = func([]string, map[string][]byte) k8stest.Outcome {
		order = append(order, "probe")
		return k8stest.Outcome{}
	}
	w.mustUp(func(a *k8s.App) { a.DataTier = "" })
	if !slices.Equal(order, []string{"probe", "seed"}) {
		t.Fatalf("order %v", order)
	}
}

func TestImagesAreBuiltBeforeTheNamespaceAndSubstitutedByDigest(t *testing.T) {
	w := newWorld(t)
	d := w.mustUp()
	digest, err := k8s.TreeDigest(filepath.Join(w.project, "app"))
	if err != nil {
		t.Fatal(err)
	}
	appImage := "registry:5000/vse/shop/app:" + digest
	var built []string
	for _, b := range w.builder.Builds {
		built = append(built, b.Image)
	}
	taskDigest, _ := k8s.TreeDigest(w.taskDir)
	if !slices.Contains(built, appImage) || !slices.Contains(built, d.TaskImage) {
		t.Fatalf("built %v", built)
	}
	if d.TaskImage != "registry:5000/vse/shop/task:"+taskDigest {
		t.Fatalf("task image %s", d.TaskImage)
	}
	frontend := w.kube.NamespaceObjects(d.Namespace)[[2]string{"Deployment", "frontend"}]
	containers := frontend["spec"].(map[string]any)["template"].(map[string]any)["spec"].(map[string]any)["containers"].([]any)
	if got := containers[0].(map[string]any)["image"]; got != appImage {
		t.Fatalf("frontend image %v", got)
	}
	if slices.Index(w.kube.Calls, "create_namespace") <= 0 {
		t.Fatal("namespace created before anything else")
	}
	for key, obj := range w.kube.NamespaceObjects(d.Namespace) {
		if key[0] != "Job" {
			continue
		}
		image := obj["spec"].(map[string]any)["template"].(map[string]any)["spec"].(map[string]any)["containers"].([]any)[0].(map[string]any)["image"]
		if image != d.TaskImage {
			t.Fatalf("job image %v", image)
		}
	}
}

func TestAChangedCandidateGetsANewTagAndGeneratedFilesDoNot(t *testing.T) {
	root := filepath.Join(t.TempDir(), "ctx")
	write(t, filepath.Join(root, "pkg", "a.py"), "x = 1\n")
	before, _ := k8s.TreeDigest(root)
	write(t, filepath.Join(root, "pkg", "__pycache__", "a.cpython-312.pyc"), "\x00")
	write(t, filepath.Join(root, "pkg", "b.pyc"), "\x00")
	if after, _ := k8s.TreeDigest(root); after != before {
		t.Fatal("generated files changed the digest")
	}
	write(t, filepath.Join(root, "pkg", "a.py"), "x = 2\n")
	if after, _ := k8s.TreeDigest(root); after == before {
		t.Fatal("a changed file kept the digest")
	}
}

// The digest is the Python implementation's: this value was computed by it for the same tree.
func TestTreeDigestMatchesThePythonImplementation(t *testing.T) {
	root := t.TempDir()
	write(t, filepath.Join(root, "Dockerfile"), "FROM scratch\n")
	write(t, filepath.Join(root, "src", "run.sh"), "#!/bin/sh\necho hi\n")
	if err := os.Chmod(filepath.Join(root, "src", "run.sh"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("src/run.sh", filepath.Join(root, "link")); err != nil {
		t.Fatal(err)
	}
	got, err := k8s.TreeDigest(root)
	if err != nil {
		t.Fatal(err)
	}
	if want := "d8bafcce66c5df3d"; got != want {
		t.Fatalf("digest %s, python gave %s", got, want)
	}
}

func TestABuildFailureCreatesNoNamespace(t *testing.T) {
	w := newWorld(t)
	w.builder.Fail = []string{"registry:5000/vse/shop/app:"}
	_, err := w.up(context.Background())
	contains(t, err, "build of registry:5000/vse/shop/app")
	if k8s.KindOf(err) != k8s.KindBuild {
		t.Fatalf("kind %s", k8s.KindOf(err))
	}
	if slices.Contains(w.kube.Calls, "create_namespace") || len(w.kube.AllNamespaces()) != 0 {
		t.Fatal("a namespace exists")
	}
}

func TestManifestErrorsComeBeforeAnyBuild(t *testing.T) {
	w := newWorld(t)
	_, err := w.up(context.Background(), func(a *k8s.App) { a.Restartable = []string{"frontend", "nosuch"} })
	contains(t, err, "restartable")
	if len(w.builder.Builds) != 0 {
		t.Fatal("built")
	}
}

func TestAnAppThatNeverServesFailsWithTheReasonAndIsDeleted(t *testing.T) {
	w := newWorld(t)
	w.kube.Broken["backend"] = "CrashLoopBackOff"
	_, err := w.up(context.Background())
	contains(t, err, "deployment/backend: 0/2 ready (backend-", "CrashLoopBackOff")
	if got := w.kube.AllNamespaces(); len(got) != 0 {
		t.Fatalf("left %v", got)
	}
	if _, err := os.Stat(filepath.Join(w.artifacts, "pods.json")); err != nil {
		t.Fatal(err)
	}
}

func TestAFailedSeedFailsTheDeployWithItsLog(t *testing.T) {
	w := newWorld(t)
	w.kube.Programs["python"] = func([]string, map[string][]byte) k8stest.Outcome {
		return k8stest.Outcome{ExitCode: 3, Log: "mongo refused\n"}
	}
	_, err := w.up(context.Background())
	contains(t, err, "seed failed (exit 3)", "mongo refused")
	if len(w.kube.AllNamespaces()) != 0 {
		t.Fatal("left")
	}
}

func TestAPhasePodGetsItsInputsAndReturnsItsOutputs(t *testing.T) {
	w := newWorld(t)
	w.kube.Programs["vse-phase"] = func(_ []string, inputs map[string][]byte) k8stest.Outcome {
		in := inputs["phase-input.binpb"]
		reversed := make([]byte, len(in))
		for i := range in {
			reversed[len(in)-1-i] = in[i]
		}
		return k8stest.Outcome{Secs: 95 * time.Second, Log: "phase done\n",
			Outputs: map[string][]byte{"phase-output.binpb": reversed, "results.binpb.gz": []byte("r")}}
	}
	d := w.mustUp()
	run, err := d.RunPod(context.Background(), k8s.RolePhase, k8s.PodCommand{
		Argv: []string{"vse-phase", "--work", k8s.WorkDir}, Inputs: map[string][]byte{"phase-input.binpb": []byte("abc")},
	}, 600*time.Second, k8s.RunOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if run.ExitCode != 0 || !slices.Equal(run.Files, []string{"phase-output.binpb", "results.binpb.gz"}) {
		t.Fatalf("run %+v", run)
	}
	if data, _ := os.ReadFile(filepath.Join(run.Outputs, "phase-output.binpb")); string(data) != "cba" {
		t.Fatalf("output %q", data)
	}
	if _, err := os.Stat(filepath.Join(run.Outputs, "phase-input.binpb")); err == nil {
		t.Fatal("the input came back")
	}
	if run.Seconds < 95 || !strings.Contains(run.Log, "phase done") {
		t.Fatalf("run %+v", run)
	}
	job := w.kube.NamespaceObjects(d.Namespace)[[2]string{"Job", run.Name}]
	resources := job["spec"].(map[string]any)["template"].(map[string]any)["spec"].(map[string]any)["containers"].([]any)[0].(map[string]any)["resources"].(map[string]any)
	for _, side := range []string{"limits", "requests"} {
		got := resources[side].(map[string]any)
		if got["cpu"] != "6" || got["memory"] != "4Gi" {
			t.Fatalf("%s %v", side, got)
		}
	}
}

func TestAPhasePodThatDiesRaisesWithItsLog(t *testing.T) {
	w := newWorld(t)
	w.kube.Programs["vse-phase"] = func([]string, map[string][]byte) k8stest.Outcome {
		return k8stest.Outcome{Crash: true, Log: "killed\n", Secs: 5 * time.Second}
	}
	d := w.mustUp()
	_, err := d.RunPod(context.Background(), k8s.RolePhase, k8s.PodCommand{Argv: []string{"vse-phase"}}, 600*time.Second, k8s.RunOptions{})
	contains(t, err, "Failed, exit 137", "killed")
}

func TestAPhasePodNonzeroExitIsReturned(t *testing.T) {
	w := newWorld(t)
	w.kube.Programs["vse-phase"] = func([]string, map[string][]byte) k8stest.Outcome { return k8stest.Outcome{ExitCode: 4} }
	d := w.mustUp()
	run, err := d.RunPod(context.Background(), k8s.RolePhase, k8s.PodCommand{Argv: []string{"vse-phase"}}, 60*time.Second, k8s.RunOptions{})
	if err != nil || run.ExitCode != 4 {
		t.Fatalf("%+v %v", run, err)
	}
}

func TestAnOOMKilledPodReturnsOutOfMemoryWithItsLimit(t *testing.T) {
	w := newWorld(t)
	w.kube.Programs["vse-phase"] = func([]string, map[string][]byte) k8stest.Outcome {
		return k8stest.Outcome{Crash: true, CrashReason: "OOMKilled"}
	}
	d := w.mustUp()
	_, err := d.RunPod(context.Background(), k8s.RolePhase, k8s.PodCommand{Argv: []string{"vse-phase"}}, 600*time.Second, k8s.RunOptions{})
	var oom *k8s.OutOfMemoryError
	if !errors.As(err, &oom) || oom.Limit != "4Gi" {
		t.Fatalf("error %v", err)
	}
	contains(t, err, "running out of memory (OOMKilled at its 4Gi limit, exit 137)")
	if k8s.KindOf(err) != k8s.KindOutOfMemory {
		t.Fatalf("kind %s", k8s.KindOf(err))
	}
}

func TestRecordedOOMKilledPodStatusParsesWithItsReason(t *testing.T) {
	pod, err := k8s.ParsePod([]byte(`{
  "metadata": {"name": "vse-phase-4-xswhx", "uid": "u1", "labels": {"vibesys.dev/evaluator-pod": "phase"}},
  "spec": {"nodeName": "node-1"},
  "status": {"phase": "Failed", "containerStatuses": [
    {"name": "main", "restartCount": 0, "state": {"terminated": {"exitCode": 137, "reason": "OOMKilled"}}}]}}`))
	if err != nil {
		t.Fatal(err)
	}
	if pod.Phase != "Failed" || pod.ExitCode == nil || *pod.ExitCode != 137 || pod.Reason != "OOMKilled" {
		t.Fatalf("%+v", pod)
	}
}

func TestRestartReplacesOnePodOrEveryPodAndWaits(t *testing.T) {
	w := newWorld(t)
	d := w.mustUp()
	ctx := context.Background()
	ns := d.Namespace
	before := uids(podsOf(t, w.kube, ns, "frontend"))
	replaced, err := d.Restart(ctx, []string{"frontend"}, k8s.RestartOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if len(replaced) != 1 || replaced[0].Deployment != "frontend" || !before[replaced[0].UID] {
		t.Fatalf("replaced %+v", replaced)
	}
	after := podsOf(t, w.kube, ns, "frontend")
	kept := 0
	for _, p := range after {
		if !p.Ready {
			t.Fatalf("%s not ready", p.Name)
		}
		if before[p.UID] {
			kept++
		}
	}
	if len(after) != 2 || kept != 1 {
		t.Fatalf("after %d kept %d", len(after), kept)
	}
	backends := uids(podsOf(t, w.kube, ns, "backend"))
	replaced, err = d.Restart(ctx, []string{"backend"}, k8s.RestartOptions{EveryPod: true})
	if err != nil || len(replaced) != 2 {
		t.Fatalf("%v %v", replaced, err)
	}
	for _, p := range podsOf(t, w.kube, ns, "backend") {
		if backends[p.UID] {
			t.Fatal("an old backend pod survived")
		}
	}
	_, err = d.Restart(ctx, []string{"db"}, k8s.RestartOptions{})
	contains(t, err, "not in k8s.App(restartable)")
}

func TestRestartWithoutWaitThenWaitReady(t *testing.T) {
	w := newWorld(t)
	d := w.mustUp()
	ctx := context.Background()
	replaced, err := d.Restart(ctx, []string{"frontend"}, k8s.RestartOptions{EveryPod: true, NoWait: true})
	if err != nil || len(replaced) != 2 {
		t.Fatal(replaced, err)
	}
	pods := podsOf(t, w.kube, d.Namespace, "frontend")
	for _, p := range pods {
		if p.Ready && !p.Deleting && !uids2(replaced)[p.UID] {
			t.Fatal("a fresh pod is ready at once")
		}
	}
	probes := 0
	w.kube.Programs["loadgen"] = func([]string, map[string][]byte) k8stest.Outcome { probes++; return k8stest.Outcome{} }
	if err := d.WaitReady(ctx, k8s.WaitOptions{Deployments: []string{"frontend"}, Replaced: uidList(replaced)}); err != nil {
		t.Fatal(err)
	}
	if probes != 1 {
		t.Fatalf("probes %d", probes)
	}
	for _, p := range podsOf(t, w.kube, d.Namespace, "frontend") {
		if !p.Ready || uids2(replaced)[p.UID] {
			t.Fatalf("%+v", p)
		}
	}
	if err := d.WaitReady(ctx, k8s.WaitOptions{Deployments: []string{"nosuch"}, Timeout: 10 * time.Second, NoProbe: true}); err == nil {
		t.Fatal("waited for a Deployment that does not exist")
	}
}

func uids2(replaced []k8s.ReplacedPod) map[string]bool {
	set := map[string]bool{}
	for _, r := range replaced {
		set[r.UID] = true
	}
	return set
}

func uidList(replaced []k8s.ReplacedPod) []string {
	var l []string
	for _, r := range replaced {
		l = append(l, r.UID)
	}
	return l
}

func TestAPodThatNeverBecomesReadyAfterARestartFails(t *testing.T) {
	w := newWorld(t)
	d := w.mustUp()
	w.kube.Broken["frontend"] = "CrashLoopBackOff"
	_, err := d.Restart(context.Background(), []string{"frontend"}, k8s.RestartOptions{})
	contains(t, err, "not serving after 300s", "CrashLoopBackOff")
}

func TestTheAppsWaitBoundsTheDeployAndARestart(t *testing.T) {
	w := newWorld(t)
	w.kube.Broken["backend"] = "ContainerCreating"
	began := w.clock.Now()
	_, err := w.up(context.Background(), func(a *k8s.App) { a.WaitSecs = 900 })
	contains(t, err, "not serving after 900s")
	if waited := w.clock.Now().Sub(began); waited < 900*time.Second {
		t.Fatalf("gave up after %v", waited)
	}
	w = newWorld(t)
	d := w.mustUp(func(a *k8s.App) { a.WaitSecs = 45 })
	w.kube.Broken["frontend"] = "CrashLoopBackOff"
	_, err = d.Restart(context.Background(), []string{"frontend"}, k8s.RestartOptions{})
	contains(t, err, "not serving after 45s")
	if err := (k8s.App{Name: "a", Manifest: "m.yaml", Gateway: "http://a:1", WaitSecs: -1}).Validate(); err == nil || !strings.Contains(err.Error(), "wait_secs") {
		t.Fatalf("got %v", err)
	}
}

func TestExecRunsInOnePodOrEveryPodOfAnAllowedDeployment(t *testing.T) {
	w := newWorld(t)
	w.kube.Execs["echo"] = func(pod string, argv []string) k8s.ExecResult {
		return k8s.ExecResult{Output: pod + ":" + strings.Join(argv[1:], " ") + "\n"}
	}
	w.kube.Execs["false"] = func(string, []string) k8s.ExecResult { return k8s.ExecResult{ExitCode: 1} }
	d := w.mustUp()
	ctx := context.Background()
	one, err := d.Exec(ctx, k8s.ExecTarget{Deployment: "frontend"}, []string{"echo", "hi"}, 0)
	if err != nil || len(one) != 1 || !strings.HasPrefix(one[0].Output, one[0].Pod+":hi") {
		t.Fatalf("%+v %v", one, err)
	}
	all, err := d.Exec(ctx, k8s.ExecTarget{Deployment: "frontend", AllPods: true}, []string{"echo", "hi"}, 0)
	if err != nil || len(all) != 2 || all[0].Pod == all[1].Pod {
		t.Fatalf("%+v %v", all, err)
	}
	byPod, err := d.Exec(ctx, k8s.ExecTarget{Pod: all[1].Pod}, []string{"echo", "x"}, 0)
	if err != nil || len(byPod) != 1 || byPod[0].Pod != all[1].Pod {
		t.Fatalf("%+v %v", byPod, err)
	}
	failed, err := d.Exec(ctx, k8s.ExecTarget{Deployment: "cache"}, []string{"false"}, 0)
	if err != nil || failed[0].ExitCode != 1 {
		t.Fatalf("a nonzero exit is a result: %+v %v", failed, err)
	}
	_, err = d.Exec(ctx, k8s.ExecTarget{Deployment: "db"}, []string{"echo"}, 0)
	contains(t, err, "not in k8s.App(restartable) or caches")
	dbPod := podsOf(t, w.kube, d.Namespace, "db")[0].Name
	_, err = d.Exec(ctx, k8s.ExecTarget{Pod: dbPod}, []string{"echo"}, 0)
	contains(t, err, "not a pod of a Deployment")
	_, err = d.Exec(ctx, k8s.ExecTarget{Pod: "x", Deployment: "frontend"}, []string{"echo"}, 0)
	contains(t, err, "not both")
}

func TestFlushEmptiesEveryPodOfACacheOnly(t *testing.T) {
	w := newWorld(t)
	d := w.mustUp()
	ctx := context.Background()
	if err := d.Flush(ctx, "cache"); err != nil {
		t.Fatal(err)
	}
	flushed := w.kube.Flushed(d.Namespace)
	if len(flushed) != 1 || !strings.HasPrefix(flushed[0], "cache-") {
		t.Fatalf("%v", flushed)
	}
	contains(t, d.Flush(ctx, "frontend"), "not in k8s.App(caches)")
}

func TestStopAndStartScaleTheRestartableDeployments(t *testing.T) {
	w := newWorld(t)
	d := w.mustUp()
	ctx := context.Background()
	if err := d.Stop(ctx); err != nil {
		t.Fatal(err)
	}
	if len(podsOf(t, w.kube, d.Namespace, "frontend")) != 0 || len(podsOf(t, w.kube, d.Namespace, "db")) != 1 {
		t.Fatal("stop")
	}
	if err := d.Start(ctx); err != nil {
		t.Fatal(err)
	}
	ready := 0
	for _, p := range podsOf(t, w.kube, d.Namespace, "frontend") {
		if p.Ready {
			ready++
		}
	}
	if ready != 2 {
		t.Fatalf("ready %d", ready)
	}
}

func TestAttachActsOnANamespaceItDoesNotOwn(t *testing.T) {
	w := newWorld(t)
	d := w.mustUp()
	ctx := context.Background()
	other, err := k8s.Attach(ctx, w.source(makeApp()), d.Namespace, w.cluster())
	if err != nil {
		t.Fatal(err)
	}
	if other.TaskImage != d.TaskImage {
		t.Fatalf("%s != %s", other.TaskImage, d.TaskImage)
	}
	if _, err := other.Restart(ctx, []string{"frontend"}, k8s.RestartOptions{}); err != nil {
		t.Fatal(err)
	}
	if err := other.Close(ctx, k8s.CloseOptions{}); err != nil {
		t.Fatal(err)
	}
	if got := w.kube.LiveNamespaces(); !slices.Equal(got, []string{d.Namespace}) {
		t.Fatalf("%v", got)
	}
	_, err = k8s.Attach(ctx, w.source(makeApp()), "hk8s-other-1", w.cluster())
	contains(t, err, "not one of shop's")
}

func TestUsageReportsCPUPerPodBetweenSnapshots(t *testing.T) {
	w := newWorld(t)
	d := w.mustUp()
	ctx := context.Background()
	first, err := d.Usage(ctx, "closed")
	if err != nil {
		t.Fatal(err)
	}
	_ = w.clock.Sleep(ctx, 10*time.Second)
	second, err := d.Usage(ctx, "open")
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range podsOf(t, w.kube, d.Namespace, "frontend") {
		if delta := second[p.Name]; delta < 4.99 || delta > 5.01 {
			t.Fatalf("%s: %v", p.Name, delta)
		}
		if first[p.Name] <= 0 {
			t.Fatalf("%s: %v", p.Name, first[p.Name])
		}
	}
	if _, err := os.Stat(filepath.Join(w.artifacts, "pod-usage-open.json")); err != nil {
		t.Fatal(err)
	}
}

func fullRunCalls(t *testing.T) int {
	w := newWorld(t)
	d := w.mustUp()
	if err := d.Close(context.Background(), k8s.CloseOptions{}); err != nil {
		t.Fatal(err)
	}
	return w.kube.NumCalls()
}

// Interrupt the deploy-and-teardown at any API call: the namespace is always deleted. After a
// signal the deletion is only requested (no wait, within the grace period).
func TestAnExitAtAnyCallLeavesNoNamespace(t *testing.T) {
	total := fullRunCalls(t)
	for _, how := range []string{"signal", "failure"} {
		for call := 1; call <= total; call++ {
			t.Run(fmt.Sprintf("%s-%d", how, call), func(t *testing.T) {
				w := newWorld(t)
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				if how == "signal" {
					w.kube.AtCall(call, cancel)
				} else {
					w.kube.FailAt(call, errors.New("the API server went away"))
				}
				d, err := w.up(ctx)
				if err == nil {
					// The interruption came during the teardown, after the deploy.
					_ = d.Close(ctx, k8s.CloseOptions{Fast: ctx.Err() != nil})
				}
				if got := w.kube.LiveNamespaces(); len(got) != 0 {
					t.Fatalf("live namespaces %v after %v", got, err)
				}
				if how == "signal" && w.kube.NumCalls()-call > 2 {
					t.Fatalf("%d calls after the signal", w.kube.NumCalls()-call)
				}
				_ = w.clock.Sleep(context.Background(), 5*time.Second)
				if got := w.kube.AllNamespaces(); len(got) != 0 {
					t.Fatalf("namespaces %v", got)
				}
			})
		}
	}
}

func TestCancellationIsReportedAsCanceled(t *testing.T) {
	w := newWorld(t)
	ctx, cancel := context.WithCancel(context.Background())
	w.kube.AtCall(5, cancel)
	_, err := w.up(ctx)
	if !errors.Is(err, context.Canceled) || k8s.KindOf(err) != k8s.KindCanceled {
		t.Fatalf("%v", err)
	}
}
