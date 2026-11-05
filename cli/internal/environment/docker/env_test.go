package docker_test

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"github.com/uw-syfi/vibesys/cli/internal/lease/fakelease"
	"math/rand/v2"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
	"github.com/uw-syfi/vibesys/cli/internal/resource/fakeresource"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// scene is a fake daemon, the world its scripts run in, and a docker environment over them.
type scene struct {
	t      *testing.T
	world  fakeworld.World
	daemon *dockerfake.Daemon
	r      *rig
	env    environment.Environment
}

func newScene(t *testing.T, o options) *scene {
	t.Helper()
	w := fakeworld.New()
	d := dockerfake.NewDaemon(w.MemoryGates)
	r := newRig(t, d, o)
	return &scene{t: t, world: w, daemon: d, r: r, env: r.env(t)}
}

// run runs a script to its end.
func (s *scene) run(script ...string) (core.Attempt, string) {
	s.t.Helper()
	var out bytes.Buffer
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	attempt := s.env.Run(ctx, core.Execution{Checkout: checkout(s.t), Argv: script, Timeout: time.Hour}, &out)
	return attempt, out.String()
}

func (s *scene) mustRun(script ...string) string {
	s.t.Helper()
	attempt, out := s.run(script...)
	if e, ok := attempt.(core.Ended); !ok || e.Exec.Kind != core.ExecExited || e.Exec.ExitCode != 0 {
		s.t.Fatalf("run %v: %+v", script, attempt)
	}
	return out
}

func (s *scene) fingerprint() core.Fingerprint {
	s.t.Helper()
	f, err := s.env.Fingerprint(context.Background())
	if err != nil {
		s.t.Fatal(err)
	}
	return f
}

var boom = &core.Failure{Kind: core.FailureUnavailable, Message: "docker: injected failure"}

