package docker_test

import (
	"bytes"
	"context"
	"testing"
	"time"

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
		c.Engine, c.AllocationLabel = engine, label
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
