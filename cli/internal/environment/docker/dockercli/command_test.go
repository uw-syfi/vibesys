package dockercli

import (
	"context"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
)

type prefixAllocator struct{ got []Allocation }

func (p *prefixAllocator) Allocate(a Allocation, argv []string) []string {
	p.got = append(p.got, a)
	return append([]string{"srun", "--job-name=" + a.Name, "--"}, argv...)
}

// Only the commands that hold the machine, a container's first process and a build, are placed in
// an allocation; every other call runs where the engine is, through its shell.
func TestOnlyAContainerAndABuildArePlacedInAnAllocation(t *testing.T) {
	alloc := &prefixAllocator{}
	e, err := New(Config{Timers: clock.System{}, Shell: remote.SSH{Host: "host-a", Program: []string{"ssh"}}, Allocator: alloc})
	if err != nil {
		t.Fatal(err)
	}
	last := func(args []string) string { return args[len(args)-1] }
	if got := last(e.command(context.Background(), nil, "ps", "--all").Args); got != "docker ps --all" {
		t.Errorf("a call ran %q", got)
	}
	keeper := last(e.command(context.Background(), &Allocation{Name: "vibesys-cli-01234567"}, "run", "x").Args)
	if !strings.HasPrefix(keeper, "srun --job-name=vibesys-cli-01234567 -- docker run x") {
		t.Errorf("the container's first process ran %q", keeper)
	}
	if len(alloc.got) != 1 {
		t.Errorf("allocations %+v, want the container's only", alloc.got)
	}
	if name := BuildName("vibesys-cli-task:0123abcd"); strings.ContainsAny(name, ":/") || !strings.HasPrefix(name, "vibesys-cli-build-") {
		t.Errorf("build name %q", name)
	}
}
