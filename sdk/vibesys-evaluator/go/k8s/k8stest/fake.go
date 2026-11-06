// Package k8stest is an in-memory cluster and image builder for testing code that uses package k8s
// without a cluster.
//
// The fake is as strict as a cluster: namespaces have UIDs and refuse a deletion whose
// precondition names another, objects go only into a live namespace, controllers keep each
// workload's replica count of pods, pods take time (on the injected FakeClock) to start and become
// Ready, a deleted pod lingers for its grace period, and exec, file copies and logs need a running
// pod. A pod of the package runs the wrapper protocol of package k8s: its command, looked up in
// Programs by argv[0], runs once the ready marker arrives, and its exit marker and outputs appear
// when it is done.
package k8stest

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/k8s"
)

// Outcome is what a fake pod's command does: its exit status, the files it leaves in WorkDir, its
// log, and how long it runs (Secs; zero means one second). Crash makes the container die instead
// (no exit marker, pod Failed, exit 137), with the termination reason CrashReason (OOMKilled when
// the kernel killed it at its memory limit).
type Outcome struct {
	ExitCode    int
	Outputs     map[string][]byte
	Log         string
	Secs        time.Duration
	Crash       bool
	CrashReason string
}

// Program is a fake command: it gets argv and the files of WorkDir when it starts.
type Program func(argv []string, inputs map[string][]byte) Outcome

// ExecProgram answers a command run in a pod with exec, other than the package's own.
type ExecProgram func(pod string, argv []string) k8s.ExecResult

type pod struct {
	name, uid  string
	labels     map[string]string
	created    time.Time
	startSecs  time.Duration
	owner      string
	argv       []string
	files      map[string][]byte
	deletedAt  *time.Time
	grace      time.Duration
	log        string
	doneAt     *time.Time
	outcome    *Outcome
	finished   bool
	neverReady string
	node       string
}

type workload struct {
	kind           string
	name           string
	labels         map[string]string
	selector       map[string]string
	templateLabels map[string]string
	replicas       int
	generation     int
}

type namespace struct {
	name, uid string
	labels    map[string]string
	created   time.Time
	deletedAt *time.Time
	objects   map[[2]string]k8s.Object
	workloads map[string]*workload
	pods      map[string]*pod
	jobs      map[string]bool
	flushed   []string
}

// Kube is an in-memory k8s.KubeAPI on a FakeClock.
//
// Tests read Calls, NamespaceObjects and Flushed, set StartSecs and Broken per workload, Programs
// per command and Execs per exec'd command, and schedule cluster events with AtCall.
type Kube struct {
	Clock *k8s.FakeClock
	// Calls are the API calls so far, by method name, in order.
	Calls []string
	// Programs run the package's pods, by argv[0].
	Programs map[string]Program
	// Execs answer exec'd commands, by argv[0].
	Execs map[string]ExecProgram
	// StartSecs is how long the pods of a workload (or Job) take to start; 3 s when absent.
	StartSecs map[string]time.Duration
	// Broken maps a workload to the waiting reason of its pods, which then never become Ready.
	Broken map[string]string

	// NamespaceDeleteSecs is how long a deleted namespace lingers.
	NamespaceDelete time.Duration

	mu         sync.Mutex
	namespaces map[string]*namespace
	serial     int
	actions    map[int]func()
}

var _ k8s.KubeAPI = (*Kube)(nil)

// New is an empty cluster on the clock.
func New(clock *k8s.FakeClock) *Kube {
	return &Kube{
		Clock: clock, Programs: map[string]Program{}, Execs: map[string]ExecProgram{},
		StartSecs: map[string]time.Duration{}, Broken: map[string]string{},
		NamespaceDelete: 5 * time.Second, namespaces: map[string]*namespace{}, actions: map[int]func(){},
	}
}

// -- test controls ------------------------------------------------------------------------------

