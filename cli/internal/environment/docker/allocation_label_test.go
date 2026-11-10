package docker_test

import (
	"bytes"
	"context"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
)

// recording is an Engine that keeps the spec of every container it is asked to start.
type recording struct {
	docker.Engine
	specs []docker.ContainerSpec
}

func (r *recording) Start(ctx context.Context, spec docker.ContainerSpec) (docker.Container, error) {
	r.specs = append(r.specs, spec)
	return r.Engine.Start(ctx, spec)
}

func runWithTarget(t *testing.T, label, allocation string) []docker.ContainerSpec {
	t.Helper()
	w := fakeworld.New()
	engine := &recording{Engine: dockerfake.NewDaemon(w.MemoryGates).Connect()}
	r := newRig(t, dockerfake.NewDaemon(w.MemoryGates), options{configure: func(c *docker.Config) {
		c.Engine, c.AllocationLabel, c.Clock = engine, label, clock.NewManual(time.Time{}, 0)
	}})
	e, err := r.provider.Environment(environment.Target{
		Repository: "/repo", Commit: "0123456789abcdef0123456789abcdef01234567", Task: "task", Allocation: allocation,
	})
	if err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	a := e.Run(t.Context(), core.Execution{Checkout: checkout(t), Argv: []string{"print", "a", ";", "exit", "0"}, Timeout: time.Hour}, &out)
	if ended, ok := a.(core.Ended); !ok || ended.Exec.ExitCode != 0 {
		t.Fatalf("run: %+v", a)
	}
	return engine.specs
}

// A job that runs inside a standing allocation names it on its container, under the label the
// placement's engine reads.
func TestAJobInAnAllocationLabelsItsFirstContainer(t *testing.T) {
	specs := runWithTarget(t, "io.vibesys.cli.allocation", "4242")
	if len(specs) == 0 {
		t.Fatal("no container started")
	}
	if got := specs[0].Labels["io.vibesys.cli.allocation"]; got != "4242" {
		t.Fatalf("the first container's labels = %v, want the allocation 4242", specs[0].Labels)
	}
	for _, s := range specs[1:] {
		if _, has := s.Labels["io.vibesys.cli.allocation"]; has {
			t.Errorf("container %s carries the allocation label, which only the first does", s.Name)
		}
	}
}

// Without an allocation, or on a placement that reads none, no label is added.
func TestNoAllocationLabelWithoutAnAllocationOrALabel(t *testing.T) {
	for name, c := range map[string]struct{ label, allocation string }{
		"no allocation": {"io.vibesys.cli.allocation", ""},
		"no label key":  {"", "4242"},
	} {
		for _, s := range runWithTarget(t, c.label, c.allocation) {
			if _, has := s.Labels["io.vibesys.cli.allocation"]; has || len(s.Labels) > 3 {
				t.Errorf("%s: labels %v carry the allocation", name, s.Labels)
			}
		}
	}
}

// entryRun runs a command of the reviewer's case in allocation 4242, whose end left the job until
// 3m from its release to enter it (60m left, a 2m margin, a 55m bound): late says its preparation
// took 10m, as an image build can.
func entryRun(t *testing.T, any, late bool) (core.Attempt, []docker.ContainerSpec, string) {
	t.Helper()
	w := fakeworld.New()
	engine := &recording{Engine: dockerfake.NewDaemon(w.MemoryGates).Connect()}
	clk := clock.NewManual(time.Date(2026, 11, 10, 8, 0, 0, 0, time.UTC), 0)
	by := clk.Now().Add(3 * time.Minute)
	if late {
		clk.Advance(10 * time.Minute)
	}
	r := newRig(t, dockerfake.NewDaemon(w.MemoryGates), options{configure: func(c *docker.Config) {
		c.Engine, c.AllocationLabel, c.Clock = engine, "io.vibesys.cli.allocation", clk
	}})
	e, err := r.provider.Environment(environment.Target{
		Repository: "/repo", Commit: "0123456789abcdef0123456789abcdef01234567", Task: "task",
		Allocation: "4242", EnterBy: by, AllocationAny: any,
	})
	if err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	a := e.Run(t.Context(), core.Execution{Checkout: checkout(t), Argv: []string{"print", "a", ";", "exit", "0"}, Timeout: 55 * time.Minute}, &out)
	return a, engine.specs, out.String()
}

// The reviewer's case: a job whose preparation outlasted the window its allocation's end left it
// does not enter the allocation. Submitted into it by name, it does not run, and its failure says
// both durations; submitted to any allocation, it runs without one, and its output says why. A job
// ready within the window enters. Before, the job entered and was killed at the time limit.
func TestAJobReadyAfterItsEntryWindowDoesNotEnterTheAllocation(t *testing.T) {
	a, specs, _ := entryRun(t, false, false)
	if ended, ok := a.(core.Ended); !ok || ended.Exec.ExitCode != 0 || len(specs) == 0 || specs[0].Labels["io.vibesys.cli.allocation"] != "4242" {
		t.Fatalf("a job ready in time: %+v, containers %+v; want it run in the allocation", a, specs)
	}

	a, specs, _ = entryRun(t, false, true)
	ns, ok := a.(core.NotStarted)
	if !ok || ns.Failure.Kind != core.FailureConflict || len(specs) != 0 {
		t.Fatalf("a strict job ready too late: %+v, containers %d; want it not started, a conflict", a, len(specs))
	}
	for _, want := range []string{"7m0s", "55m0s", "4242"} {
		if !strings.Contains(ns.Failure.Message, want) {
			t.Errorf("the failure %q does not say %q", ns.Failure.Message, want)
		}
	}

	a, specs, out := entryRun(t, true, true)
	if ended, ok := a.(core.Ended); !ok || ended.Exec.ExitCode != 0 || len(specs) == 0 {
		t.Fatalf("a job of any allocation ready too late: %+v; want it run", a)
	}
	for _, s := range specs {
		if _, has := s.Labels["io.vibesys.cli.allocation"]; has {
			t.Fatalf("the late job of any allocation ran in it: %v", s.Labels)
		}
	}
	if !strings.Contains(out, "allocation any:") || !strings.Contains(out, "runs without one") {
		t.Fatalf("the output %q does not say why the job runs without the allocation", out)
	}
}
