package k8s

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/BurntSushi/toml"
)

// Waits and deadlines of the deploy layer.
const (
	WaitSecs       = 300 * time.Second // data tier, all replicas, a pod's start, a restart
	SeedSecs       = 1200 * time.Second
	ProbeSecs      = 300 * time.Second
	BuildSecs      = 2400 * time.Second
	DeleteWait     = 240 * time.Second
	DeleteAttempts = 3
	pollInterval   = 2 * time.Second
	exitPoll       = 1 * time.Second
	logTail        = 1500
	// OOMKilled is the termination reason of a container the kernel killed at its memory limit.
	OOMKilled = "OOMKilled"
	// ProbeFile is the pod file the probe's requests are written to.
	ProbeFile = "probe.binpb"
	// StateFile is where a Deployment keeps its CPU snapshot between processes (in WorkDir).
	usageStateFile = "usage-state.json"
)

// Environment is what vibesys-cli's `kubernetes` resource and the job give the deploy layer.
type Environment struct {
	Registry     string
	Builder      string
	ArtifactsDir string // "" when the job has none
	WorkDir      string
	Job          string
}

// ParseEnvironment reads the job's variables through getenv; artifacts overrides
// $VIBESYS_ARTIFACTS_DIR when not empty.
func ParseEnvironment(getenv func(string) string, workDir, artifacts string) (Environment, error) {
	for _, name := range []string{"KUBECONFIG", "VIBESYS_K8S_REGISTRY", "VIBESYS_K8S_BUILDER"} {
		if getenv(name) == "" {
			return Environment{}, deployErr("%s is not set: the task needs `[environment] needs = [\"kubernetes\"]` and a placement whose kubernetes resource names the registry and the builder", name)
		}
	}
	jobDir := getenv("VIBESYS_ARTIFACTS_DIR")
	if artifacts == "" {
		artifacts = jobDir
	}
	return Environment{
		Registry:     strings.TrimRight(getenv("VIBESYS_K8S_REGISTRY"), "/"),
		Builder:      getenv("VIBESYS_K8S_BUILDER"),
		ArtifactsDir: artifacts,
		WorkDir:      workDir,
		Job:          JobLabelOf(jobDir, randomToken()),
	}, nil
}

