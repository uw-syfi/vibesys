package slurm_test

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os/exec"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockercli"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
	"github.com/uw-syfi/vibesys/cli/internal/environment/slurm"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
)

// fakeScheduler holds jobs by name and comment, and records what was cancelled.
type fakeScheduler struct {
	mu        sync.Mutex
	jobs      map[string]string // name to comment
	cancelled []string
	fail      error
}

func newScheduler() *fakeScheduler { return &fakeScheduler{jobs: map[string]string{}} }

func (f *fakeScheduler) Jobs(_ context.Context, scope string) ([]string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []string
	for name, comment := range f.jobs {
		if comment == scope {
			out = append(out, name)
		}
	}
	slices.Sort(out)
	return out, nil
}

func (f *fakeScheduler) Cancel(_ context.Context, name string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.cancelled = append(f.cancelled, name)
	if f.fail != nil {
		return f.fail
	}
	delete(f.jobs, name)
	return nil
}

func (f *fakeScheduler) Cancelled() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.cancelled)
}

var facts = core.MachineFacts{HostID: "host-a", OS: "linux", Arch: "arm64", CPUModel: "Test CPU", LogicalCPUs: 20, MemoryBytes: 120 << 30}

func provider(t testing.TB, engine docker.Engine, timers clock.Timers) *docker.Provider {
	t.Helper()
	p, err := docker.New(docker.Config{
		Engine: engine, Source: dockerfake.Fixed(docker.Spec{Dockerfile: []byte("FROM busybox:1.36.1\n")}),
		Backend: cpu.New(cpu.NewFakeSystem(facts), clock.NewManual(time.Unix(1, 0), time.Millisecond)),
		Timers:  timers, IDs: dockerfake.NewIDs(1), Scope: "test", User: docker.User{UID: 1002, GID: 1002},
	})
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func envOf(t testing.TB, p *docker.Provider) environment.Environment {
	t.Helper()
	e, err := p.Environment(environment.Target{Repository: "/repo", Commit: "0123456789abcdef0123456789abcdef01234567", Task: "task"})
	if err != nil {
		t.Fatal(err)
	}
	return e
}

var checkouts sync.Map

func checkout(t *testing.T) core.Checkout {
	n := 0
	checkouts.Range(func(any, any) bool { n++; return true })
	checkouts.Store(t.Name()+fmt.Sprint(n), true)
	return core.Checkout{Commit: "0123456789abcdef0123456789abcdef01234567", Dir: fmt.Sprintf("/fake-checkouts/s%d", n)}
}

// The docker environment over the Slurm engine is held to the same contract as every environment.
func TestTheDockerEnvironmentInSlurmPassesTheEnvironmentContract(t *testing.T) {
	envtest.Run(t, envtest.Rig{
		New: func(t *testing.T, w envtest.World, timers clock.Timers) environment.Environment {
			daemon := dockerfake.NewDaemon(w.(fakeworld.World).MemoryGates)
			sched := newScheduler()
			t.Cleanup(func() {
				if running := daemon.Running(); len(running) > 0 {
					t.Errorf("containers still run after the case: %v", running)
				}
			})
			return envOf(t, provider(t, slurm.Engine{Inner: daemon.Connect(), Scheduler: sched}, timers))
		},
		World:    func(t *testing.T) envtest.World { return fakeworld.New() },
		Checkout: func(t *testing.T, _ envtest.World) core.Checkout { return checkout(t) },
		Spawn: func(t *testing.T, w envtest.World, x core.Execution) func() {
			daemon := dockerfake.NewDaemon(w.(fakeworld.World).MemoryGates)
			client := daemon.Connect()
			e := envOf(t, provider(t, slurm.Engine{Inner: client, Scheduler: newScheduler()}, envtest.NewTimers()))
			var wg sync.WaitGroup
			wg.Go(func() { _ = e.Run(context.Background(), x, io.Discard) })
			var once sync.Once
			kill := func() { once.Do(func() { client.Crash(); wg.Wait() }) }
			t.Cleanup(kill)
			return kill
		},
	})
}

func run(t *testing.T, e environment.Environment) core.Attempt {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	return e.Run(ctx, core.Execution{Checkout: checkout(t), Argv: []string{"print", "x"}, Timeout: time.Hour}, io.Discard)
}

// Every run cancels its container's allocation after removing the container, and a run's sweep
// cancels the allocations a dead run of its scope left, and no other scope's.
func TestARunEndsItsAllocationAndSweepsWhatADeadRunLeft(t *testing.T) {
	w := fakeworld.New()
	daemon := dockerfake.NewDaemon(w.MemoryGates)
	sched := newScheduler()
	sched.jobs[slurm.JobName("vibesys-cli-deadbeef01")] = "test"
	sched.jobs[slurm.JobName("vibesys-cli-0th3r5c0pe")] = "other"
	sched.jobs[slurm.JobName(dockercli.BuildName("vibesys-cli-task:0123"))] = "test"
	e := envOf(t, provider(t, slurm.Engine{Inner: daemon.Connect(), Scheduler: sched}, envtest.NewTimers()))
	if a := run(t, e); a != (core.Ended{Exec: core.ExecOutcome{Kind: core.ExecExited}}) {
		t.Fatalf("attempt %+v", a)
	}
	got := sched.Cancelled()
	if !slices.Contains(got, slurm.JobName("vibesys-cli-deadbeef01")) {
		t.Errorf("cancelled %v: the dead run's allocation was not swept", got)
	}
	for _, name := range got {
		if strings.Contains(name, "0th3r5c0pe") || strings.Contains(name, "-build-") {
			t.Errorf("cancelled %s, which is not a leftover of this scope's runs", name)
		}
	}
	if len(got) != 2 || !strings.HasPrefix(got[1], slurm.JobPrefix+"vibesys-cli-") {
		t.Errorf("cancelled %v, want the leftover and then the run's own allocation", got)
	}
	if len(daemon.Containers()) != 0 {
		t.Errorf("containers left: %v", daemon.Containers())
	}
}

// An allocation whose end is not confirmed makes the run's ending unknown: something may still
// hold the node.
func TestAnAllocationNotConfirmedEndedMakesTheRunUnobserved(t *testing.T) {
	w := fakeworld.New()
	daemon := dockerfake.NewDaemon(w.MemoryGates)
	sched := newScheduler()
	sched.fail = errors.New("slurmctld does not answer")
	e := envOf(t, provider(t, slurm.Engine{Inner: daemon.Connect(), Scheduler: sched}, envtest.NewTimers()))
	a := run(t, e)
	u, ok := a.(core.Unobserved)
	if !ok || !strings.Contains(u.Failure.Message, "not confirmed ended") {
		t.Errorf("attempt %+v, want unobserved saying the allocation was not confirmed ended", a)
	}
}

func TestAnAllocationAsksForTheNodeAndOutlastsTheCommand(t *testing.T) {
	a := slurm.Allocator{Partition: "gpu", Gres: "gpu:1", Exclusive: true, Arguments: []string{"--nodelist=node-a"}}
	got := a.Allocate(dockercli.Allocation{Name: "vibesys-cli-0123", Scope: "d42", Limit: time.Hour}, []string{"docker", "run", "x"})
	want := []string{
		"srun", "--job-name=vibesys-docker-vibesys-cli-0123", "--nodes=1", "--ntasks=1", "--time=70",
		"--comment=d42", "--partition=gpu", "--gres=gpu:1", "--exclusive", "--nodelist=node-a", "--", "docker", "run", "x",
	}
	if !slices.Equal(got, want) {
		t.Errorf("\n%q\nwant\n%q", got, want)
	}
	build := a.Allocate(dockercli.Allocation{Name: "b", Build: true}, []string{"docker", "build", "-"})
	if !slices.Contains(build, "--time=240") || slices.ContainsFunc(build, func(s string) bool { return strings.HasPrefix(s, "--comment") }) {
		t.Errorf("a build's allocation %q", build)
	}
}

func TestOnlyThisPackagesJobsOfTheScopeAreListed(t *testing.T) {
	out := "vibesys-docker-vibesys-cli-01|d1\nvibesys-docker-vibesys-cli-02|d2\nother-job|d1\nvibesys-docker-vibesys-cli-03|(null)\n\n"
	if got := slurm.ParseJobs(out, "d1"); !slices.Equal(got, []string{"vibesys-docker-vibesys-cli-01"}) {
		t.Errorf("%v", got)
	}
}

func TestAJobHasEndedOnlyWhenNeitherSqueueNorSacctSaysItRuns(t *testing.T) {
	cases := []struct {
		squeue, sacct string
		ended         bool
	}{
		{"", "", true},
		{"", "CANCELLED by 1002\n", true},
		{"", "COMPLETED\nCANCELLED+\n", true},
		{"RUNNING\n", "", false},
		{"", "RUNNING\n", false},
		{"", "PENDING\n", false},
		{"COMPLETING", "CANCELLED", false},
	}
	for _, c := range cases {
		if got := slurm.Ended(c.squeue, c.sacct); got != c.ended {
			t.Errorf("squeue %q sacct %q: ended %v, want %v", c.squeue, c.sacct, got, c.ended)
		}
	}
}

// scriptShell answers each Slurm command with the next of its answers, and records the commands.
type scriptShell struct {
	mu      sync.Mutex
	squeue  []string // answers to squeue -n, in order; the last repeats
	scripts []string
}

func (s *scriptShell) Command(ctx context.Context, argv []string) *exec.Cmd {
	s.mu.Lock()
	defer s.mu.Unlock()
	script := argv[2]
	s.scripts = append(s.scripts, script)
	answer := ""
	if strings.HasPrefix(script, "squeue") && strings.Contains(script, "-n") {
		answer = s.squeue[0]
		if len(s.squeue) > 1 {
			s.squeue = s.squeue[1:]
		}
	}
	return exec.CommandContext(ctx, "/bin/sh", "-c", `printf '%s' "$1"`, "sh", answer)
}

func (*scriptShell) Lost(int) bool { return false }
func (*scriptShell) Where() string { return "host-a" }

func TestACancelWaitsUntilTheJobIsGone(t *testing.T) {
	sh := &scriptShell{squeue: []string{"RUNNING\n", "COMPLETING\n", ""}}
	c := slurm.CLI{Shell: sh, Timers: clock.System{}, Poll: time.Nanosecond}
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	if err := c.Cancel(ctx, "vibesys-docker-x"); err != nil {
		t.Fatal(err)
	}
	var squeues int
	for _, s := range sh.scripts {
		if strings.HasPrefix(s, "squeue") {
			squeues++
		}
	}
	if !strings.HasPrefix(sh.scripts[0], "scancel") || squeues != 3 || !strings.HasPrefix(sh.scripts[len(sh.scripts)-1], "sacct") {
		t.Errorf("commands %q: want scancel, squeue until it lists nothing, then sacct", sh.scripts)
	}
}

var _ remote.Shell = (*scriptShell)(nil)