func TestAnUnchangedTaskBuildsItsImageOnce(t *testing.T) {
	s := newScene(t, options{})
	s.fingerprint()
	s.mustRun("print", "a")
	s.mustRun("print", "b")
	s.fingerprint()
	if n := s.daemon.Builds(); n != 1 {
		t.Fatalf("%d builds for one unchanged Dockerfile, want 1", n)
	}
	// A task whose Dockerfile changed builds a new image, and the old one stays.
	other := newRig(t, s.daemon, options{spec: docker.Spec{Dockerfile: []byte("FROM busybox:1.36.1\nRUN true # changed\n")}, process: 2})
	e := other.env(t)
	f1, err := e.Fingerprint(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if n := s.daemon.Builds(); n != 2 {
		t.Fatalf("%d builds after the Dockerfile changed, want 2", n)
	}
	if f1.Equal(s.fingerprint()) {
		t.Fatalf("two Dockerfiles have the same fingerprint %v", f1)
	}
}

func TestTheFingerprintPinsTheImageAndTheMachine(t *testing.T) {
	s := newScene(t, options{})
	f := s.fingerprint()
	if f.Kind != core.EnvironmentDocker || f.Backend != core.BackendCPU || !strings.HasPrefix(f.ImageDigest, "sha256:") || len(f.ImageDigest) != 71 {
		t.Fatalf("fingerprint %v is not a docker fingerprint pinned to an image digest", f)
	}
	for _, key := range []string{"os", "arch", "cpu_model", "logical_cpus", "image_platform"} {
		if f.Facts[key] == "" {
			t.Errorf("the fingerprint has no fact %q: %v", key, f)
		}
	}
	// The same Dockerfile built again is the same image, so a rebuilt environment is comparable.
	again := newRig(t, dockerfake.NewDaemon(s.world.MemoryGates), options{process: 2}).env(t)
	if g, err := again.Fingerprint(context.Background()); err != nil || !g.Equal(f) {
		t.Errorf("the same Dockerfile built on another daemon: %v, %v; want %v", g, err, f)
	}
	// An image that is not reproducible is told apart by its digest.
	d := dockerfake.NewDaemon(s.world.MemoryGates, dockerfake.NonReproducibleBuilds())
	e := newRig(t, d, options{}).env(t)
	first, _ := e.Fingerprint(context.Background())
	d.Prune()
	second, _ := e.Fingerprint(context.Background())
	if first.Equal(second) {
		t.Errorf("a rebuilt image that differs has the same fingerprint %v", first)
	}
}

func TestARunStartsItsContainerAsTheUserInTheCheckout(t *testing.T) {
	s := newScene(t, options{})
	f := s.fingerprint()
	x := core.Execution{Checkout: checkout(t), Scratch: "/memory/scratch-1", Argv: []string{"mark", "up", ";", "hold", "go"}, Timeout: time.Hour}
	done := make(chan struct{})
	var wg sync.WaitGroup
	wg.Go(func() {
		defer close(done)
		if a, ok := s.env.Run(context.Background(), x, &bytes.Buffer{}).(core.Ended); !ok {
			t.Errorf("Run: %+v", a)
		}
	})
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	if !s.world.Reached(ctx, "up") {
		t.Fatal("the command never started")
	}
	boxes := s.daemon.Containers()
	if len(boxes) != 1 || !boxes[0].Running {
		t.Fatalf("containers %+v, want exactly one, running", boxes)
	}
	spec := boxes[0].Spec
	if !strings.HasPrefix(spec.Name, "vibesys-cli-") || spec.Image != f.ImageDigest {
		t.Errorf("container %q of image %q, want a vibesys-cli- name and the fingerprint's image %q", spec.Name, spec.Image, f.ImageDigest)
	}
	if spec.User != (docker.User{UID: 1000, GID: 1000}) || spec.Dir != x.Checkout.Dir {
		t.Errorf("runs as %+v in %q, want the invoking user in the checkout %q", spec.User, spec.Dir, x.Checkout.Dir)
	}
	wantMounts := []docker.Mount{{Host: x.Checkout.Dir, Container: x.Checkout.Dir}, {Host: x.Scratch, Container: x.Scratch}}
	if !slices.Equal(spec.Mounts, wantMounts) {
		t.Errorf("mounts %+v, want the checkout and the scratch directory, each at its own path, writable", spec.Mounts)
	}
	for k, want := range map[string]string{docker.LabelOwner: docker.Owner, docker.LabelScope: "test"} {
		if spec.Labels[k] != want {
			t.Errorf("label %s = %q, want %q", k, spec.Labels[k], want)
		}
	}
	if spec.Labels[docker.LabelRun] == "" || !strings.HasSuffix(spec.Name, spec.Labels[docker.LabelRun]) {
		t.Errorf("labels %v do not name the run %q belongs to", spec.Labels, spec.Name)
	}
	if spec.Env["HOME"] == "" {
		t.Errorf("no HOME for a user the image does not know")
	}
	s.world.Release("go")
	<-done
	wg.Wait()
	if left := s.daemon.Containers(); len(left) != 0 {
		t.Fatalf("containers %+v left after the run", left)
	}
}

func TestWhatAContainerCannotGiveIsRefusedBeforeAnythingStarts(t *testing.T) {
	cases := map[string]struct {
		o    options
		want string
		kind core.FailureKind
	}{
		"cuda with no GPU passed":   {options{spec: docker.Spec{Dockerfile: []byte(simpleDockerfile), Backend: core.BackendCUDA}}, "gpu resource", core.FailureUnavailable},
		"rocm with NVIDIA GPUs":     {options{spec: docker.Spec{Dockerfile: []byte(simpleDockerfile), Backend: core.BackendROCm}, configure: withGPUs}, "rocm resource", core.FailureUnavailable},
		"rocm with no GPU passed":   {options{spec: docker.Spec{Dockerfile: []byte(simpleDockerfile), Backend: core.BackendROCm}}, "rocm resource", core.FailureUnavailable},
		"cuda with AMD GPUs":        {options{spec: docker.Spec{Dockerfile: []byte(simpleDockerfile), Backend: core.BackendCUDA}, configure: withAMDGPUs}, "gpu resource", core.FailureUnavailable},
		"an accelerator of neither": {options{spec: docker.Spec{Dockerfile: []byte(simpleDockerfile), Backend: core.BackendTrainium}, configure: withGPUs}, "--env host", core.FailureUnavailable},
		"nested docker":             {options{spec: docker.Spec{Dockerfile: []byte(simpleDockerfile), NestedDocker: true}}, "docker_in_docker", core.FailureUnavailable},
		"no Dockerfile":             {options{spec: docker.Spec{Dockerfile: []byte{}, Files: task.Tree{"run.sh": {Data: []byte("true")}}}}, "Dockerfile", core.FailureInvalidInput},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			s := newScene(t, c.o)
			_, errFingerprint := s.env.Fingerprint(context.Background())
			attempt, _ := s.run("print", "x")
			var errRun error = &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("%+v", attempt)}
			if ns, ok := attempt.(core.NotStarted); ok {
				errRun = &ns.Failure
			}
			for what, err := range map[string]error{"Fingerprint": errFingerprint, "Run": errRun} {
				var f *core.Failure
				if !errors.As(err, &f) || f.Kind != c.kind || !strings.Contains(f.Message, c.want) {
					t.Errorf("%s: %v, want %s naming %q", what, err, c.kind, c.want)
				}
			}
			if s.daemon.Builds() != 0 || len(s.daemon.Containers()) != 0 {
				t.Errorf("a refused task built %d images and left %d containers", s.daemon.Builds(), len(s.daemon.Containers()))
			}
		})
	}
}

