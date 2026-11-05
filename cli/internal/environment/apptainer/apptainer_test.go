package apptainer_test

import (
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"path"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clusterfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

const (
	root          = "/shared/vibesys"
	scope         = "test"
	dockerfile    = "FROM busybox:1.36.1\n"
	probeProgram  = "/probe/vibesys-cli"
	probePause    = 7 * time.Second
	startup       = 3 * time.Minute
	limit         = time.Hour
	commitOfTests = "0123456789abcdef0123456789abcdef01234567"
)

// setup is what a test changes of a rig.
type setup struct {
	// timers are the environment's; engineTimers the engine's own, which never fire when unset.
	timers, engineTimers clock.Timers
	process              uint32
	scripts              map[string]string
	configure            func(*docker.Config)
}

// rig is one process's docker environment over the apptainer engine, on a cluster it shares with
// other rigs.
type rig struct {
	cluster  *clusterfake.Cluster
	client   *clusterfake.Client
	local    *dockerfake.Client
	engine   *apptainer.Engine
	provider *docker.Provider
	env      environment.Environment
}

func newRig(t testing.TB, w fakeworld.World, cluster *clusterfake.Cluster, o setup) *rig {
	t.Helper()
	if o.timers == nil {
		o.timers = envtest.NewTimers()
	}
	if o.engineTimers == nil {
		o.engineTimers = envtest.NewTimers()
	}
	local := dockerfake.NewDaemon(w.MemoryGates).Connect()
	client := cluster.Connect()
	eng, err := apptainer.New(apptainer.Config{
		Local: local, Export: local, Cluster: client, Root: root, Scope: scope,
		Timers: o.engineTimers, Clock: clock.NewManual(time.Unix(1_700_000_000, 0), time.Second),
		Sizing:      apptainer.Sizing{Partition: "part-a", Arguments: []string{"--account=acct-a"}},
		NodeScripts: o.scripts,
	})
	if err != nil {
		t.Fatal(err)
	}
	cfg := docker.Config{
		Engine: eng, Source: dockerfake.Fixed(docker.Spec{Dockerfile: []byte(dockerfile)}), Backend: eng.Backend(),
		Timers: o.timers, IDs: dockerfake.NewIDs(o.process), Scope: scope, Placement: "default",
		User: docker.User{UID: 1000, GID: 1000},
	}
	if o.configure != nil {
		o.configure(&cfg)
	}
	p, err := docker.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	e, err := p.Environment(environment.Target{Repository: "/repo", Commit: commitOfTests, Task: "task"})
	if err != nil {
		t.Fatal(err)
	}
	return &rig{cluster: cluster, client: client, local: local, engine: eng, provider: p, env: e}
}

var checkouts sync.Map

func checkout(t testing.TB) core.Checkout {
	n := 0
	checkouts.Range(func(any, any) bool { n++; return true })
	checkouts.Store(t.Name()+fmt.Sprint(n), true)
	return core.Checkout{Commit: commitOfTests, Dir: fmt.Sprintf("/fake-checkouts/a%d", n)}
}

// nothingLeft fails the test when an allocation or a process is left on the cluster.
func nothingLeft(t testing.TB, c *clusterfake.Cluster) {
	t.Helper()
	if live := c.Live(); len(live) != 0 {
		t.Errorf("allocations left: %v", live)
	}
	if running := c.Running(); len(running) != 0 {
		t.Errorf("processes left: %v", running)
	}
}

// The docker environment over the apptainer engine is held to the same contract as every
// environment.
func TestTheDockerEnvironmentOnApptainerPassesTheEnvironmentContract(t *testing.T) {
	envtest.Run(t, envtest.Rig{
		New: func(t *testing.T, w envtest.World, timers clock.Timers) environment.Environment {
			cluster := clusterfake.New(w.(fakeworld.World).MemoryGates)
			t.Cleanup(func() { nothingLeft(t, cluster) })
			return newRig(t, w.(fakeworld.World), cluster, setup{timers: timers}).env
		},
		World:    func(t *testing.T) envtest.World { return fakeworld.New() },
		Checkout: func(t *testing.T, _ envtest.World) core.Checkout { return checkout(t) },
		Spawn: func(t *testing.T, w envtest.World, x core.Execution) func() {
			cluster := clusterfake.New(w.(fakeworld.World).MemoryGates)
			r := newRig(t, w.(fakeworld.World), cluster, setup{})
			var wg sync.WaitGroup
			wg.Go(func() { _ = r.env.Run(context.Background(), x, io.Discard) })
			var once sync.Once
			kill := func() {
				once.Do(func() {
					// The caller's death ends its ssh connections, the holder's among them.
					r.client.Crash()
					r.local.Crash()
					wg.Wait()
				})
			}
			t.Cleanup(kill)
			return kill
		},
	})
}

func run(t testing.TB, e environment.Environment, argv ...string) (core.Attempt, string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	var out bytes.Buffer
	a := e.Run(ctx, core.Execution{Checkout: checkout(t), Argv: argv, Timeout: limit}, &out)
	return a, out.String()
}

func exited(t testing.TB, a core.Attempt, code int) {
	t.Helper()
	if a != (core.Ended{Exec: core.ExecOutcome{Kind: core.ExecExited, ExitCode: code}}) {
		t.Fatalf("attempt %+v, want an exit with code %d", a, code)
	}
}

func failure(t testing.TB, a core.Attempt) *core.Failure {
	t.Helper()
	switch a := a.(type) {
	case core.NotStarted:
		return &a.Failure
	case core.Unobserved:
		return &a.Failure
	}
	t.Fatalf("attempt %+v, want a failure", a)
	return nil
}

// A run asks for one allocation, named by its container and commented with the scope, never
// longer than its limit and the margin, sized as the operator said; its command runs on the node;
// and the allocation is gone, confirmed, when the run ends.
func TestARunHasAnAllocationOfItsOwnThatEndsWithIt(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	r := newRig(t, w, cluster, setup{})
	a, out := run(t, r.env, "print", "hello")
	exited(t, a, 0)
	if strings.TrimSpace(out) != "hello" {
		t.Errorf("output %q", out)
	}
	sub := cluster.Submitted()
	if len(sub) != 1 {
		t.Fatalf("allocations %+v, want one", sub)
	}
	got := sub[0]
	if !strings.HasPrefix(got.Name, apptainer.JobPrefix) || got.Comment != scope || got.Limit != limit+apptainer.Margin {
		t.Errorf("allocation %+v", got)
	}
	if !slices.Contains(got.Options, "--partition=part-a") || got.Options[len(got.Options)-1] != "--account=acct-a" {
		t.Errorf("options %v: the partition first, the operator's own arguments last", got.Options)
	}
	nothingLeft(t, cluster)
}

// A job waits in the queue, polled on the engine's timers, and runs once Slurm starts it.
func TestARunWaitsInTheQueue(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	cluster.Queue(3, false)
	r := newRig(t, w, cluster, setup{engineTimers: clock.Immediate{Longest: apptainer.DefaultPoll}})
	a, _ := run(t, r.env, "exit", "0")
	exited(t, a, 0)
	nothingLeft(t, cluster)
}

// A job that waits longer than the queue's bound is cancelled, and the run fails before its
// command with what Slurm said of the wait; a job Slurm ends before it runs fails the same way.
func TestAJobThatNeverRunsFailsTheRunSayingWhy(t *testing.T) {
	cases := []struct {
		name string
		ends bool
		want []string
	}{
		{"the queue's bound", false, []string{"did not start within", "PENDING, Priority"}},
		{"ended in the queue", true, []string{"ended before it ran", "FAILED"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			w := fakeworld.New()
			cluster := clusterfake.New(w.MemoryGates)
			// The bound fires at once and a poll never does, or a poll fires at once and the job ends.
			var engineTimers clock.Timers = clock.Exactly{D: apptainer.DefaultQueueTimeout, Others: envtest.NewTimers()}
			cluster.Queue(1_000_000, false)
			if c.ends {
				engineTimers = clock.Immediate{Longest: apptainer.DefaultPoll}
				cluster.Queue(0, true)
			}
			r := newRig(t, w, cluster, setup{engineTimers: engineTimers})
			a, _ := run(t, r.env, "mark", "ran")
			f := failure(t, a)
			for _, want := range c.want {
				if !strings.Contains(f.Message, want) {
					t.Errorf("failure %q lacks %q", f.Message, want)
				}
			}
			if w.Marked("ran") {
				t.Error("the command ran")
			}
			nothingLeft(t, cluster)
		})
	}
}

// A run first cancels what a dead run of its scope left, and nothing of another scope or of
// anything else.
func TestARunSweepsTheAllocationsADeadRunOfItsScopeLeft(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	dead := apptainer.JobName("vibesys-cli-deadbeef01")
	cluster.Plant(dead, scope)
	other := apptainer.JobName("vibesys-cli-0th3r5c0pe")
	cluster.Plant(other, "other")
	cluster.Plant("someone-elses-job", scope)
	r := newRig(t, w, cluster, setup{})
	a, _ := run(t, r.env, "exit", "0")
	exited(t, a, 0)
	if live := cluster.Live(); !slices.Equal(live, []string{"someone-elses-job", other}) {
		t.Errorf("live allocations %v, want only what this scope's runs did not leave", live)
	}
}

// An allocation whose end Slurm does not confirm leaves the run's ending unknown.
func TestAnAllocationNotConfirmedEndedMakesTheRunUnobserved(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	r := newRig(t, w, cluster, setup{})
	cluster.SetFaults(func(op string) error {
		if op == "cancel" {
			return &core.Failure{Kind: core.FailureUnavailable, Message: "slurmctld does not answer"}
		}
		return nil
	})
	a, _ := run(t, r.env, "exit", "0")
	if u, ok := a.(core.Unobserved); !ok || !strings.Contains(u.Failure.Message, "not confirmed ended") {
		t.Errorf("attempt %+v, want unobserved saying the allocation was not confirmed ended", a)
	}
}

// A process whose step was lost (the transport's own exit, a step that never started its command,
// or a SIF that is not there) is never a command's exit.
func TestALostStepIsNeverTheCommandsExit(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	r := newRig(t, w, cluster, setup{})
	cluster.SetFaults(func(op string) error {
		if op == "start" {
			return errors.New("ssh: exit 255")
		}
		return nil
	})
	a, _ := run(t, r.env, "mark", "ran")
	if _, ok := a.(core.Ended); ok {
		t.Fatalf("attempt %+v: a lost step was taken for the command's ending", a)
	}
	if w.Marked("ran") {
		t.Error("the command ran")
	}
	cluster.SetFaults(nil)
	nothingLeft(t, cluster)
}

func gzipped(t testing.TB, data []byte) []byte {
	t.Helper()
	var b bytes.Buffer
	gz, err := gzip.NewWriterLevel(&b, gzip.BestSpeed)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = gz.Write(data)
	if err := gz.Close(); err != nil {
		t.Fatal(err)
	}
	return b.Bytes()
}

// imageID is the ID the fake builds the rigs' task image with.
func imageID(t testing.TB) string {
	t.Helper()
	w := fakeworld.New()
	r := newRig(t, w, clusterfake.New(w.MemoryGates), setup{})
	f, err := r.env.Fingerprint(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return f.ImageDigest
}

func imagePaths(id string) (archive, size, sif string) {
	base := path.Join(root, "images", strings.TrimPrefix(id, "sha256:"))
	return base + ".tar.gz", base + ".size", base + ".sif"
}

// An image is copied to the cluster once, whole, named by its digest, with its size beside it, and
// converted once: a later run of another process copies and converts nothing, and a run that finds
// only the SIF copies nothing either.
func TestAnImageIsCopiedAndConvertedOnce(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	id := imageID(t)
	archive, size, sif := imagePaths(id)
	for process := range uint32(3) {
		r := newRig(t, w, cluster, setup{process: process})
		a, _ := run(t, r.env, "exit", "0")
		exited(t, a, 0)
	}
	if n := cluster.Published(archive); n != 1 {
		t.Errorf("the archive was published %d times, want once", n)
	}
	if n := cluster.Converts(); n != 1 {
		t.Errorf("%d conversions, want one", n)
	}
	got, _ := cluster.File(archive)
	if !bytes.Equal(got, gzipped(t, dockerfake.Archive(id))) {
		t.Errorf("the archive is not the image's, compressed")
	}
	if got, _ := cluster.File(size); strings.TrimSpace(string(got)) != fmt.Sprint(len(dockerfake.Archive(id))) {
		t.Errorf("size file %q", got)
	}

	// A cluster that has the SIF and no archive needs no copy.
	other := clusterfake.New(w.MemoryGates)
	other.Put(sif, []byte("a SIF"))
	r := newRig(t, w, other, setup{})
	a, _ := run(t, r.env, "exit", "0")
	exited(t, a, 0)
	if n := other.Published(archive); n != 0 || other.Converts() != 0 {
		t.Errorf("published %d archives and converted %d times with the SIF there", n, other.Converts())
	}
}

// Runs of several processes at once, on a cluster that has none of the image, each run, and the
// cluster ends with one whole archive and a SIF of it.
func TestRunsOfSeveralProcessesAtOnceShareTheImage(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	id := imageID(t)
	archive, _, sif := imagePaths(id)
	var wg sync.WaitGroup
	attempts := make([]core.Attempt, 4)
	for i := range attempts {
		r := newRig(t, w, cluster, setup{process: uint32(i)})
		wg.Go(func() { attempts[i], _ = run(t, r.env, "exit", "0") })
	}
	wg.Wait()
	for _, a := range attempts {
		exited(t, a, 0)
	}
	got, _ := cluster.File(archive)
	if !bytes.Equal(got, gzipped(t, dockerfake.Archive(id))) {
		t.Error("the archive is not whole")
	}
	if _, ok := cluster.File(sif); !ok {
		t.Error("no SIF")
	}
	nothingLeft(t, cluster)
}

// A node whose scratch space cannot hold the conversion fails the run before its command, naming
// the space needed and the space there is, and converts nothing.
func TestTooLittleScratchSpaceFailsTheRunNamingTheSpace(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	cluster.SetScratch(10)
	r := newRig(t, w, cluster, setup{})
	a, _ := run(t, r.env, "mark", "ran")
	f := failure(t, a)
	need := apptainer.Size(int64(apptainer.ScratchFactor * len(dockerfake.Archive(imageID(t)))))
	for _, want := range []string{"needs about " + need, apptainer.Size(10) + " free"} {
		if !strings.Contains(f.Message, want) {
			t.Errorf("failure %q lacks %q", f.Message, want)
		}
	}
	if f.Kind != core.FailureUnavailable || w.Marked("ran") || cluster.Converts() != 0 {
		t.Errorf("failure %+v, ran %v, converts %d", f, w.Marked("ran"), cluster.Converts())
	}
	nothingLeft(t, cluster)
}

// The machine and its load are read on the node, inside the allocation: before a process of the
// scope ran there is no machine to say, and after one ran every process of the scope says that
// node's machine, while the load is only ever a process's own node's.
func TestTheMachineIsTheNodesReadInsideTheAllocation(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	first := newRig(t, w, cluster, setup{})
	b := first.engine.Backend()
	ctx := context.Background()
	if m, err := b.Machine(ctx); err != nil || m != (core.MachineFacts{}) {
		t.Fatalf("before any node: %+v, %v", m, err)
	}
	if _, err := b.Load(ctx); err == nil {
		t.Fatal("a load before any node")
	}
	a, _ := run(t, first.env, "exit", "0")
	exited(t, a, 0)
	m, err := b.Machine(ctx)
	if err != nil || m.HostID != "node-a" || m.LogicalCPUs != 2 || m.Arch != "amd64" {
		t.Fatalf("machine %+v, %v", m, err)
	}
	if l, err := b.Load(ctx); err != nil || l.LoadAverage1m != 0.5 {
		t.Errorf("load %+v, %v", l, err)
	}
	second := newRig(t, w, cluster, setup{process: 1}).engine.Backend()
	if got, err := second.Machine(ctx); err != nil || got != m {
		t.Errorf("another process of the scope: %+v, %v, want %+v", got, err, m)
	}
	if _, err := second.Load(ctx); err == nil {
		t.Error("another process read a load of a node it never ran on")
	}
}

// Facts reads a node script's output from the same reading, and says the facts are unread before
// any node was.
func TestFactsAreReadFromTheNodesScript(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	cluster.Say("gpu", "model=MI210 count=4")
	r := newRig(t, w, cluster, setup{scripts: map[string]string{"gpu": "rocminfo"}})
	parse := func(out string) (map[string]string, error) {
		facts := map[string]string{}
		for _, kv := range strings.Fields(out) {
			k, v, _ := strings.Cut(kv, "=")
			facts[k] = v
		}
		return facts, nil
	}
	f := apptainer.Facts{Engine: r.engine, Script: "gpu", Parse: parse, Unread: map[string]string{"model": apptainer.Unread}}
	got, err := f.GPUFacts(context.Background())
	if err != nil || got["model"] != apptainer.Unread {
		t.Fatalf("before any node: %v, %v", got, err)
	}
	a, _ := run(t, r.env, "exit", "0")
	exited(t, a, 0)
	got, err = f.GPUFacts(context.Background())
	if err != nil || got["model"] != "MI210" || got["count"] != "4" {
		t.Errorf("facts %v, %v", got, err)
	}
}

// candidateFiles is the candidate's commit, as the daemon reads it.
type candidateFiles struct{}

func (candidateFiles) Candidate(_ context.Context, _ environment.Target, b core.CandidateBuild) (task.CandidateContext, error) {
	text := "FROM busybox:1.36.1\nCOPY . /srv\n"
	sum := sha256.Sum256([]byte(text))
	return task.CandidateContext{Dockerfile: []byte(text), Files: task.Tree{"Dockerfile": {Data: []byte(text)}}, Digest: hex.EncodeToString(sum[:])}, nil
}

// candidateRun runs command beside a server that runs serve, firing the probes' pauses as they are
// armed, and act once the run started.
func candidateRun(t *testing.T, serve, command []string, act func(*envtest.Timers)) (core.Attempt, string, *clusterfake.Cluster) {
	t.Helper()
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	timers := envtest.NewTimers()
	r := newRig(t, w, cluster, setup{timers: timers, configure: func(c *docker.Config) {
		c.Candidates, c.Probe, c.ProbePause = candidateFiles{}, probeProgram, probePause
	}})
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	img, err := r.env.Candidate(ctx, core.CandidateBuild{Dockerfile: "serve/Dockerfile", Context: "serve"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	x := core.Execution{
		Checkout: checkout(t), Argv: command, Timeout: limit,
		Candidate: &core.CandidateRun{Image: img.ID, Serve: &core.CandidateServe{Argv: serve, Ready: "/health", StartupTimeout: startup}},
	}
	var (
		wg  sync.WaitGroup
		out bytes.Buffer
		a   core.Attempt
	)
	pumping, stop := context.WithCancel(ctx)
	wg.Go(func() {
		defer stop()
		a = r.env.Run(ctx, x, &out)
	})
	wg.Go(func() {
		for n := 1; ; n++ {
			timer := timers.Await(pumping, n)
			if timer == nil {
				return
			}
			if timer.Duration == probePause {
				timer.Fire()
			}
		}
	})
	if act != nil {
		act(timers)
	}
	wg.Wait()
	return a, out.String(), cluster
}

// fire fires the first timer armed for d.
func fire(t *testing.T, timers *envtest.Timers, d time.Duration) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	for n := 1; ; n++ {
		timer := timers.Await(ctx, n)
		if timer == nil {
			t.Fatalf("no timer of %s was armed", d)
		}
		if timer.Duration == d {
			timer.Fire()
			return
		}
	}
}

// The command's container joins the server's allocation, so both run on one node and share its
// network: the command waits for the server's probe on a port the node chose, then runs.
func TestATaskOfTwoImagesRunsBothInOneAllocation(t *testing.T) {
	a, out, cluster := candidateRun(t, []string{"ready", ";", "hold", "forever"}, []string{"print", "done"}, nil)
	exited(t, a, 0)
	if !strings.Contains(out, "done") {
		t.Errorf("output %q", out)
	}
	if n := len(cluster.Submitted()); n != 1 {
		t.Errorf("%d allocations for a task of two images, want one", n)
	}
	names := map[string]bool{}
	for _, b := range cluster.Started() {
		names[b.Name] = true
		if !strings.HasPrefix(b.Image, root+"/images/") || !strings.HasSuffix(b.Image, ".sif") {
			t.Errorf("box %s runs %q, want a SIF under the root", b.Name, b.Image)
		}
	}
	if len(names) != 2 {
		t.Errorf("boxes %v, want the server's and the command's", names)
	}
	nothingLeft(t, cluster)
}

// A server that is never ready ends the run at its startup bound, and nothing is left.
func TestAServerNeverReadyEndsTheRunAtItsStartupBound(t *testing.T) {
	a, _, cluster := candidateRun(t, []string{"mark", "serving", ";", "hold", "forever"}, []string{"exit", "0"},
		func(timers *envtest.Timers) { fire(t, timers, startup) })
	e, ok := a.(core.Ended)
	if !ok || e.Exec.Kind != core.ExecServiceNotReady {
		t.Fatalf("attempt %+v, want a server that was not ready", a)
	}
	nothingLeft(t, cluster)
}

// A container is refused what a cluster's node cannot give it: NVIDIA GPUs and a network of its
// own.
func TestTheEngineRefusesWhatANodeCannotGive(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	r := newRig(t, w, cluster, setup{})
	base := docker.ContainerSpec{Name: "vibesys-cli-0123456789", Image: "sha256:" + strings.Repeat("0", 64), Dir: "/work"}
	for _, change := range []func(*docker.ContainerSpec){
		func(s *docker.ContainerSpec) { s.GPUs = docker.GPUs{Devices: "device=0", Runtime: "nvidia"} },
		func(s *docker.ContainerSpec) { s.Network = "bridge" },
	} {
		spec := base
		change(&spec)
		if _, err := r.engine.Start(context.Background(), spec); err == nil {
			t.Errorf("started %+v", spec)
		}
	}
	if len(cluster.Submitted()) != 0 {
		t.Error("a refused container asked for an allocation")
	}
}
