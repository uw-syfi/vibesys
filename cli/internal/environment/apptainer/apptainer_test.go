package apptainer_test

import (
	"bytes"
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
	"sync/atomic"
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
	localDir      = "/local/scratch"
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
	// converter is the machine's apptainer; nil is one of the rig's own with room for any image.
	converter *clusterfake.Converter
	// daemon configures the rig's local Docker daemon.
	daemon []dockerfake.Option
	// scope is the rig's; empty is the tests' scope. A scope is one process's: a run sweeps what
	// every other process of its scope left, as a dead one's.
	scope string
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
	if o.scope == "" {
		o.scope = scope
	}
	if o.converter == nil {
		o.converter = clusterfake.NewConverter()
	}
	local := dockerfake.NewDaemon(w.MemoryGates, o.daemon...).Connect()
	client := cluster.Connect()
	eng, err := apptainer.New(apptainer.Config{
		Local: local, Export: local, Cluster: client, Root: root, Scope: o.scope,
		Timers: o.engineTimers, Clock: clock.NewManual(time.Unix(1_700_000_000, 0), time.Second),
		Sizing:      apptainer.Sizing{Partition: "part-a", Arguments: []string{"--account=acct-a"}},
		NodeScripts: o.scripts, Converter: o.converter, LocalDir: localDir,
	})
	if err != nil {
		t.Fatal(err)
	}
	cfg := docker.Config{
		Engine: eng, Source: dockerfake.Fixed(docker.Spec{Dockerfile: []byte(dockerfile)}), Backend: eng.Backend(),
		Timers: o.timers, IDs: dockerfake.NewIDs(o.process), Scope: o.scope, Placement: "default",
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
	if !strings.HasPrefix(got.Name, apptainer.JobPrefix) || got.Comment != scope || got.Limit != limit+apptainer.StopMargin {
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

// imagesOn are the files of the cluster's images directory.
func imagesOn(c *clusterfake.Cluster) []string {
	var out []string
	for _, f := range c.Files() {
		if strings.HasPrefix(f, root+"/images/") {
			out = append(out, f)
		}
	}
	return out
}

// sifOf is where the cluster keeps the SIF of the image of the ID, which the local engine has: the
// SIF of the image's content.
func sifOf(t testing.TB, local *dockerfake.Client, id string) string {
	t.Helper()
	content, err := local.Content(context.Background(), id)
	if err != nil {
		t.Fatal(err)
	}
	return path.Join(root, "images", strings.TrimPrefix(content, "sha256:")+".sif")
}

// taskSIF is where the cluster keeps the SIF of the rigs' task image.
func taskSIF(t testing.TB) string {
	t.Helper()
	w := fakeworld.New()
	r := newRig(t, w, clusterfake.New(w.MemoryGates), setup{})
	f, err := r.env.Fingerprint(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return sifOf(t, r.local, f.ImageDigest)
}

// An image is converted to a SIF on this machine, in its local directory, and the SIF is copied to
// the cluster once, named by its digest, with what built it beside it; the containers run it. A
// later run of another process converts and copies nothing, and nothing but the SIF and what built
// it is put on the cluster: no archive.
func TestAnImageIsConvertedHereAndItsSIFCopiedOnce(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	converter := clusterfake.NewConverter()
	id := imageID(t)
	sif := taskSIF(t)
	for process := range uint32(3) {
		r := newRig(t, w, cluster, setup{process: process, converter: converter})
		a, _ := run(t, r.env, "exit", "0")
		exited(t, a, 0)
	}
	if builds := converter.Builds(); !slices.Equal(builds, []string{localDir}) {
		t.Errorf("builds in %v, want one in %s", builds, localDir)
	}
	if n := cluster.Published(sif); n != 1 {
		t.Errorf("the SIF was published %d times, want once", n)
	}
	if got, _ := cluster.File(sif); !bytes.Equal(got, clusterfake.SIF(dockerfake.Archive(id))) {
		t.Errorf("the SIF %q is not the image's", got)
	}
	if got, _ := cluster.File(sif + apptainer.BuiltBy); strings.TrimSpace(string(got)) != clusterfake.Version {
		t.Errorf("built by %q", got)
	}
	if files := imagesOn(cluster); !slices.Equal(files, []string{sif, sif + apptainer.BuiltBy}) {
		t.Errorf("files on the cluster %v", files)
	}
	for _, b := range cluster.Started() {
		if b.Image != sif {
			t.Errorf("a container ran %s, want %s", b.Image, sif)
		}
	}
	nothingLeft(t, cluster)
}

// An image built for a run is converted as part of its build, whose log says what converted it,
// with its version.
func TestTheBuildsLogSaysWhatConvertedTheImage(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	r := newRig(t, w, cluster, setup{})
	buildContext, err := docker.Context(docker.Spec{Dockerfile: []byte(dockerfile)})
	if err != nil {
		t.Fatal(err)
	}
	var log bytes.Buffer
	if err := r.engine.Build(context.Background(), docker.BuildSpec{Tag: "vibesys-task:x", Context: buildContext}, &log); err != nil {
		t.Fatal(err)
	}
	img, ok, err := r.engine.Image(context.Background(), "vibesys-task:x")
	if err != nil || !ok {
		t.Fatalf("image %v, %v", ok, err)
	}
	if !strings.Contains(log.String(), clusterfake.Version) || !strings.Contains(log.String(), sifOf(t, r.local, img.ID)) {
		t.Errorf("the build's log %q does not say what converted the image to what", log.String())
	}
	if _, ok := cluster.File(sifOf(t, r.local, img.ID)); !ok {
		t.Error("the build copied no SIF to the cluster")
	}
}

// Runs of several processes at once, on machines of their own and of scopes of their own, on a
// cluster that has no SIF of the image, each run, and the cluster ends with one whole SIF.
func TestRunsOfSeveralProcessesAtOnceShareTheImage(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	id := imageID(t)
	var wg sync.WaitGroup
	attempts := make([]core.Attempt, 4)
	for i := range attempts {
		r := newRig(t, w, cluster, setup{process: uint32(i), scope: fmt.Sprintf("scope-%d", i)})
		wg.Go(func() { attempts[i], _ = run(t, r.env, "exit", "0") })
	}
	wg.Wait()
	for _, a := range attempts {
		exited(t, a, 0)
	}
	if got, _ := cluster.File(taskSIF(t)); !bytes.Equal(got, clusterfake.SIF(dockerfake.Archive(id))) {
		t.Errorf("the SIF %q is not whole", got)
	}
	nothingLeft(t, cluster)
}

// A conversion needs the image unpacked, and its archive and SIF, each about as large as its
// stored layers, free in the local directory: with exactly that it runs, and with a byte less the
// run fails before its command, naming the space needed, the directory and the space there is,
// and nothing is converted or copied.
func TestAConversionIsCheckedAgainstTheLocalDirectorysSpace(t *testing.T) {
	n := int64(len(dockerfake.Archive(imageID(t))))
	need := dockerfake.UnpackedFactor*n + 2*n
	w := fakeworld.New()
	converter := clusterfake.NewConverter()
	converter.SetFree(need)
	cluster := clusterfake.New(w.MemoryGates)
	a, _ := run(t, newRig(t, w, cluster, setup{converter: converter}).env, "exit", "0")
	exited(t, a, 0)

	w = fakeworld.New()
	converter = clusterfake.NewConverter()
	converter.SetFree(need - 1)
	cluster = clusterfake.New(w.MemoryGates)
	a, _ = run(t, newRig(t, w, cluster, setup{converter: converter}).env, "mark", "ran")
	f := failure(t, a)
	for _, want := range []string{"needs about " + apptainer.Size(need), localDir, apptainer.Size(need-1) + " free", "local_scratch_dir"} {
		if !strings.Contains(f.Message, want) {
			t.Errorf("failure %q lacks %q", f.Message, want)
		}
	}
	if f.Kind != core.FailureUnavailable || w.Marked("ran") || len(converter.Builds()) != 0 || len(imagesOn(cluster)) != 0 {
		t.Errorf("failure %+v, ran %v, builds %v, files %v", f, w.Marked("ran"), converter.Builds(), imagesOn(cluster))
	}
	nothingLeft(t, cluster)
}

// A conversion that fails, or that does not end within its bound, fails the run before its command
// saying why, and leaves no SIF on the cluster, so the next run converts again.
func TestAFailedConversionLeavesNoSIF(t *testing.T) {
	cases := []struct {
		name  string
		fault error
		// engineTimers fire the conversion's bound at once, and nothing else.
		timers clock.Timers
		want   string
	}{
		{"failed", &core.Failure{Kind: core.FailureUnavailable, Message: "FATAL: unpacking failed"}, nil, "FATAL: unpacking failed"},
		{"too slow", clusterfake.ErrHang, clock.Exactly{D: apptainer.DefaultConvertTimeout, Others: envtest.NewTimers()}, "convert_timeout_seconds"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			w := fakeworld.New()
			cluster := clusterfake.New(w.MemoryGates)
			converter := clusterfake.NewConverter()
			converter.SetFaults(func(op string) error {
				if op == "build" {
					return c.fault
				}
				return nil
			})
			r := newRig(t, w, cluster, setup{converter: converter, engineTimers: c.timers})
			a, _ := run(t, r.env, "mark", "ran")
			f := failure(t, a)
			if !strings.Contains(f.Message, c.want) || w.Marked("ran") {
				t.Errorf("failure %q, want one saying %q; ran %v", f.Message, c.want, w.Marked("ran"))
			}
			if files := imagesOn(cluster); len(files) != 0 {
				t.Errorf("files on the cluster %v", files)
			}
			nothingLeft(t, cluster)
		})
	}
}

// build builds the tests' image with the rig's engine, which converts it, and returns what the
// build logged.
func build(ctx context.Context, r *rig) (string, error) {
	buildContext, err := docker.Context(docker.Spec{Dockerfile: []byte(dockerfile)})
	if err != nil {
		return "", err
	}
	var log bytes.Buffer
	err = r.engine.Build(ctx, docker.BuildSpec{Tag: "vibesys-task:x", Context: buildContext}, &log)
	return log.String(), err
}

// receive returns what ch gives, failing the test if ctx, its bound against a hang, ends first.
func receive[T any](ctx context.Context, t *testing.T, ch <-chan T, what string) T {
	t.Helper()
	select {
	case v := <-ch:
		return v
	case <-ctx.Done():
		t.Fatalf("%s never happened", what)
		var zero T
		return zero
	}
}

// heldConversion is a converter of one machine whose first build, once it started, waits for the
// test to end it with the error it sends on release.
type heldConversion struct {
	converter *clusterfake.Converter
	started   chan struct{}
	release   chan error
}

func holdFirstConversion() *heldConversion {
	h := &heldConversion{converter: clusterfake.NewConverter(), started: make(chan struct{}, 1), release: make(chan error, 1)}
	var first sync.Once
	h.converter.SetFaults(func(op string) error {
		if op != "build" {
			return nil
		}
		var err error
		held := false
		first.Do(func() {
			held = true
			h.started <- struct{}{}
			err = <-h.release
		})
		if held {
			return err
		}
		return nil
	})
	return h
}

// Two processes of one machine that publish one image at once convert it once: the second finds
// the first converting it, says so in its build's log, waits for it on the engine's poll, and then
// uses the SIF the first copied, which is copied once. When the first conversion fails, the one
// that waited converts the image itself. Every lock is let go after.
func TestPublishersOfOneImageOnOneMachineConvertItOnce(t *testing.T) {
	cases := []struct {
		name   string
		first  error
		builds int
	}{
		{"the first converts", nil, 1},
		{"the first fails", &core.Failure{Kind: core.FailureUnavailable, Message: "FATAL: unpacking failed"}, 1},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
			defer cancel()
			w := fakeworld.New()
			cluster := clusterfake.New(w.MemoryGates)
			machine := holdFirstConversion()
			sif := taskSIF(t)
			first := newRig(t, w, cluster, setup{process: 0, converter: machine.converter})
			waiterTimers := envtest.NewTimers()
			second := newRig(t, w, cluster, setup{process: 1, converter: machine.converter, engineTimers: waiterTimers})

			firstDone := make(chan error, 1)
			go func() { _, err := build(ctx, first); firstDone <- err }()
			receive(ctx, t, machine.started, "the first conversion's start")
			type result struct {
				log string
				err error
			}
			secondDone := make(chan result, 1)
			go func() { log, err := build(ctx, second); secondDone <- result{log, err} }()
			// The second waits: its bound of the wait, then its pause before it tries again.
			pause := waiterTimers.Await(ctx, 2)
			if pause == nil || pause.Duration != apptainer.DefaultPoll {
				t.Fatalf("the second publisher did not pause to wait for the first: %+v", pause)
			}
			machine.release <- c.first
			if err := receive(ctx, t, firstDone, "the first build's end"); (err != nil) != (c.first != nil) {
				t.Fatalf("the first build: %v, want the error %v", err, c.first)
			}
			pause.Fire()
			got := receive(ctx, t, secondDone, "the second build's end")
			if got.err != nil {
				t.Fatalf("the second build: %v", got.err)
			}
			if !strings.Contains(got.log, "another process of this machine is converting") {
				t.Errorf("the second build's log %q does not say it waited for another conversion", got.log)
			}
			if builds := machine.converter.Builds(); len(builds) != c.builds {
				t.Errorf("builds %v, want %d", builds, c.builds)
			}
			if n := cluster.Published(sif); n != 1 {
				t.Errorf("the SIF was published %d times, want once", n)
			}
			if held := machine.converter.Held(); len(held) != 0 {
				t.Errorf("locks held after: %v", held)
			}
		})
	}
}

// A publisher that waits longer than ConvertTimeout for another conversion of its image on this
// machine fails saying so, and converts nothing.
func TestAWaitForAnotherConversionIsBounded(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	machine := holdFirstConversion()
	first := newRig(t, w, cluster, setup{process: 0, converter: machine.converter})
	second := newRig(t, w, cluster, setup{
		process: 1, converter: machine.converter,
		engineTimers: clock.Exactly{D: apptainer.DefaultConvertTimeout, Others: envtest.NewTimers()},
	})
	firstDone := make(chan error, 1)
	go func() { _, err := build(ctx, first); firstDone <- err }()
	receive(ctx, t, machine.started, "the first conversion's start")
	_, err := build(ctx, second)
	if err == nil || !strings.Contains(err.Error(), "stopped waiting") || !strings.Contains(err.Error(), "convert_timeout_seconds") {
		t.Errorf("the second build: %v, want one saying it stopped waiting at its bound", err)
	}
	machine.release <- nil
	if err := receive(ctx, t, firstDone, "the first build's end"); err != nil {
		t.Fatal(err)
	}
	if builds := machine.converter.Builds(); len(builds) != 1 {
		t.Errorf("builds %v, want the first's only", builds)
	}
	if held := machine.converter.Held(); len(held) != 0 {
		t.Errorf("locks held after: %v", held)
	}
}

// losingCopies makes the first n copies to the cluster lose their connection, as ssh's exit 255
// does, and counts every copy.
func losingCopies(cluster *clusterfake.Cluster, n int, err error) *atomic.Int64 {
	var copies atomic.Int64
	cluster.SetFaults(func(op string) error {
		if op != "publish" {
			return nil
		}
		if copies.Add(1) <= int64(n) {
			return err
		}
		return nil
	})
	return &copies
}

// lost is a copy's failure whose connection to the cluster was lost.
var lost = fmt.Errorf("%w (%w)", &core.Failure{Kind: core.FailureUnavailable, Message: "copying failed: exit status 255"}, apptainer.ErrConnectionLost)

// A copy of a SIF that loses its connection to the cluster is made again, on the engine's poll, of
// the SIF the conversion kept, each attempt said in the build's log, and the image is converted
// once. Once the cluster has the SIF, the kept one is removed.
func TestACopyThatLosesItsConnectionIsMadeAgain(t *testing.T) {
	for losses := 1; losses < apptainer.CopyAttempts; losses++ {
		t.Run(fmt.Sprint(losses), func(t *testing.T) {
			w := fakeworld.New()
			cluster := clusterfake.New(w.MemoryGates)
			converter := clusterfake.NewConverter()
			r := newRig(t, w, cluster, setup{converter: converter, engineTimers: clock.Immediate{Longest: apptainer.DefaultPoll}})
			copies := losingCopies(cluster, losses, lost)
			sif := taskSIF(t)
			log, err := build(context.Background(), r)
			if err != nil {
				t.Fatalf("the build: %v", err)
			}
			if n := strings.Count(log, "lost its connection to the cluster"); n != losses {
				t.Errorf("the build's log says %d copies lost their connection, want %d: %q", n, losses, log)
			}
			if got := copies.Load(); got != int64(losses)+2 {
				t.Errorf("%d copies, want %d of the SIF and one of what built it", got, losses+2)
			}
			if builds := converter.Builds(); len(builds) != 1 {
				t.Errorf("builds %v, want one", builds)
			}
			if got, _ := cluster.File(sif); !bytes.Equal(got, clusterfake.SIF(dockerfake.Archive(imageID(t)))) {
				t.Errorf("the SIF %q is not the image's", got)
			}
			if kept := converter.Kept(); len(kept) != 0 {
				t.Errorf("kept after the copy: %v", kept)
			}
		})
	}
}

// A copy that loses its connection CopyAttempts times fails the build saying so, and keeps the
// SIF on this machine: the next publisher of the image, of another process, copies it without
// converting the image again, and then removes it.
func TestASIFNeverCopiedIsKeptForTheNextPublisher(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	converter := clusterfake.NewConverter()
	timers := clock.Immediate{Longest: apptainer.DefaultPoll}
	sif := taskSIF(t)
	copies := losingCopies(cluster, apptainer.CopyAttempts, lost)
	_, err := build(context.Background(), newRig(t, w, cluster, setup{process: 0, converter: converter, engineTimers: timers}))
	if err == nil || !errors.Is(err, apptainer.ErrConnectionLost) || !strings.Contains(err.Error(), fmt.Sprintf("%d times", apptainer.CopyAttempts)) {
		t.Fatalf("the build: %v, want one saying the copy lost its connection %d times", err, apptainer.CopyAttempts)
	}
	if got := copies.Load(); got != apptainer.CopyAttempts {
		t.Errorf("%d copies, want %d", got, apptainer.CopyAttempts)
	}
	if kept := converter.Kept(); len(kept) != 1 {
		t.Fatalf("kept %v, want the SIF", kept)
	}
	cluster.SetFaults(nil)
	log, err := build(context.Background(), newRig(t, w, cluster, setup{process: 1, converter: converter, engineTimers: timers}))
	if err != nil {
		t.Fatalf("the next build: %v", err)
	}
	if builds := converter.Builds(); len(builds) != 1 {
		t.Errorf("builds %v, want the first only", builds)
	}
	if !strings.Contains(log, "earlier conversion kept") {
		t.Errorf("the next build's log %q does not say it used the kept SIF", log)
	}
	if got, _ := cluster.File(sif); !bytes.Equal(got, clusterfake.SIF(dockerfake.Archive(imageID(t)))) {
		t.Errorf("the SIF %q is not the image's", got)
	}
	if kept := converter.Kept(); len(kept) != 0 {
		t.Errorf("kept after the copy: %v", kept)
	}
}

// A copy that fails for another reason than its connection is not made again.
func TestACopyThatFailsOtherwiseIsNotMadeAgain(t *testing.T) {
	w := fakeworld.New()
	cluster := clusterfake.New(w.MemoryGates)
	r := newRig(t, w, cluster, setup{engineTimers: clock.Immediate{Longest: apptainer.DefaultPoll}})
	full := &core.Failure{Kind: core.FailureUnavailable, Message: "No space left on device"}
	copies := losingCopies(cluster, apptainer.CopyAttempts, full)
	if _, err := build(context.Background(), r); err == nil || !strings.Contains(err.Error(), full.Message) {
		t.Errorf("the build: %v, want %v", err, full)
	}
	if got := copies.Load(); got != 1 {
		t.Errorf("%d copies, want one", got)
	}
}

// Builds of one Dockerfile whose images have IDs of their own and one content, as BuildKit's
// attestations make them under the containerd image store, share one SIF: the image is converted
// and copied once, and every container of either runs that SIF. Builds of different content
// (a Dockerfile that downloads what changes) each have a SIF of their own.
func TestImagesOfOneContentShareOneSIF(t *testing.T) {
	cases := []struct {
		name   string
		daemon dockerfake.Option
		sifs   int
	}{
		{"one content, IDs of their own", dockerfake.AttestedBuilds(), 1},
		{"contents of their own", dockerfake.NonReproducibleBuilds(), 2},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			w := fakeworld.New()
			cluster := clusterfake.New(w.MemoryGates)
			converter := clusterfake.NewConverter()
			r := newRig(t, w, cluster, setup{converter: converter, daemon: []dockerfake.Option{c.daemon}})
			ids := map[string]bool{}
			var sifs []string
			for range 2 {
				if _, err := build(context.Background(), r); err != nil {
					t.Fatal(err)
				}
				img, ok, err := r.engine.Image(context.Background(), "vibesys-task:x")
				if err != nil || !ok {
					t.Fatalf("image %v, %v", ok, err)
				}
				ids[img.ID] = true
				sifs = append(sifs, sifOf(t, r.local, img.ID))
			}
			if len(ids) != 2 {
				t.Fatalf("the builds made the images %v, want two IDs", ids)
			}
			if builds := converter.Builds(); len(builds) != c.sifs {
				t.Errorf("%d conversions, want %d", len(builds), c.sifs)
			}
			if got := len(imagesOn(cluster)); got != 2*c.sifs {
				t.Errorf("files on the cluster %v, want %d SIFs and what built each", imagesOn(cluster), c.sifs)
			}
			for _, sif := range sifs {
				if n := cluster.Published(sif); n != 1 {
					t.Errorf("%s was published %d times, want once", sif, n)
				}
			}
		})
	}
}

// An engine needs a converter and an absolute local directory.
func TestAnEngineNeedsAConverterAndALocalDirectory(t *testing.T) {
	w := fakeworld.New()
	local := dockerfake.NewDaemon(w.MemoryGates).Connect()
	base := apptainer.Config{
		Local: local, Export: local, Cluster: clusterfake.New(w.MemoryGates).Connect(), Root: root, Scope: scope,
		Timers: envtest.NewTimers(), Clock: clock.NewManual(time.Unix(1_700_000_000, 0), time.Second),
		Converter: clusterfake.NewConverter(), LocalDir: localDir,
	}
	if _, err := apptainer.New(base); err != nil {
		t.Fatal(err)
	}
	for name, change := range map[string]func(*apptainer.Config){
		"no converter": func(c *apptainer.Config) { c.Converter = nil },
		"relative":     func(c *apptainer.Config) { c.LocalDir = "scratch" },
		"root":         func(c *apptainer.Config) { c.LocalDir = "/" },
	} {
		cfg := base
		change(&cfg)
		_, err := apptainer.New(cfg)
		if f := core.AsFailure(err); err == nil || f.Kind != core.FailureInvalidInput {
			t.Errorf("%s: %v", name, err)
		}
	}
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