func TestAnErrorOfTheSourceStopsTheRunBeforeAnythingStarts(t *testing.T) {
	w := fakeworld.New()
	d := dockerfake.NewDaemon(w.MemoryGates)
	missing := &core.Failure{Kind: core.FailureNotFound, Message: "no Dockerfile at this commit"}
	p, err := docker.New(docker.Config{
		Engine:  d.Connect(),
		Source:  dockerfake.Source(func(context.Context, environment.Target) (docker.Spec, error) { return docker.Spec{}, missing }),
		Backend: cpuBackend(), Timers: envtest.NewTimers(), IDs: dockerfake.NewIDs(1), Scope: "test", Placement: "default", Builds: (&fakelease.Place{}).Daemon(),
	})
	if err != nil {
		t.Fatal(err)
	}
	e, _ := p.Environment(environment.Target{Task: "t"})
	if f := envtest.NotStarted(t, e.Run(context.Background(), core.Execution{Checkout: checkout(t), Argv: []string{"print", "x"}, Timeout: time.Hour}, nil)); f != *missing {
		t.Fatalf("got %v, want the source's error", f)
	}
	if _, err := e.Fingerprint(context.Background()); !errors.Is(err, missing) {
		t.Fatalf("Fingerprint: got %v, want the source's error", err)
	}
	if d.Builds() != 0 || len(d.Containers()) != 0 {
		t.Fatal("something was built or started")
	}
}

func TestConfigIsValidated(t *testing.T) {
	w := fakeworld.New()
	good := docker.Config{
		Engine: dockerfake.NewDaemon(w.MemoryGates).Connect(), Source: dockerfake.Fixed(docker.Spec{}), Backend: cpuBackend(),
		Timers: envtest.NewTimers(), IDs: dockerfake.NewIDs(1), Scope: "test", Placement: "default",
		Builds: (&fakelease.Place{}).Daemon(),
	}
	if _, err := docker.New(good); err != nil {
		t.Fatal(err)
	}
	cases := map[string]func(*docker.Config){
		"Engine":    func(c *docker.Config) { c.Engine = nil },
		"Builds":    func(c *docker.Config) { c.Builds = nil },
		"Source":    func(c *docker.Config) { c.Source = nil },
		"Backend":   func(c *docker.Config) { c.Backend = nil },
		"Timers":    func(c *docker.Config) { c.Timers = nil },
		"IDs":       func(c *docker.Config) { c.IDs = nil },
		"scope":     func(c *docker.Config) { c.Scope = "has space" },
		"user":      func(c *docker.Config) { c.User = docker.User{UID: -1} },
		"placement": func(c *docker.Config) { c.Placement = "" },
		"resource": func(c *docker.Config) {
			c.Resources = []resource.Resource{&fakeresource.Fake{Of: core.ResourceGPU}, &fakeresource.Fake{Of: core.ResourceGPU}}
		},
	}
	for name, mutate := range cases {
		c := good
		mutate(&c)
		_, err := docker.New(c)
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.Contains(strings.ToLower(f.Message), strings.ToLower(name)) {
			t.Errorf("%s: %v, want invalid input naming it", name, err)
		}
	}
}