func randomToken() string {
	var b [4]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

// PodCommand is a command of the task image, and the files written into its pod's WorkDir first.
type PodCommand struct {
	Argv   []string
	Inputs map[string][]byte
}

// PodRun is a finished pod: its command's exit status, its outputs and its log. Files are the
// files the command left in WorkDir (inputs excepted), copied into Outputs.
type PodRun struct {
	Name     string
	ExitCode int
	Outputs  string
	Files    []string
	Log      string
	Seconds  float64
}

// Cluster is where and with what the deploy layer works: the job's environment and the interfaces.
type Cluster struct {
	Env     Environment
	Kube    KubeAPI
	Builder Builder
	Clock   Clock
	Log     func(string)
	// Token makes the suffix of a namespace name; 8 random hex digits when nil.
	Token func() string
}

func (c Cluster) logf(format string, args ...any) {
	if c.Log != nil {
		c.Log(fmt.Sprintf(format, args...))
	}
}

// AppSource is an App with what it is built from: the task directory (manifest, task image), the
// project root (candidate images) and the encoded `ready` requests (a loadgen requests file).
type AppSource struct {
	App         App
	TaskDir     string
	ProjectRoot string
	ProbePlan   []byte
}

// Deployment is an App in its namespace. Deploy makes one that owns the namespace; Attach one that
// does not. Methods are safe for concurrent use except Close.
type Deployment struct {
	App       App
	Namespace string
	TaskImage string
	Gateway   Gateway
	// Images are the candidate images by manifest variable, and BuildSeconds the seconds each
	// image took; both only on a Deployment made by Deploy.
	Images       map[string]string
	BuildSeconds map[string]float64

	rendered  Rendered
	probePlan []byte
	cluster   Cluster
	uid       string
	creating  bool
	owned     bool
	closed    bool

	mu    sync.Mutex
	pods  int
	usage map[string]Usage
}

func (d *Deployment) log(format string, args ...any) { d.cluster.logf(format, args...) }

// UID is the namespace's UID once it is created ("" before).
func (d *Deployment) UID() string { return d.uid }

// Owned makes a Deployment that owns an existing namespace, for a process that closes what
// another one deployed: Close deletes the namespace if it still has the UID. It reads no manifest.
func Owned(app App, namespace, uid string, c Cluster) *Deployment {
	gateway, _ := ParseGateway(app.Gateway)
	return &Deployment{App: app, Namespace: namespace, Gateway: gateway, cluster: c, uid: uid, creating: true, owned: true}
}

func (d *Deployment) kube() KubeAPI { return d.cluster.Kube }

// TaskTimeout is the longest a job of the task may run: the larger of the two timeout_seconds of
// the task's vibesys.input.toml.
func TaskTimeout(taskDir string) (time.Duration, error) {
	path := filepath.Join(taskDir, "vibesys.input.toml")
	bad := func(err any) (time.Duration, error) {
		return 0, deployErr("%s: cannot read [accuracy]/[benchmark] timeout_seconds (%v)", path, err)
	}
	var manifest map[string]map[string]any
	if _, err := toml.DecodeFile(path, &manifest); err != nil {
		return bad(err)
	}
	var longest int64 = -1
	for _, section := range []string{"accuracy", "benchmark"} {
		table, ok := manifest[section]
		if !ok {
			continue
		}
		secs, ok := table["timeout_seconds"].(int64)
		if !ok {
			return bad("KeyError: 'timeout_seconds'")
		}
		longest = max(longest, secs)
	}
	if longest < 0 {
		return bad("no section")
	}
	return time.Duration(longest) * time.Second, nil
}

// Sweep deletes this task's namespaces that killed samples left behind and returns their names.
//
// Only namespaces labelled with the App's name, named with its prefix and older than the longest a
// job may run plus a margin are deleted, each only if it still has the UID it was listed with, so
// a concurrent sample's namespace or another task's is never touched.
func Sweep(ctx context.Context, src AppSource, c Cluster) ([]string, error) {
	ttl, err := TaskTimeout(src.TaskDir)
	if err != nil {
		return nil, err
	}
	ttl += SweepMargin
	listed, err := c.Kube.Namespaces(ctx, map[string]string{TaskLabel: src.App.Name})
	if err != nil {
		return nil, err
	}
	var swept []string
	for _, ns := range Stale(listed, src.App.Name, c.Clock.Now(), ttl) {
		if err := c.Kube.DeleteNamespace(ctx, ns.Name, ns.UID); err != nil {
			if ctx.Err() != nil {
				return swept, ctx.Err()
			}
			// Gone, recreated, or refused: never retried against a new UID.
			c.logf("sweep: left %s (%s)", ns.Name, shorten(err.Error(), 200))
			continue
		}
		c.logf("sweep: deleted stale namespace %s (job %s)", ns.Name, orQuestion(ns.Labels[JobLabel]))
		swept = append(swept, ns.Name)
	}
	return swept, nil
}

func orQuestion(s string) string {
	if s == "" {
		return "?"
	}
	return s
}

func shorten(s string, n int) string {
	if len(s) > n {
		return s[:n]
	}
	return s
}

func lastN(s string, n int) string {
	if len(s) > n {
		return s[len(s)-n:]
	}
	return s
}

// images are the candidate images by manifest variable, the task image, and the builds that make
// them.
func images(src AppSource, registry, staging string) (map[string]string, string, []Build, error) {
	app := src.App
	builds := map[string]Build{}
	var order []string
	byVariable := map[string]string{}
	slug := Slug(app.Name)
	for variable, directory := range sortedPairs(app.Images) {
		context := filepath.Join(src.ProjectRoot, directory)
		if !isFile(filepath.Join(context, "Dockerfile")) {
			return nil, "", nil, deployErr("k8s.App(images[%q]): %s/Dockerfile not found", variable, context)
		}
		digest, err := TreeDigest(context)
		if err != nil {
			return nil, "", nil, deployErr("k8s.App(images[%q]): %v", variable, err)
		}
		image := ImageName(registry, slug, filepath.Base(directory), digest)
		byVariable[variable] = image
		if _, seen := builds[image]; !seen {
			order = append(order, image)
		}
		builds[image] = Build{Context: context, Image: image}
	}
	if !isFile(filepath.Join(src.TaskDir, "Dockerfile")) {
		return nil, "", nil, deployErr("%s/Dockerfile not found: the phase pods run the task image", src.TaskDir)
	}
	digest, err := TreeDigest(src.TaskDir)
	if err != nil {
		return nil, "", nil, deployErr("%s: %v", src.TaskDir, err)
	}
	taskImage := ImageName(registry, slug, "task", digest)
	staged, err := Stage(src.TaskDir, staging)
	if err != nil {
		return nil, "", nil, deployErr("staging %s: %v", src.TaskDir, err)
	}
	if _, seen := builds[taskImage]; !seen {
		order = append(order, taskImage)
	}
	builds[taskImage] = Build{Context: staged, Image: taskImage}
	list := make([]Build, 0, len(order))
	for _, image := range order {
		list = append(list, builds[image])
	}
	return byVariable, taskImage, list, nil
}

func isFile(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular()
}

func readManifest(src AppSource) (string, error) {
	data, err := os.ReadFile(filepath.Join(src.TaskDir, src.App.Manifest))
	if err != nil {
		return "", deployErr("k8s.App(manifest=%q): %v", src.App.Manifest, err)
	}
	return string(data), nil
}

// checkNames checks that the App's Deployment and Service names exist in the manifest (before any
// build).
func checkNames(app App, rendered Rendered) error {
	deployments := workloadNames(rendered.Objects(), "Deployment")
	for _, field := range []struct {
		name  string
		names []string
	}{{"restartable", app.Restartable}, {"caches", cacheNames(app)}} {
		var missing []string
		for _, n := range field.names {
			if !deployments[n] {
				missing = append(missing, n)
			}
		}
		if len(missing) > 0 {
			sort.Strings(missing)
			return deployErr("k8s.App(%s): %s not a Deployment of %s", field.name, strings.Join(missing, ", "), app.Manifest)
		}
	}
	gateway, _ := ParseGateway(app.Gateway)
	if !workloadNames(rendered.Objects(), "Service")[gateway.Service] {
		return deployErr("k8s.App(gateway=%q): no Service %q in %s", app.Gateway, gateway.Service, app.Manifest)
	}
	return nil
}

func cacheNames(app App) []string {
	names := make([]string, 0, len(app.Caches))
	for n := range sortedPairs(app.Caches) {
		names = append(names, n)
	}
	return names
}

// Deploy sweeps, builds, deploys into a fresh namespace, seeds and probes, and returns the serving
// Deployment. The caller owns the result and must Close it. On any failure here, including the
// context's cancellation (a signal), the namespace is deleted before the error returns.
func Deploy(ctx context.Context, src AppSource, seed *PodCommand, c Cluster) (*Deployment, error) {
	app := src.App
	if err := app.Validate(); err != nil {
		return nil, &Error{Kind: KindInvalid, Message: err.Error()}
	}
	if (seed == nil) == app.HasSeed {
		return nil, deployErr("deploy: a seed pod command is given exactly when k8s.App(seed=...) is set")
	}
	if len(src.ProbePlan) == 0 {
		return nil, deployErr("deploy: no probe plan (k8s.App(ready=...) encoded as a requests file)")
	}
	token := c.Token
	if token == nil {
		token = randomToken
	}
	namespace := NamespaceName(app.Name, token())
	text, err := readManifest(src)
	if err != nil {
		return nil, err
	}
	if _, err := Sweep(ctx, src, c); err != nil {
		return nil, err
	}
	imageMap, taskImage, builds, err := images(src, c.Env.Registry, filepath.Join(c.Env.WorkDir, "task-context"))
	if err != nil {
		return nil, err
	}
	tier, err := ParseSelector(app.DataTier)
	if err != nil {
		return nil, err
	}
	rendered, err := Render(text, namespace, imageMap, tier, app.Manifest)
	if err != nil {
		return nil, err
	}
	if err := checkNames(app, rendered); err != nil {
		return nil, err
	}
	gateway, _ := ParseGateway(app.Gateway)
	d := &Deployment{
		App: app, Namespace: namespace, TaskImage: taskImage, Gateway: gateway, Images: imageMap,
		rendered: rendered, probePlan: src.ProbePlan, cluster: c, owned: true,
	}
	d.writeArtifact("manifest.rendered.yaml", rendered.Text)
	fail := func(err error) (*Deployment, error) {
		stop := ctx.Err() != nil
		if closeErr := d.Close(ctx, CloseOptions{Fast: stop, Failed: true}); closeErr != nil && !stop {
			d.log("teardown after failure: %v", closeErr)
		}
		return nil, err
	}
	c.logf("building %d images", len(builds))
	seconds, err := c.Builder.Build(ctx, builds, BuildSecs, filepath.Join(c.Env.WorkDir, "build"))
	if err != nil {
		return fail(err)
	}
	d.BuildSeconds = seconds
	summary, _ := json.MarshalIndent(map[string]any{"images": imageMap, "task": taskImage, "build_seconds": seconds}, "", "  ")
	d.writeArtifact("images.json", string(summary)+"\n")
	d.creating = true
	ns, err := c.Kube.CreateNamespace(ctx, namespace, NamespaceLabels(app.Name, c.Clock.Now(), c.Env.Job))
	if err != nil {
		return fail(err)
	}
	d.uid = ns.UID
	if err := d.bringUp(ctx, seed); err != nil {
		return fail(err)
	}
	return d, nil
}

// Attach makes a Deployment for a namespace another process deployed (the fault actions); it never
// deletes it.
func Attach(ctx context.Context, src AppSource, namespace string, c Cluster) (*Deployment, error) {
	app := src.App
	if err := app.Validate(); err != nil {
		return nil, &Error{Kind: KindInvalid, Message: err.Error()}
	}
	if !strings.HasPrefix(namespace, NamespacePrefixOf(app.Name)) {
		return nil, deployErr("ctl: namespace %q is not one of %s's", namespace, app.Name)
	}
	listed, err := c.Kube.Namespaces(ctx, map[string]string{TaskLabel: app.Name})
	if err != nil {
		return nil, err
	}
	found := false
	for _, ns := range listed {
		found = found || ns.Name == namespace
	}
	if !found {
		return nil, deployErr("ctl: no namespace %q labelled for %s", namespace, app.Name)
	}
	text, err := readManifest(src)
	if err != nil {
		return nil, err
	}
	// Only replica counts and selectors are read here: the image names do not matter.
	placeholder := map[string]string{}
	for v := range app.Images {
		placeholder[v] = strings.ToLower(v)
	}
	tier, err := ParseSelector(app.DataTier)
	if err != nil {
		return nil, err
	}
	rendered, err := Render(text, namespace, placeholder, tier, app.Manifest)
	if err != nil {
		return nil, err
	}
	digest, err := TreeDigest(src.TaskDir)
	if err != nil {
		return nil, deployErr("%s: %v", src.TaskDir, err)
	}
	gateway, _ := ParseGateway(app.Gateway)
	return &Deployment{
		App: app, Namespace: namespace, Gateway: gateway,
		TaskImage: ImageName(c.Env.Registry, Slug(app.Name), "task", digest),
		rendered:  rendered, probePlan: src.ProbePlan, cluster: c,
	}, nil
}

// CloseOptions say how Close ends the namespace.
type CloseOptions struct {
	// Fast (after a signal) asks for the deletion and returns, within the CLI's grace period.
	// Otherwise Close waits (bounded) until the namespace is gone so the next sample finds the
	// cluster free.
	Fast bool
	// Failed saves diagnostics first (unless Fast).
	Failed bool
}

// Close deletes the namespace, if this Deployment owns it; it is idempotent. A cancelled ctx does
// not stop the deletion request: only its wait.
func (d *Deployment) Close(ctx context.Context, opts CloseOptions) error {
	if d.closed || !d.owned || !d.creating {
		return nil
	}
	d.closed = true
	done := context.WithoutCancel(ctx)
	if opts.Failed && !opts.Fast {
		d.saveDiagnostics(done)
	}
	uid := d.uid
	if uid == "" {
		var err error
		if uid, err = d.findUID(done); err != nil {
			return err
		}
	}
	if uid == "" {
		return nil
	}
	d.requestDelete(done, uid)
	if !opts.Fast && ctx.Err() == nil {
		return d.waitDeleted(ctx)
	}
	return nil
}

// findUID is the namespace's UID when its creation was cut short before its answer arrived.
func (d *Deployment) findUID(ctx context.Context) (string, error) {
	listed, err := d.kube().Namespaces(ctx, map[string]string{TaskLabel: d.App.Name})
	if err != nil {
		return "", err
	}
	for _, ns := range listed {
		if ns.Name == d.Namespace {
			return ns.UID, nil
		}
	}
	return "", nil
}

// requestDelete asks for the deletion, retrying a failed request twice; gone or recreated is final.
func (d *Deployment) requestDelete(ctx context.Context, uid string) {
	d.log("deleting namespace %s", d.Namespace)
	for attempt := 0; attempt < DeleteAttempts; attempt++ {
		err := d.kube().DeleteNamespace(ctx, d.Namespace, uid)
		switch {
		case err == nil:
			return
		case errors.Is(err, ErrNotFound) || errors.Is(err, ErrConflict):
			d.log("namespace %s: %s", d.Namespace, shorten(err.Error(), 300))
			return
		default:
			d.log("namespace %s: deletion attempt %d failed: %s", d.Namespace, attempt+1, shorten(err.Error(), 300))
		}
	}
	d.log("namespace %s left for the next sample's sweep", d.Namespace)
}

func (d *Deployment) waitDeleted(ctx context.Context) error {
	clock := d.cluster.Clock
	deadline := clock.Now().Add(DeleteWait)
	for clock.Now().Before(deadline) {
		listed, err := d.kube().Namespaces(ctx, map[string]string{TaskLabel: d.App.Name})
		if err != nil {
			return err
		}
		gone := true
		for _, ns := range listed {
			gone = gone && ns.Name != d.Namespace
		}
		if gone {
			return nil
		}
		if err := clock.Sleep(ctx, pollInterval); err != nil {
			return err
		}
	}
	d.log("namespace %s: still terminating after %.0fs", d.Namespace, DeleteWait.Seconds())
	return nil
}

// -- bring-up -----------------------------------------------------------------------------------

// bringUp applies, waits, seeds and probes in the order the data tier asks for.
func (d *Deployment) bringUp(ctx context.Context, seed *PodCommand) error {
	tier := len(d.rendered.Rest) > 0
	what := "the manifest"
	if tier {
		what = "the data tier"
	}
	d.log("applying %s into %s", what, d.Namespace)
	if err := d.kube().Apply(ctx, d.Namespace, d.rendered.First); err != nil {
		return err
	}
	first := workloadNames(d.rendered.First, "Deployment")
	for n := range workloadNames(d.rendered.First, "StatefulSet") {
		first[n] = true
	}
	if err := d.waitServing(ctx, first, nil, WaitSecs); err != nil {
		return err
	}
	if tier {
		if err := d.seed(ctx, seed); err != nil {
			return err
		}
		d.log("applying the application")
		if err := d.kube().Apply(ctx, d.Namespace, d.rendered.Rest); err != nil {
			return err
		}
		if err := d.waitServing(ctx, nil, nil, WaitSecs); err != nil {
			return err
		}
		return d.Probe(ctx)
	}
	if err := d.waitServing(ctx, nil, nil, WaitSecs); err != nil {
		return err
	}
	if err := d.Probe(ctx); err != nil {
		return err
	}
	return d.seed(ctx, seed)
}

func (d *Deployment) seed(ctx context.Context, seed *PodCommand) error {
	if seed == nil {
		return nil
	}
	d.log("seeding: starting the seed pod")
	run, err := d.RunPod(ctx, RoleSeed, *seed, SeedSecs, RunOptions{})
	if err != nil {
		return err
	}
	if run.ExitCode != 0 {
		return deployErr("seed failed (exit %d) after %.0fs; log tail:\n%s", run.ExitCode, run.Seconds, lastN(run.Log, logTail))
	}
	d.log("seeded in %.0fs", run.Seconds)
	return nil
}

// WaitServing waits until every workload (of names, when not nil) has all its replicas Ready and
// available, none of them among the replaced pod UIDs.
func (d *Deployment) WaitServing(ctx context.Context, names, replaced map[string]bool, timeout time.Duration) error {
	return d.waitServing(ctx, names, replaced, timeout)
}

func (d *Deployment) waitServing(ctx context.Context, names, replaced map[string]bool, timeout time.Duration) error {
	clock := d.cluster.Clock
	deadline := clock.Now().Add(timeout)
	for {
		pods, err := d.kube().Pods(ctx, d.Namespace, nil)
		if err != nil {
			return err
		}
		var appPods []Pod
		for _, p := range pods {
			if _, ours := p.Labels[PodLabel]; !ours {
				appPods = append(appPods, p)
			}
		}
		workloads, err := d.kube().Workloads(ctx, d.Namespace)
		if err != nil {
			return err
		}
		reasons := Unready(workloads, appPods, names, replaced)
		if len(reasons) == 0 {
			return nil
		}
		fatal := Fatal(appPods)
		if len(fatal) > 0 || !clock.Now().Before(deadline) {
			why := fmt.Sprintf("not serving after %.0fs", timeout.Seconds())
			if len(fatal) > 0 {
				why = "cannot start"
				reasons = fatal
			}
			return deployErr("%s: %s", why, strings.Join(head(reasons, 12), "; "))
		}
		if err := clock.Sleep(ctx, pollInterval); err != nil {
			return err
		}
	}
}

// Probe runs loadgen probe from a pod: every ready request succeeds twice per ready gateway pod.
func (d *Deployment) Probe(ctx context.Context) error {
	var service Object
	for _, obj := range d.rendered.Objects() {
		if obj["kind"] == "Service" && objectName(obj) == d.Gateway.Service {
			service = obj
		}
	}
	if service == nil {
		return deployErr("k8s.App(gateway=%q): the manifest has no Service %q", d.App.Gateway, d.Gateway.Service)
	}
	selector := map[string]string{}
	if spec, ok := service["spec"].(map[string]any); ok {
		if sel, ok := spec["selector"].(map[string]any); ok {
			for k, v := range sel {
				selector[k], _ = v.(string)
			}
		}
	}
	pods, err := d.kube().Pods(ctx, d.Namespace, selector)
	if err != nil {
		return err
	}
	ready := 0
	for _, p := range pods {
		if p.Ready && !p.Deleting {
			ready++
		}
	}
	rounds := 2 * max(ready, 1)
	argv := []string{
		"loadgen", "probe", "--target", d.Gateway.URL, "--requests", WorkDir + "/" + ProbeFile,
		"--rounds", fmt.Sprint(rounds), "--deadline", fmt.Sprint(int(ProbeSecs.Seconds())),
	}
	run, err := d.RunPod(ctx, RoleProbe, PodCommand{Argv: argv, Inputs: map[string][]byte{ProbeFile: d.probePlan}}, ProbeSecs+WaitSecs, RunOptions{})
	if err != nil {
		return err
	}
	if run.ExitCode != 0 {
		return deployErr("not ready: the probes through %s failed (loadgen probe exit %d); log tail:\n%s", d.Gateway.URL, run.ExitCode, lastN(run.Log, logTail))
	}
	d.log("ready: probes answered through %s (%d rounds)", d.Gateway.URL, rounds)
	return nil
}

// -- the package's pods ---------------------------------------------------------------------------

// RunOptions are the optional parts of RunPod.
type RunOptions struct {
	// Outputs is the local directory the files the command leaves in WorkDir are copied into
	// (WorkDir/<pod name> of the Environment by default).
	Outputs string
	// Name is the pod's Job name (vse-<role>-<n> by default, a random suffix instead of n from
	// a process that did not deploy); it must be unique in the namespace.
	Name string
}

// RunPod runs a command of the task image in a pod of the namespace and collects WorkDir.
//
// The pod gets command.Inputs in WorkDir before the command starts; the files the command leaves
// there are copied into the outputs directory. A nonzero exit is returned, not an error (the
// caller judges it); a pod that cannot start, dies or runs past the timeout is an error with its
// log tail (an *OutOfMemoryError when the kernel killed it at its memory limit).
func (d *Deployment) RunPod(ctx context.Context, role Role, command PodCommand, timeout time.Duration, opts RunOptions) (PodRun, error) {
	clock := d.cluster.Clock
	name := opts.Name
	if name == "" {
		// A Deployment that made the namespace counts its pods, as the artifacts have always named
		// them; another process attached to it cannot know the count, and a Job name that exists
		// is not run again, so its pods get a random suffix.
		d.mu.Lock()
		d.pods++
		if d.owned {
			name = fmt.Sprintf("vse-%s-%d", role, d.pods)
		} else {
			name = fmt.Sprintf("vse-%s-%s", role, randomToken())
		}
		d.mu.Unlock()
	}
	started := clock.Now()
	deadline := int(timeout.Seconds() + 2*WaitSecs.Seconds())
	if err := d.kube().Apply(ctx, d.Namespace, []Object{job(name, role, d.TaskImage, command.Argv, deadline)}); err != nil {
		return PodRun{}, err
	}
	pod, err := d.waitPodRunning(ctx, name)
	if err != nil {
		return PodRun{}, err
	}
	inputNames := make([]string, 0, len(command.Inputs))
	for key := range command.Inputs {
		inputNames = append(inputNames, key)
	}
	sort.Strings(inputNames)
	work := strings.TrimPrefix(WorkDir, Root+"/")
	var files []File
	for _, key := range inputNames {
		files = append(files, File{Name: work + "/" + key, Data: command.Inputs[key]})
	}
	files = append(files, File{Name: "control/" + readyMark})
	if err := d.kube().PutFiles(ctx, d.Namespace, pod, Root, files); err != nil {
		return PodRun{}, err
	}
	code, err := d.waitExit(ctx, name, pod, started.Add(timeout))
	if err != nil {
		return PodRun{}, err
	}
	outputs := opts.Outputs
	if outputs == "" {
		outputs = filepath.Join(d.cluster.Env.WorkDir, name)
	}
	got, err := d.kube().GetFiles(ctx, d.Namespace, pod, WorkDir, outputs)
	if err != nil {
		return PodRun{}, err
	}
	var names []string
	for _, n := range got {
		if _, isInput := command.Inputs[n]; !isInput {
			names = append(names, n)
		}
	}
	for key := range command.Inputs {
		_ = os.Remove(filepath.Join(outputs, key))
	}
	if err := d.kube().PutFiles(ctx, d.Namespace, pod, Root, []File{{Name: "control/" + collectedMark}}); err != nil {
		return PodRun{}, err
	}
	log, err := d.kube().Logs(ctx, d.Namespace, pod, 0, false)
	if err != nil {
		return PodRun{}, err
	}
	d.writeArtifact(name+".log", log)
	return PodRun{
		Name: name, ExitCode: code, Outputs: outputs, Files: names, Log: log,
		Seconds: round(clock.Now().Sub(started).Seconds(), 1),
	}, nil
}

func (d *Deployment) jobPod(ctx context.Context, name string) (*Pod, error) {
	pods, err := d.kube().Pods(ctx, d.Namespace, map[string]string{"job-name": name})
	if err != nil || len(pods) == 0 {
		return nil, err
	}
	return &pods[0], nil
}

func (d *Deployment) podFailure(ctx context.Context, name string, pod Pod) error {
	log, _ := d.kube().Logs(context.WithoutCancel(ctx), d.Namespace, pod.Name, 0, false)
	d.writeArtifact(name+".log", log)
	state := pod.Waiting
	if state == "" {
		state = pod.Phase
	}
	code := ""
	if pod.ExitCode != nil {
		code = fmt.Sprintf(", exit %d", *pod.ExitCode)
	}
	if pod.Reason == OOMKilled {
		limit := Sizes[Role(pod.Labels[PodLabel])].Memory
		return &OutOfMemoryError{
			Message: fmt.Sprintf("pod %s (%s) was killed for running out of memory (OOMKilled at its %s limit%s); log tail:\n%s",
				name, pod.Name, limit, code, lastN(log, logTail)),
			Limit: limit,
		}
	}
	return deployErr("pod %s (%s) %s%s; log tail:\n%s", name, pod.Name, state, code, lastN(log, logTail))
}

func (d *Deployment) waitPodRunning(ctx context.Context, name string) (string, error) {
	clock := d.cluster.Clock
	deadline := clock.Now().Add(WaitSecs)
	for {
		pod, err := d.jobPod(ctx, name)
		if err != nil {
			return "", err
		}
		if pod != nil && pod.Phase == "Running" {
			return pod.Name, nil
		}
		if pod != nil && (pod.Phase == "Succeeded" || pod.Phase == "Failed" || fatalWaiting[pod.Waiting]) {
			return "", d.podFailure(ctx, name, *pod)
		}
		if !clock.Now().Before(deadline) {
			state := "no pod"
			if pod != nil {
				if state = pod.Waiting; state == "" {
					state = pod.Phase
				}
			}
			return "", deployErr("pod %s did not start in %.0fs (%s)", name, WaitSecs.Seconds(), state)
		}
		if err := clock.Sleep(ctx, pollInterval); err != nil {
			return "", err
		}
	}
}

func (d *Deployment) waitExit(ctx context.Context, name, pod string, deadline time.Time) (int, error) {
	clock := d.cluster.Clock
	mark := ControlDir + "/" + exitMark
	for {
		result, err := d.kube().Exec(ctx, d.Namespace, pod, []string{"cat", mark}, 30*time.Second)
		if err != nil {
			if ctx.Err() != nil {
				return 0, ctx.Err()
			}
			result = ExecResult{ExitCode: -1} // the container stopped (or the API blinked): the pod's state says which
		}
		if result.ExitCode == 0 {
			if code, err := strconv.Atoi(strings.TrimSpace(result.Output)); err == nil {
				return code, nil
			}
		}
		current, err := d.jobPod(ctx, name)
		if err != nil {
			return 0, err
		}
		if current == nil {
			return 0, deployErr("pod %s vanished", name)
		}
		if current.Phase != "Running" {
			return 0, d.podFailure(ctx, name, *current)
		}
		if !clock.Now().Before(deadline) {
			log, _ := d.kube().Logs(context.WithoutCancel(ctx), d.Namespace, pod, 40, false)
			return 0, deployErr("pod %s did not finish in time; log tail:\n%s", name, lastN(log, logTail))
		}
		if err := clock.Sleep(ctx, exitPoll); err != nil {
			return 0, err
		}
	}
}

// -- diagnostics ------------------------------------------------------------------------------------

// Usage returns each pod's CPU seconds since the previous snapshot, logs it and writes
// pod-usage-<label>.json into the artifacts. It is diagnostics only: a cluster that does not give
// kubelet stats logs why and returns nil. The previous snapshot is kept in the Environment's
// WorkDir, so successive processes of one deploy share it.
func (d *Deployment) Usage(ctx context.Context, label string) (map[string]float64, error) {
	pods, err := d.kube().Pods(ctx, d.Namespace, nil)
	var now map[string]Usage
	if err == nil {
		var appPods []Pod
		stats := map[string]PodStats{}
		for _, p := range pods {
			if _, ours := p.Labels[PodLabel]; !ours {
				appPods = append(appPods, p)
			}
		}
		nodes := map[string]bool{}
		for _, p := range appPods {
			if p.Node != "" {
				nodes[p.Node] = true
			}
		}
		for _, node := range sortedKeys(nodes) {
			var perNode map[string]PodStats
			if perNode, err = d.kube().PodStats(ctx, d.Namespace, node); err != nil {
				break
			}
			for k, v := range perNode {
				stats[k] = v
			}
		}
		if err == nil {
			var workloads []Workload
			if workloads, err = d.kube().Workloads(ctx, d.Namespace); err == nil {
				now = JoinUsage(appPods, workloads, stats)
			}
		}
	}
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		d.log("pod usage %s: unavailable (%s)", label, shorten(err.Error(), 200))
		return nil, nil
	}
	d.mu.Lock()
	if d.usage == nil {
		d.usage = d.loadUsage()
	}
	delta := UsageDelta(d.usage, now)
	d.usage = now
	d.saveUsage(now)
	d.mu.Unlock()
	var lines []string
	for _, name := range sortedKeys(delta) {
		lines = append(lines, fmt.Sprintf("  %s (%s): %.1f CPU s", name, now[name].Workload, delta[name]))
	}
	d.log("pod usage %s:\n%s", label, strings.Join(lines, "\n"))
	report := map[string]any{}
	for name, item := range now {
		report[name] = map[string]any{
			"workload": item.Workload, "cpu_seconds": item.CPUSeconds, "cpu_seconds_delta": delta[name],
			"rx_bytes": item.RxBytes, "tx_bytes": item.TxBytes,
		}
	}
	text, _ := json.MarshalIndent(report, "", "  ")
	d.writeArtifact("pod-usage-"+label+".json", string(text)+"\n")
	return delta, nil
}

