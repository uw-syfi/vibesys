package app_test

import (
	"fmt"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// commandsAlone is a demand under which a command whose script starts with "alone" needs the
// machine for itself, as a measure does.
type commandsAlone struct{}

func (commandsAlone) Claims(job core.Job) []schedule.Claim {
	c, _ := job.Spec.Settings.(core.CommandSettings)
	alone := len(c.Argv) > 1 && c.Argv[1] == "alone"
	return []schedule.Claim{{Resource: schedule.DefaultPlacement, Amount: 1, Exclusive: alone}}
}

// A daemon of four slots runs three commands at once, holds back one that needs the machine alone
// and one after it, which keeps behind it, and says why of each. It starts
// the one that needs the machine alone once the others ended, and runs nothing beside it.
func TestADaemonOfSeveralSlotsRunsJobsBesideEachOther(t *testing.T) {
	w := newWorld(t)
	w.scheduler = app.Scheduler{Resources: []schedule.Resource{{ID: schedule.DefaultPlacement, Capacity: 4}}, Demand: commandsAlone{}}
	a := w.newApp()
	ctx := ctxFor(t)
	held := func(n int) []string {
		return []string{"mark", fmt.Sprintf("started-%d", n), ";", "hold", fmt.Sprintf("g%d", n), ";", "exit", "0"}
	}
	var jobs []core.Job
	for n := range 3 {
		jobs = append(jobs, submit(t, a, held(n)...))
	}
	for n := range 3 {
		if !w.gates.Reached(ctx, fmt.Sprintf("started-%d", n)) {
			t.Fatalf("command %d did not start beside the others", n)
		}
	}
	alone := submit(t, a, append([]string{"mark", "alone", ";"}, held(3)[3:]...)...)
	later := submit(t, a, held(4)...)
	waits := a.Waits(scope, []core.JobID{alone.ID, later.ID})
	if w := waits[alone.ID]; w.Kind != core.WaitInUse || w.Resource != string(schedule.DefaultPlacement) || len(w.Holders) != 3 || w.Holders[0].Run != call.Run || w.Holders[0].Kind != core.JobCommand {
		t.Fatalf("the command that needs the machine alone waits as %+v", w)
	}
	if w := waits[later.ID]; w.Kind != core.WaitBehind || len(w.Holders) != 1 || w.Holders[0].Job != alone.ID {
		t.Fatalf("the command after it waits as %+v", w)
	}
	for n := range 3 {
		w.gates.Release(fmt.Sprintf("g%d", n))
		awaitState(t, a, jobs[n].ID, core.JobSucceeded)
	}
	if !w.gates.Reached(ctx, "alone") {
		t.Fatalf("the command that needs the machine alone did not start once the others ended")
	}
	if n := w.env.Active(); n != 1 {
		t.Fatalf("%d commands run beside the one that needs the machine alone", n)
	}
	if w := a.Waits(scope, []core.JobID{later.ID})[later.ID]; w.Kind != core.WaitHeldExclusively || w.Holders[0].Job != alone.ID {
		t.Fatalf("the command after it waits as %+v", w)
	}
	w.gates.Release("g3")
	awaitState(t, a, alone.ID, core.JobSucceeded)
	w.gates.Release("g4")
	awaitState(t, a, later.ID, core.JobSucceeded)
}