// plant makes a random mix of containers: of this scope or not, made by this tool or not, running
// or not. It returns the names a sweep must remove and the names it must leave.
func plant(rng *rand.Rand, d *dockerfake.Daemon) (sweep, keep []string) {
	for i := range 1 + rng.IntN(8) {
		name := "planted-" + string(rune('a'+i))
		labels := docker.Labels{}
		mine := rng.IntN(2) == 0
		if mine {
			labels[docker.LabelOwner] = docker.Owner
		} else if rng.IntN(2) == 0 {
			labels[docker.LabelOwner] = "someone-else"
		}
		inScope := rng.IntN(2) == 0
		if inScope {
			labels[docker.LabelScope] = "test"
		} else if rng.IntN(2) == 0 {
			labels[docker.LabelScope] = "other-scope"
		}
		if rng.IntN(2) == 0 {
			labels["unrelated"] = "label"
		}
		d.Plant(name, labels, rng.IntN(2) == 0)
		if mine && inScope {
			sweep = append(sweep, name)
		} else {
			keep = append(keep, name)
		}
	}
	return sweep, keep
}

// Whatever mix of containers is on the machine, a run removes the leftovers of its own scope and
// touches nothing else.
func TestARunRemovesTheLeftoversOfItsScopeAndNothingElse(t *testing.T) {
	for seed := range uint64(200) {
		rng := rand.New(rand.NewPCG(seed, 7))
		s := newScene(t, options{})
		sweep, keep := plant(rng, s.daemon)
		s.mustRun("print", "x")
		left := map[string]bool{}
		for _, c := range s.daemon.Containers() {
			left[c.Name] = true
		}
		for _, name := range sweep {
			if left[name] {
				t.Fatalf("seed %d: the leftover %s was not removed", seed, name)
			}
		}
		for _, name := range keep {
			if !left[name] {
				t.Fatalf("seed %d: %s is not ours and was removed", seed, name)
			}
		}
		if len(left) != len(keep) {
			t.Fatalf("seed %d: %d containers left, want the %d that are not ours", seed, len(left), len(keep))
		}
	}
}

func TestARunKeepsTheContainerOfAnotherLiveRunOfItsProcess(t *testing.T) {
	s := newScene(t, options{})
	x := core.Execution{Checkout: checkout(t), Argv: []string{"mark", "first-up", ";", "hold", "first-go"}, Timeout: time.Hour}
	done := make(chan struct{})
	go func() {
		defer close(done)
		if e, ok := s.env.Run(context.Background(), x, nil).(core.Ended); !ok || e.Exec.ExitCode != 0 {
			t.Errorf("first run: %+v", e)
		}
	}()
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	if !s.world.Reached(ctx, "first-up") {
		t.Fatal("the first run never started")
	}
	s.mustRun("print", "second")
	if got := s.daemon.Running(); len(got) != 1 {
		t.Fatalf("running containers %v after the second run, want the first run's alone", got)
	}
	s.world.Release("first-go")
	<-done
	if got := s.daemon.Containers(); len(got) != 0 {
		t.Fatalf("containers %+v left", got)
	}
}

// A run whose sweep fails does not start: a leftover that could not be removed would run beside
// the job.
func TestAFailedSweepStopsTheRun(t *testing.T) {
	for _, failing := range []dockerfake.Op{dockerfake.OpContainers, dockerfake.OpRemove} {
		for _, late := range []bool{false, true} {
			s := newScene(t, options{})
			s.daemon.Plant("leftover", docker.Labels{docker.LabelOwner: docker.Owner, docker.LabelScope: "test"}, true)
			s.daemon.SetFaults(func(c dockerfake.Call) *dockerfake.Fault {
				if c.Op == failing {
					return &dockerfake.Fault{Err: boom, Late: late}
				}
				return nil
			})
			attempt, _ := s.run("print", "x")
			if f, ok := attempt.(core.NotStarted); !ok || f.Failure.Kind != core.FailureUnavailable || !strings.Contains(f.Failure.Message, "not starting") {
				t.Errorf("%s late=%v: %+v; want not started, unavailable, not starting", failing, late, attempt)
			}
			if s.daemon.Builds() != 0 {
				t.Errorf("%s late=%v: an image was built for a run that did not start", failing, late)
			}
			for _, c := range s.daemon.Containers() {
				if c.Name != "leftover" {
					t.Errorf("%s late=%v: the run started %s", failing, late, c.Name)
				}
			}
		}
	}
}

