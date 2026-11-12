package daemon_test

import (
	"runtime"
	"testing"
	"time"

	"buf.build/go/protovalidate"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/memstores"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/fakelease"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// everyCommandAlone has every command hold the machine alone, as a measure does.
type everyCommandAlone struct{}

func (everyCommandAlone) Options(core.Job) []schedule.Option {
	return schedule.Only(schedule.Claim{Resource: schedule.DefaultPlacement, Amount: 1, Exclusive: true})
}

// TestStatusListsTheLeasesAndTheOtherDaemons has a job hold the machine's lease: the status names
// the lease, the job and where it is kept, lists the other daemons of the machine, and is a valid
// message. Once the job ends, the status lists no lease.
func TestStatusListsTheLeasesAndTheOtherDaemons(t *testing.T) {
	gates := fakeenv.NewMemoryGates()
	env := fakeenv.New(core.EnvironmentHost, gates)
	survivors := supervisor.NewSurvivors()
	place := &fakelease.Place{Name: "this machine (/leases)"}
	a := app.New(app.Config{
		Clock:        clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), time.Millisecond),
		Stores:       memstores.New(app.Rules(5)),
		Environments: fakeenv.Environments{core.EnvironmentHost: env},
		Supervisors:  supervisor.NewInProcess(fakeenv.Environments{core.EnvironmentHost: env}, &fakeenv.Checkouts{}, survivors),
		Repositories: repos(),
		Scheduler:    app.Scheduler{Demand: everyCommandAlone{}},
		Leasing: app.Leasing{
			Book:   lease.Map{string(schedule.DefaultPlacement): {Leases: place.Daemon(), Key: "machine"}},
			Holder: core.LeaseHolder{User: "alice", Host: "host-a", PID: 41},
		},
	})
	t.Cleanup(func() { a.Close(); survivors.Close() })
	if err := a.Start(ctxFor(t)); err != nil {
		t.Fatal(err)
	}
	srv := daemon.NewServer(a, "test", daemon.Inherited{}).WithNeighbors(func() []daemon.Neighbor {
		return []daemon.Neighbor{{PID: 7, User: "bob"}}
	})
	scope, err := core.ResolvedScope("/repo", "task")
	if err != nil {
		t.Fatal(err)
	}
	cc := core.CallContext{Scope: scope, Run: "run-1", Harness: "h", Agent: "agent-1"}
	out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: cc, Spec: core.JobSpec{
		Commit: commit, Environment: core.EnvironmentHost, Settings: core.CommandSettings{Image: core.ImageHarness, Argv: []string{"mark", "on", ";", "hold", "g", ";", "exit", "0"}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	if !gates.Reached(ctxFor(t), "on") {
		t.Fatalf("the job did not start")
	}
	st, err := srv.Status(ctxFor(t), &cliv1.StatusRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if err := protovalidate.Validate(st); err != nil {
		t.Fatalf("the status is not a valid message: %v", err)
	}
	if l := st.GetLeases(); len(l) != 1 || l[0].GetJobId() != string(out.Job.ID) || l[0].GetKey() != "machine" || l[0].GetResource() != string(schedule.DefaultPlacement) || l[0].GetWhere() != "this machine (/leases)" || l[0].GetTask() != "task" {
		t.Fatalf("the status lists the leases %v", l)
	}
	if d := st.GetOtherDaemons(); len(d) != 1 || d[0].GetPid() != 7 || d[0].GetUser() != "bob" {
		t.Fatalf("the status lists the daemons %v", d)
	}
	gates.Release("g")
	ctx := ctxFor(t)
	for {
		st, err = srv.Status(ctx, &cliv1.StatusRequest{})
		if err == nil && len(st.GetLeases()) == 0 && len(place.Held()) == 0 {
			break
		}
		if ctx.Err() != nil {
			t.Fatalf("the lease is still held after the job ended: %v %v", st.GetLeases(), place.Held())
		}
		runtime.Gosched()
	}
}