func (d *Deployment) loadUsage() map[string]Usage {
	usage := map[string]Usage{}
	if dir := d.cluster.Env.WorkDir; dir != "" {
		if data, err := os.ReadFile(filepath.Join(dir, usageStateFile)); err == nil {
			_ = json.Unmarshal(data, &usage)
		}
	}
	return usage
}

func (d *Deployment) saveUsage(usage map[string]Usage) {
	if dir := d.cluster.Env.WorkDir; dir != "" {
		if data, err := json.Marshal(usage); err == nil {
			_ = os.MkdirAll(dir, 0o755)
			_ = os.WriteFile(filepath.Join(dir, usageStateFile), data, 0o644)
		}
	}
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// saveDiagnostics saves the pods, and the log tails of those not serving or restarted, into the
// artifacts.
func (d *Deployment) saveDiagnostics(ctx context.Context) {
	if d.cluster.Env.ArtifactsDir == "" {
		return
	}
	unavailable := func(err error) { d.log("diagnostics unavailable: %s", shorten(err.Error(), 200)) }
	pods, err := d.kube().Pods(ctx, d.Namespace, nil)
	if err != nil {
		unavailable(err)
		return
	}
	var list []map[string]any
	for _, p := range pods {
		var node any
		if p.Node != "" {
			node = p.Node
		}
		list = append(list, map[string]any{
			"name": p.Name, "phase": p.Phase, "ready": p.Ready, "restarts": p.Restarts,
			"waiting": p.Waiting, "node": node,
		})
	}
	text, _ := json.MarshalIndent(list, "", "  ")
	d.writeArtifact("pods.json", string(text)+"\n")
	for _, p := range pods {
		if p.Ready && p.Restarts == 0 {
			continue
		}
		out, err := d.kube().Logs(ctx, d.Namespace, p.Name, 300, false)
		if err != nil {
			unavailable(err)
			return
		}
		if p.Restarts > 0 {
			previous, err := d.kube().Logs(ctx, d.Namespace, p.Name, 300, true)
			if err != nil {
				unavailable(err)
				return
			}
			out += "\n--- previous container ---\n" + previous
		}
		d.writeArtifact("logs/"+p.Name+".log", out)
	}
}

func (d *Deployment) writeArtifact(name, text string) {
	dir := d.cluster.Env.ArtifactsDir
	if dir == "" {
		return
	}
	path := filepath.Join(dir, name)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err == nil {
		_ = os.WriteFile(path, bytes.NewBufferString(text).Bytes(), 0o644)
	}
}