func TestACancellationDuringTheBuildEndsTheRunWithNothingStarted(t *testing.T) {
	s := newScene(t, options{})
	ctx, cancel := context.WithCancel(context.Background())
	s.daemon.SetFaults(func(c dockerfake.Call) *dockerfake.Fault {
		if c.Op == dockerfake.OpBuild {
			cancel()
		}
		return nil
	})
	if outcome := envtest.Ended(t, s.env.Run(ctx, core.Execution{Checkout: checkout(t), Argv: []string{"print", "x"}, Timeout: time.Hour}, nil)); outcome.Kind != core.ExecCancelled {
		t.Fatalf("got %+v, want a cancelled outcome", outcome)
	}
	if len(s.daemon.Containers()) != 0 {
		t.Fatalf("containers %+v left", s.daemon.Containers())
	}
}

func TestTwoRunsThatShareAnIDAreNotBothAllowed(t *testing.T) {
	w := fakeworld.New()
	d := dockerfake.NewDaemon(w.MemoryGates)
	p, err := docker.New(docker.Config{
		Engine: d.Connect(), Source: dockerfake.Fixed(docker.Spec{Dockerfile: []byte(simpleDockerfile)}), Backend: cpuBackend(),
		Timers: envtest.NewTimers(), IDs: sameID{}, Scope: "test", Placement: "default", Builds: (&fakelease.Place{}).Daemon(),
	})
	if err != nil {
		t.Fatal(err)
	}
	e, _ := p.Environment(environment.Target{Task: "t"})
	x := core.Execution{Checkout: checkout(t), Argv: []string{"mark", "up", ";", "hold", "go"}, Timeout: time.Hour}
	done := make(chan struct{})
	go func() {
		defer close(done)
		_ = e.Run(context.Background(), x, nil)
	}()
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	if !w.Reached(ctx, "up") {
		t.Fatal("the first run never started")
	}
	second := e.Run(context.Background(), core.Execution{Checkout: checkout(t), Argv: []string{"print", "x"}, Timeout: time.Hour}, nil)
	if f, ok := second.(core.NotStarted); !ok || f.Failure.Kind != core.FailureInternal {
		t.Errorf("the second run: %+v, want not started with an internal failure for the id in use", second)
	}
	if got := d.Running(); len(got) != 1 {
		t.Errorf("running containers %v, want the first run's alone", got)
	}
	w.Release("go")
	<-done
	if len(d.Containers()) != 0 {
		t.Errorf("containers %+v left", d.Containers())
	}
}

type sameID struct{}

func (sameID) New() string { return "0123456789abcdef" }

func TestARunIDThatCannotNameAContainerIsRefused(t *testing.T) {
	for _, id := range []string{"", "short", "UPPERCASE12345678", "has space 1234567", "a/b/c/d/e/f/g/h"} {
		w := fakeworld.New()
		d := dockerfake.NewDaemon(w.MemoryGates)
		p, _ := docker.New(docker.Config{
			Engine: d.Connect(), Source: dockerfake.Fixed(docker.Spec{Dockerfile: []byte(simpleDockerfile)}), Backend: cpuBackend(),
			Timers: envtest.NewTimers(), IDs: fixedID(id), Scope: "test", Placement: "default", Builds: (&fakelease.Place{}).Daemon(),
		})
		e, _ := p.Environment(environment.Target{Task: "t"})
		a := e.Run(context.Background(), core.Execution{Checkout: checkout(t), Argv: []string{"print", "x"}, Timeout: time.Hour}, nil)
		if f, ok := a.(core.NotStarted); !ok || f.Failure.Kind != core.FailureInternal || len(d.Containers()) != 0 {
			t.Errorf("id %q: %+v, %d containers; want not started with an internal failure", id, a, len(d.Containers()))
		}
	}
}

type fixedID string

func (f fixedID) New() string { return string(f) }

func TestARunWithoutScratchMountsOnlyTheCheckout(t *testing.T) {
	s := newScene(t, options{})
	s.fingerprint()
	x := core.Execution{Checkout: checkout(t), Argv: []string{"mark", "up", ";", "hold", "go"}, Timeout: time.Hour}
	var wg sync.WaitGroup
	wg.Go(func() {
		if a, ok := s.env.Run(context.Background(), x, &bytes.Buffer{}).(core.Ended); !ok {
			t.Errorf("Run: %+v", a)
		}
	})
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	if !s.world.Reached(ctx, "up") {
		t.Fatal("the command never started")
	}
	boxes := s.daemon.Containers()
	if len(boxes) != 1 || len(boxes[0].Spec.Mounts) != 1 {
		t.Errorf("containers %+v, want one with only the checkout mounted", boxes)
	}
	s.world.Release("go")
	wg.Wait()
}