// AtCall runs action when the call-th API call (1-based) starts, before it takes effect. It may
// change the cluster (another job's doing) or cancel a context (a signal).
func (k *Kube) AtCall(call int, action func()) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.actions[call] = action
}

// FailAt makes the call-th API call fail with err.
func (k *Kube) FailAt(call int, err error) { k.failAt(call, err) }

func (k *Kube) failAt(call int, err error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.actions[-call] = func() { panic(failure{err}) }
}

type failure struct{ err error }

// AddNamespace adds a namespace some other job made (for sweep tests).
func (k *Kube) AddNamespace(name string, labels map[string]string, created time.Time) k8s.Namespace {
	k.mu.Lock()
	defer k.mu.Unlock()
	return k.addNamespace(name, labels, created)
}

func (k *Kube) addNamespace(name string, labels map[string]string, created time.Time) k8s.Namespace {
	ns := &namespace{
		name: name, uid: k.uid(), labels: copyMap(labels), created: created,
		objects: map[[2]string]k8s.Object{}, workloads: map[string]*workload{}, pods: map[string]*pod{}, jobs: map[string]bool{},
	}
	k.namespaces[name] = ns
	return k.view(ns)
}

// Recreate deletes and recreates a namespace at once with the same labels: a new UID.
func (k *Kube) Recreate(name string) k8s.Namespace {
	k.mu.Lock()
	defer k.mu.Unlock()
	return k.addNamespace(name, k.namespaces[name].labels, k.Clock.Now())
}

// LiveNamespaces are the namespaces not being deleted, sorted.
func (k *Kube) LiveNamespaces() []string {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.purge()
	var names []string
	for name, ns := range k.namespaces {
		if ns.deletedAt == nil {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	return names
}

// AllNamespaces are the namespaces including those still terminating, sorted.
func (k *Kube) AllNamespaces() []string {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.purge()
	names := make([]string, 0, len(k.namespaces))
	for name := range k.namespaces {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// NamespaceObjects are the objects applied into a namespace, by {kind, name}.
func (k *Kube) NamespaceObjects(name string) map[[2]string]k8s.Object {
	k.mu.Lock()
	defer k.mu.Unlock()
	out := map[[2]string]k8s.Object{}
	for key, obj := range k.namespaces[name].objects {
		out[key] = deepCopy(obj)
	}
	return out
}

// Flushed are the pods of a namespace whose cache a flush command emptied.
func (k *Kube) Flushed(name string) []string {
	k.mu.Lock()
	defer k.mu.Unlock()
	return append([]string(nil), k.namespaces[name].flushed...)
}

// NumCalls is the number of API calls so far.
func (k *Kube) NumCalls() int {
	k.mu.Lock()
	defer k.mu.Unlock()
	return len(k.Calls)
}

// -- internals ----------------------------------------------------------------------------------

func copyMap(m map[string]string) map[string]string {
	out := make(map[string]string, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}

func deepCopy(obj k8s.Object) k8s.Object {
	data, _ := json.Marshal(obj)
	var out k8s.Object
	_ = json.Unmarshal(data, &out)
	return out
}

func (k *Kube) uid() string {
	k.serial++
	return fmt.Sprintf("uid-%04d", k.serial)
}

// call records an API call, runs what was scheduled for it and returns the context's error.
func (k *Kube) call(ctx context.Context, name string) (err error) {
	k.Calls = append(k.Calls, name)
	n := len(k.Calls)
	defer func() {
		if r := recover(); r != nil {
			f, ok := r.(failure)
			if !ok {
				panic(r)
			}
			err = f.err
		}
	}()
	if action := k.actions[n]; action != nil {
		delete(k.actions, n)
		k.mu.Unlock() // the action may use the fake's own methods
		func() {
			defer k.mu.Lock()
			action()
		}()
	}
	if action := k.actions[-n]; action != nil {
		delete(k.actions, -n)
		action()
	}
	return ctx.Err()
}

func (k *Kube) purge() {
	now := k.Clock.Now()
	for name, ns := range k.namespaces {
		if ns.deletedAt != nil && !now.Before(ns.deletedAt.Add(k.NamespaceDelete)) {
			delete(k.namespaces, name)
		}
	}
	for _, ns := range k.namespaces {
		k.reconcile(ns)
	}
}

func (k *Kube) view(ns *namespace) k8s.Namespace {
	return k8s.Namespace{Name: ns.name, UID: ns.uid, Labels: copyMap(ns.labels), Created: ns.created, Deleting: ns.deletedAt != nil}
}

func (k *Kube) live(name string) (*namespace, error) {
	k.purge()
	ns := k.namespaces[name]
	if ns == nil {
		return nil, k8s.NotFound("Error from server (NotFound): namespaces %q not found", name)
	}
	return ns, nil
}

func (k *Kube) writable(name string) (*namespace, error) {
	ns, err := k.live(name)
	if err != nil {
		return nil, err
	}
	if ns.deletedAt != nil {
		return nil, &k8s.KubeError{Message: fmt.Sprintf("namespace %s is being terminated", name)}
	}
	return ns, nil
}

func (k *Kube) newPod(ns *namespace, name string, labels map[string]string, owner string, argv []string) *pod {
	start, ok := k.StartSecs[owner]
	if !ok {
		start = 3 * time.Second
	}
	p := &pod{
		name: name, uid: k.uid(), labels: copyMap(labels), created: k.Clock.Now(), startSecs: start,
		owner: owner, argv: argv, files: map[string][]byte{}, neverReady: k.Broken[owner], node: "node-a",
	}
	ns.pods[name] = p
	return p
}

func (k *Kube) reconcile(ns *namespace) {
	now := k.Clock.Now()
	for name, p := range ns.pods {
		if p.deletedAt != nil && !now.Before(p.deletedAt.Add(p.grace)) {
			delete(ns.pods, name)
		}
	}
	names := make([]string, 0, len(ns.workloads))
	for n := range ns.workloads {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, wn := range names {
		w := ns.workloads[wn]
		var live []*pod
		for _, p := range ns.pods {
			if p.owner == w.name && p.deletedAt == nil {
				live = append(live, p)
			}
		}
		sort.Slice(live, func(i, j int) bool { return live[i].name < live[j].name })
		if len(live) > w.replicas {
			for _, p := range live[w.replicas:] {
				t := now
				p.deletedAt, p.grace = &t, time.Second
			}
		}
		for i := len(live); i < w.replicas; i++ {
			k.newPod(ns, fmt.Sprintf("%s-%s", w.name, k.uid()[4:]), w.templateLabels, w.name, nil)
		}
	}
	for _, p := range ns.pods {
		if p.outcome != nil && p.doneAt != nil && !now.Before(*p.doneAt) && p.outcome.Crash {
			p.finished = true
		}
	}
}

func (k *Kube) running(p *pod) bool {
	return p.deletedAt == nil && !p.finished && p.neverReady == "" && !k.Clock.Now().Before(p.created.Add(p.startSecs))
}

func (k *Kube) podView(p *pod) k8s.Pod {
	started := !k.Clock.Now().Before(p.created.Add(p.startSecs))
	var exitCode *int
	reason := ""
	var phase string
	switch {
	case p.finished:
		phase = "Succeeded"
		if p.outcome == nil || p.outcome.Crash || p.outcome.ExitCode != 0 {
			phase = "Failed"
		}
		code := 137
		if p.outcome != nil && !p.outcome.Crash {
			code = p.outcome.ExitCode
		}
		exitCode = &code
		if p.outcome != nil && p.outcome.Crash {
			reason = p.outcome.CrashReason
			if reason == "" {
				reason = "Error"
			}
		}
	case p.neverReady == "ImagePullBackOff" || p.neverReady == "ErrImagePull" || p.neverReady == "InvalidImageName":
		phase = "Pending"
	case started:
		phase = "Running"
	default:
		phase = "Pending"
	}
	waiting := p.neverReady
	if waiting == "" && !started {
		waiting = "ContainerCreating"
	}
	if phase == "Failed" {
		waiting = ""
	}
	return k8s.Pod{
		Name: p.name, UID: p.uid, Labels: copyMap(p.labels), Phase: phase,
		Ready: phase == "Running" && p.neverReady == "", Deleting: p.deletedAt != nil, Node: p.node,
		Waiting: waiting, ExitCode: exitCode, Reason: reason,
	}
}

func (k *Kube) pod(namespace, name string) (*pod, error) {
	ns, err := k.live(namespace)
	if err != nil {
		return nil, err
	}
	p := ns.pods[name]
	if p == nil {
		return nil, k8s.NotFound("Error from server (NotFound): pods %q not found", name)
	}
	if !k.running(p) {
		return nil, &k8s.KubeError{Message: fmt.Sprintf("Error from server: pod %s is not running", name)}
	}
	return p, nil
}

func matches(selector, labels map[string]string) bool {
	for k, v := range selector {
		if labels[k] != v {
			return false
		}
	}
	return true
}

// -- k8s.KubeAPI --------------------------------------------------------------------------------

// CreateNamespace implements k8s.KubeAPI.
func (k *Kube) CreateNamespace(ctx context.Context, name string, labels map[string]string) (k8s.Namespace, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if err := k.call(ctx, "create_namespace"); err != nil {
		return k8s.Namespace{}, err
	}
	k.purge()
	if _, exists := k.namespaces[name]; exists {
		return k8s.Namespace{}, &k8s.KubeError{Message: fmt.Sprintf("Error from server (AlreadyExists): namespaces %q already exists", name)}
	}
	return k.addNamespace(name, labels, k.Clock.Now()), nil
}

// Namespaces implements k8s.KubeAPI.
func (k *Kube) Namespaces(ctx context.Context, selector map[string]string) ([]k8s.Namespace, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if err := k.call(ctx, "namespaces"); err != nil {
		return nil, err
	}
	k.purge()
	var found []k8s.Namespace
	for _, ns := range k.namespaces {
		if matches(selector, ns.labels) {
			found = append(found, k.view(ns))
		}
	}
	sort.Slice(found, func(i, j int) bool { return found[i].Name < found[j].Name })
	return found, nil
}

// DeleteNamespace implements k8s.KubeAPI.
func (k *Kube) DeleteNamespace(ctx context.Context, name, uid string) error {
	k.mu.Lock()
	defer k.mu.Unlock()
	if err := k.call(ctx, "delete_namespace"); err != nil {
		return err
	}
	ns, err := k.live(name)
	if err != nil {
		return err
	}
	if ns.uid != uid {
		return k8s.Conflict("Error from server (Conflict): Precondition failed: UID in precondition: %s, UID in object meta: %s", uid, ns.uid)
	}
	if ns.deletedAt == nil {
		t := k.Clock.Now()
		ns.deletedAt = &t
	}
	return nil
}

// Apply implements k8s.KubeAPI.
func (k *Kube) Apply(ctx context.Context, namespaceName string, objects []k8s.Object) error {
	k.mu.Lock()
	defer k.mu.Unlock()
	if err := k.call(ctx, "apply"); err != nil {
		return err
	}
	ns, err := k.writable(namespaceName)
	if err != nil {
		return err
	}
	for _, raw := range objects {
		obj := deepCopy(raw)
		kind, _ := obj["kind"].(string)
		meta := obj["metadata"].(map[string]any)
		name := meta["name"].(string)
		if got, ok := meta["namespace"]; ok && got != namespaceName {
			return &k8s.KubeError{Message: fmt.Sprintf("%s %s: the namespace of the object does not match %s", kind, name, namespaceName)}
		}
		ns.objects[[2]string{kind, name}] = obj
		spec, _ := obj["spec"].(map[string]any)
		switch {
		case kind == "Deployment" || kind == "StatefulSet":
			template := strMap(dig(spec, "template", "metadata", "labels"))
			selector := strMap(dig(spec, "selector", "matchLabels"))
			if !matches(selector, template) {
				return &k8s.KubeError{Message: fmt.Sprintf("%s %s: selector does not match template labels", kind, name)}
			}
			generation := 1
			if previous := ns.workloads[name]; previous != nil {
				generation = previous.generation + 1
			}
			replicas := 1
			if r, ok := spec["replicas"].(float64); ok {
				replicas = int(r)
			}
			ns.workloads[name] = &workload{
				kind: kind, name: name, labels: strMap(meta["labels"]), selector: selector,
				templateLabels: template, replicas: replicas, generation: generation,
			}
		case kind == "Job" && !ns.jobs[name]:
			ns.jobs[name] = true
			container := dig(spec, "template", "spec", "containers").([]any)[0].(map[string]any)
			labels := strMap(dig(spec, "template", "metadata", "labels"))
			labels["job-name"] = name
			// The wrapper: ["sh", "-c", WRAPPER, "vse-pod", *argv].
			var argv []string
			for _, a := range container["command"].([]any)[4:] {
				argv = append(argv, a.(string))
			}
			k.newPod(ns, fmt.Sprintf("%s-%s", name, k.uid()[4:]), labels, name, argv)
		}
	}
	k.reconcile(ns)
	return nil
}

func dig(m map[string]any, path ...string) any {
	var cur any = m
	for _, p := range path {
		next, ok := cur.(map[string]any)
		if !ok {
			return nil
		}
		cur = next[p]
	}
	return cur
}

func strMap(v any) map[string]string {
	out := map[string]string{}
	if m, ok := v.(map[string]any); ok {
		for k, val := range m {
			out[k], _ = val.(string)
		}
	}
	return out
}

// Workloads implements k8s.KubeAPI.
func (k *Kube) Workloads(ctx context.Context, namespace string) ([]k8s.Workload, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if err := k.call(ctx, "workloads"); err != nil {
		return nil, err
	}
	ns, err := k.live(namespace)
	if err != nil {
		return nil, err
	}
	var result []k8s.Workload
	for _, w := range ns.workloads {
		live, ready := 0, 0
		for _, p := range ns.pods {
			if p.owner == w.name && p.deletedAt == nil {
				live++
				if k.podView(p).Ready {
					ready++
				}
			}
		}
		result = append(result, k8s.Workload{
			Kind: w.kind, Name: w.name, Labels: copyMap(w.labels), Selector: copyMap(w.selector),
			Replicas: w.replicas, Ready: ready, Available: ready, Updated: live,
			Generation: w.generation, Observed: w.generation,
		})
	}
	sort.Slice(result, func(i, j int) bool { return result[i].Name < result[j].Name })
	return result, nil
}

// Pods implements k8s.KubeAPI.
func (k *Kube) Pods(ctx context.Context, namespace string, selector map[string]string) ([]k8s.Pod, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if err := k.call(ctx, "pods"); err != nil {
		return nil, err
	}
	ns, err := k.live(namespace)
	if err != nil {
		return nil, err
	}
	var names []string
	for name := range ns.pods {
		names = append(names, name)
	}
	sort.Strings(names)
	var found []k8s.Pod
	for _, name := range names {
		if matches(selector, ns.pods[name].labels) {
			found = append(found, k.podView(ns.pods[name]))
		}
	}
	return found, nil
}

// DeletePods implements k8s.KubeAPI.
func (k *Kube) DeletePods(ctx context.Context, namespace string, names []string, graceSecs int) error {
	k.mu.Lock()
	defer k.mu.Unlock()
	if err := k.call(ctx, "delete_pods"); err != nil {
		return err
	}
	ns, err := k.live(namespace)
	if err != nil {
		return err
	}
	for _, name := range names {
		if p := ns.pods[name]; p != nil && p.deletedAt == nil {
			t := k.Clock.Now()
			p.deletedAt, p.grace = &t, time.Duration(graceSecs)*time.Second
		}
	}
	return nil
}

// Scale implements k8s.KubeAPI.
func (k *Kube) Scale(ctx context.Context, namespace, deployment string, replicas int) error {
	k.mu.Lock()
	defer k.mu.Unlock()
	if err := k.call(ctx, "scale"); err != nil {
		return err
	}
	ns, err := k.writable(namespace)
	if err != nil {
		return err
	}
	w := ns.workloads[deployment]
	if w == nil {
		return k8s.NotFound("Error from server (NotFound): deployments.apps %q not found", deployment)
	}
	w.replicas = replicas
	w.generation++
	k.reconcile(ns)
	return nil
}

// Exec implements k8s.KubeAPI.
func (k *Kube) Exec(ctx context.Context, namespace, podName string, argv []string, _ time.Duration) (k8s.ExecResult, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if err := k.call(ctx, "exec"); err != nil {
		return k8s.ExecResult{}, err
	}
	target, err := k.pod(namespace, podName)
	if err != nil {
		return k8s.ExecResult{}, err
	}
	k.finishDue(target)
	switch {
	case len(argv) == 2 && argv[0] == "cat":
		data, ok := target.files[argv[1]]
		if !ok {
			return k8s.ExecResult{ExitCode: 1, Output: fmt.Sprintf("cat: %s: No such file or directory\n", argv[1])}, nil
		}
		return k8s.ExecResult{Output: string(data)}, nil
	case (len(argv) == 2 && argv[0] == "redis-cli" && argv[1] == "FLUSHALL") ||
		(len(argv) == 3 && argv[0] == "bash" && argv[1] == "-c" && strings.Contains(argv[2], "flush_all")):
		ns, _ := k.live(namespace)
		ns.flushed = append(ns.flushed, podName)
		return k8s.ExecResult{Output: "OK\n"}, nil
	}
	if program := k.Execs[argv[0]]; program != nil {
		return program(podName, argv), nil
	}
	return k8s.ExecResult{ExitCode: 127, Output: argv[0] + ": not found\n"}, nil
}

// finishDue publishes a finished command's exit marker and outputs once its time has come.
func (k *Kube) finishDue(p *pod) {
	if p.outcome == nil || p.doneAt == nil || k.Clock.Now().Before(*p.doneAt) || p.outcome.Crash {
		return
	}
	for name, data := range p.outcome.Outputs {
		if _, ok := p.files[k8s.WorkDir+"/"+name]; !ok {
			p.files[k8s.WorkDir+"/"+name] = data
		}
	}
	if _, ok := p.files[k8s.ControlDir+"/exit"]; !ok {
		p.files[k8s.ControlDir+"/exit"] = []byte(fmt.Sprintf("%d\n", p.outcome.ExitCode))
	}
}

// PutFiles implements k8s.KubeAPI.
func (k *Kube) PutFiles(ctx context.Context, namespace, podName, directory string, files []k8s.File) error {
	k.mu.Lock()
	defer k.mu.Unlock()
	if err := k.call(ctx, "put_files"); err != nil {
		return err
	}
	target, err := k.pod(namespace, podName)
	if err != nil {
		return err
	}
	for _, f := range files {
		target.files[directory+"/"+f.Name] = f.Data
	}
	if _, ready := target.files[k8s.ControlDir+"/ready"]; ready && target.outcome == nil && len(target.argv) > 0 {
		inputs := map[string][]byte{}
		for key, data := range target.files {
			if rest, ok := strings.CutPrefix(key, k8s.WorkDir+"/"); ok {
				inputs[rest] = data
			}
		}
		var outcome Outcome
		if program := k.Programs[target.argv[0]]; program != nil {
			// A program may use the fake's own methods (it looks at the cluster).
			k.mu.Unlock()
			outcome = program(target.argv, inputs)
			k.mu.Lock()
		} else {
			outcome = Outcome{ExitCode: 127, Log: fmt.Sprintf("sh: %s: not found\n", target.argv[0]), Secs: 100 * time.Millisecond}
		}
		secs := outcome.Secs
		if secs == 0 {
			secs = time.Second
		}
		done := k.Clock.Now().Add(secs)
		target.outcome, target.doneAt, target.log = &outcome, &done, outcome.Log
	}
	if _, collected := target.files[k8s.ControlDir+"/collected"]; collected && target.outcome != nil {
		k.finishDue(target)
		if _, exited := target.files[k8s.ControlDir+"/exit"]; exited {
			target.finished = true
		}
	}
	return nil
}

// GetFiles implements k8s.KubeAPI.
func (k *Kube) GetFiles(ctx context.Context, namespace, podName, directory, into string) ([]string, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if err := k.call(ctx, "get_files"); err != nil {
		return nil, err
	}
	target, err := k.pod(namespace, podName)
	if err != nil {
		return nil, err
	}
	k.finishDue(target)
	keys := make([]string, 0, len(target.files))
	for key := range target.files {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	var names []string
	for _, key := range keys {
		if rel, ok := strings.CutPrefix(key, directory+"/"); ok {
			path := filepath.Join(into, rel)
			if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
				return nil, err
			}
			if err := os.WriteFile(path, target.files[key], 0o644); err != nil {
				return nil, err
			}
			names = append(names, rel)
		}
	}
	return names, os.MkdirAll(into, 0o755)
}

// Logs implements k8s.KubeAPI.
func (k *Kube) Logs(ctx context.Context, namespace, podName string, tail int, previous bool) (string, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if err := k.call(ctx, "logs"); err != nil {
		return "", err
	}
	ns, err := k.live(namespace)
	if err != nil {
		return "", err
	}
	target := ns.pods[podName]
	if target == nil || previous {
		return "", nil
	}
	lines := strings.SplitAfter(target.log, "\n")
	if n := len(lines); n > 0 && lines[n-1] == "" {
		lines = lines[:n-1]
	}
	if tail > 0 && len(lines) > tail {
		lines = lines[len(lines)-tail:]
	}
	return strings.Join(lines, ""), nil
}

// PodStats implements k8s.KubeAPI.
func (k *Kube) PodStats(ctx context.Context, namespace, node string) (map[string]k8s.PodStats, error) {
	k.mu.Lock()
	defer k.mu.Unlock()
	if err := k.call(ctx, "pod_stats"); err != nil {
		return nil, err
	}
	ns, err := k.live(namespace)
	if err != nil {
		return nil, err
	}
	now := k.Clock.Now()
	stats := map[string]k8s.PodStats{}
	for _, p := range ns.pods {
		if p.node == node && k.running(p) {
			stats[p.name] = k8s.PodStats{CPUSeconds: max(now.Sub(p.created.Add(p.startSecs)).Seconds(), 0) * 0.5}
		}
	}
	return stats, nil
}

// Builder records builds (the image names and contexts) and fails those whose image starts with a
// prefix of Fail.
type Builder struct {
	Builds []k8s.Build
	Fail   []string
}

var _ k8s.Builder = (*Builder)(nil)

// Build implements k8s.Builder.
func (b *Builder) Build(ctx context.Context, builds []k8s.Build, _ time.Duration, _ string) (map[string]float64, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	seconds := map[string]float64{}
	for _, build := range builds {
		if _, err := os.Stat(filepath.Join(build.Context, "Dockerfile")); err != nil {
			return nil, &k8s.BuildError{Message: fmt.Sprintf("build of %s: no Dockerfile in %s", build.Image, build.Context)}
		}
		for _, prefix := range b.Fail {
			if strings.HasPrefix(build.Image, prefix) {
				return nil, &k8s.BuildError{Message: fmt.Sprintf("build of %s failed (buildctl exit 1); log tail:\nfake", build.Image)}
			}
		}
		b.Builds = append(b.Builds, build)
		seconds[build.Image] = 1
	}
	return seconds, nil
}
