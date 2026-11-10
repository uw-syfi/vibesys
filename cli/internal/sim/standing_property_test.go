package sim_test

import (
	"fmt"
	"math/rand/v2"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// standingSeeds is how many seeds the property test runs. Each is a few dozen steps of a daemon
// over the apptainer allocator and the fake cluster, and takes milliseconds.
const standingSeeds = 40

// coverageFloor is the fraction of seeds that must reach each thing the invariants are about, or the
// invariants hold of nothing.
const coverageFloor = 0.7

// settle ticks the daemon's allocation clock until cond holds. Each tick is one look the daemon
// took at its cluster, so what a tick settles is settled by the daemon's own steps, not by time.
func (r *standingRig) settle(what string, cond func() bool) {
	r.t.Helper()
	ctx := r.ctx()
	for !cond() {
		select {
		case <-ctx.Done():
			r.t.Fatalf("never settled: %s", what)
		default:
		}
		r.tick()
	}
}

// finalJob is the job once it ended.
func (r *standingRig) finalJob(a *app.App, id core.JobID) core.Job {
	r.t.Helper()
	var job core.Job
	r.settle(fmt.Sprintf("job %s ended", id), func() bool {
		j, err := a.Job(r.ctx(), standingScope, id)
		job = j
		return err == nil && j.State.Final()
	})
	return job
}

// view is an allocation's record, which the daemon always has.
func view(t *testing.T, a *app.App, id alloc.ID) app.AllocationView {
	t.Helper()
	v, err := a.Allocation(id)
	if err != nil {
		t.Fatalf("Allocation(%s): %v", id, err)
	}
	return v
}

// What a run does after its allocations run. The first five are each done once, in an order the seed
// chooses; actRequest is only for the extra things it does.
const (
	actJob = iota
	actRestart
	actOutage
	actRenew
	actEnd
	actRequest
)

// reached says what a seed did of what the property test is about.
type reached struct {
	jobInAllocation, renewalSuccessor, readoptedWithQueuedJob, outageSurvived, endedAfterJobs bool
}

// Seeds of a run's life with standing allocations on an apptainer placement hold what the design
// says of them: a job submitted into one runs in its batch job and asks the cluster for no other,
// a restart finds the allocations again (the daemon's identity names them) and submits nothing
// twice, an outage is never an end, a renewal makes exactly one successor, and ending the run
// leaves nothing on the cluster. The seeds choose the order and the sizes of what the run does.
func TestSeedsOfStandingAllocationsOnAClusterHoldTheirInvariants(t *testing.T) {
	var got []reached
	for seed := range uint64(standingSeeds) {
		t.Run(fmt.Sprint("seed ", seed), func(t *testing.T) { got = append(got, runStandingSeed(t, seed)) })
	}
	for name, hit := range map[string]func(reached) bool{
		"a job ran inside an allocation":                      func(r reached) bool { return r.jobInAllocation },
		"a renewal made a successor":                          func(r reached) bool { return r.renewalSuccessor },
		"a restart readopted a running allocation with a job": func(r reached) bool { return r.readoptedWithQueuedJob },
		"an outage went by with the allocation running":       func(r reached) bool { return r.outageSurvived },
		"an allocation ended once its jobs were done":         func(r reached) bool { return r.endedAfterJobs },
	} {
		n := 0
		for _, r := range got {
			if hit(r) {
				n++
			}
		}
		t.Logf("%d of %d seeds: %s", n, len(got), name)
		if float64(n) < coverageFloor*float64(len(got)) {
			t.Errorf("only %d of %d seeds: %s; at least %.0f%% must", n, len(got), name, coverageFloor*100)
		}
	}
}

func runStandingSeed(t *testing.T, seed uint64) (out reached) {
	rng := rand.New(rand.NewPCG(seed, 0x5ca1ab1e))
	r := newStandingRig(t, standingConfig)
	a, _ := r.start()
	run := standingCall.Run
	var mine []alloc.ID
	handleOf := func(id alloc.ID) string { return view(t, a, id).Handle.Ref }
	// running is the allocations that run and have time left for a job and its margin; one past its
	// end that the daemon has not seen yet is not one a job is submitted into.
	running := func() []alloc.ID {
		var ids []alloc.ID
		for _, id := range mine {
			if p, ok := view(t, a, id).Phase.(alloc.Running); ok && p.EndsAt.Sub(r.clock.Now()) > 15*time.Minute {
				ids = append(ids, id)
			}
		}
		return ids
	}
	request := func(d time.Duration) alloc.ID {
		v, err := a.RequestAllocation(r.ctx(), run, clusterPlacement, d)
		if err != nil {
			t.Fatalf("RequestAllocation: %v", err)
		}
		mine = append(mine, v.ID)
		return v.ID
	}
	// check is what holds after every step: the cluster is asked once for each allocation the daemon
	// has a batch job of, and holds no batch job that no record names.
	check := func() {
		t.Helper()
		var views []app.AllocationView
		// A request is answered before the cluster is asked, so the batch job of each allocation is
		// waited for.
		r.settle("every allocation has its batch job", func() bool {
			views, _ = a.Allocations("", true)
			return !slices.ContainsFunc(views, func(v app.AllocationView) bool {
				return v.Handle.Ref == "" && v.Stage() != alloc.StageEnded
			})
		})
		handles := map[string]bool{}
		successorOf := map[alloc.ID]alloc.ID{}
		for _, v := range views {
			if v.RenewedBy != "" {
				if was, dup := successorOf[v.RenewedBy]; dup {
					t.Fatalf("%s is the successor of both %s and %s", v.RenewedBy, was, v.ID)
				}
				successorOf[v.RenewedBy] = v.ID
			}
			if v.Handle.Ref != "" {
				handles[v.Handle.Ref] = true
			}
		}
		if n := len(r.cluster.Submitted()); n != len(views) {
			t.Fatalf("the cluster was asked for %d batch jobs, and the daemon has %d allocations: something was submitted twice or not recorded", n, len(views))
		}
		for _, id := range r.cluster.Live() {
			// Live names jobs by their names, which carry the allocation's id after the owner's.
			if !slices.ContainsFunc(views, func(v app.AllocationView) bool { return len(id) > len(v.ID) && id[len(id)-len(v.ID):] == string(v.ID) }) {
				t.Fatalf("the cluster holds %s, which no allocation names", id)
			}
		}
	}

	// Two allocations run to begin with. Then the run does each thing the invariants are about, in an
	// order the seed chooses, and a few more things it chooses at random.
	for _, d := range []time.Duration{time.Hour, 2 * time.Hour} {
		r.await(a, request(d), app.AllocUntil{Kind: app.UntilRunning})
	}
	check()
	script := rng.Perm(actRequest)
	for range rng.IntN(4) {
		script = append(script, rng.IntN(actRequest+1))
	}
	for step, what := range script {
		switch what {
		case actJob:
			// A job into any, or into a named allocation that runs: it runs in a batch job that was
			// already running, and the cluster is asked for nothing else.
			ids := running()
			if len(ids) == 0 {
				continue
			}
			into := core.AllocationAny
			if rng.IntN(2) == 0 {
				into = string(ids[rng.IntN(len(ids))])
			}
			before := len(r.cluster.Submitted())
			argv := []string{"print", fmt.Sprint("step ", step), ";", "exit", fmt.Sprint(rng.IntN(2))}
			job := r.finalJob(a, r.jobIn(a, into, argv...).ID)
			if job.State != core.JobSucceeded && job.State != core.JobFailed {
				t.Fatalf("the job ended %s", job.State)
			}
			if len(r.cluster.Submitted()) != before {
				t.Fatalf("the job asked the cluster for a batch job of its own")
			}
			inside := 0
			for _, id := range ids {
				inside += len(r.cluster.StartedIn(handleOf(id)))
			}
			out.jobInAllocation = out.jobInAllocation || inside > 0
		case actRequest:
			if len(mine) < 3 {
				request([]time.Duration{time.Hour, 90 * time.Minute, 2 * time.Hour}[rng.IntN(3)])
			}
		case actRestart:
			// A restart with a job running in a running allocation and another queued behind it: the
			// daemon finds the allocation by its log and its owner, asks for nothing twice, and both jobs
			// run in it, one at a time.
			ids := running()
			if len(ids) == 0 {
				continue
			}
			id := ids[rng.IntN(len(ids))]
			mark, hold := fmt.Sprint("in-", seed, "-", step), fmt.Sprint("g-", seed, "-", step)
			slow := r.jobIn(a, string(id), "mark", mark, ";", "hold", hold, ";", "exit", "0")
			if !r.gates.Reached(r.ctx(), mark) {
				t.Fatal("the first job did not start")
			}
			queued := r.jobIn(a, string(id), "print", "behind", ";", "exit", "0")
			a.Close()
			a, _ = r.start()
			r.settle("the restarted daemon has the allocation running", func() bool {
				_, ok := view(t, a, id).Phase.(alloc.Running)
				return ok
			})
			r.gates.Release(hold)
			for _, j := range []core.JobID{slow.ID, queued.ID} {
				if got := r.finalJob(a, j); got.State != core.JobSucceeded {
					t.Fatalf("job %s ended %s after the restart", j, got.State)
				}
			}
			if len(r.cluster.StartedIn(handleOf(id))) == 0 {
				t.Fatal("no job ran in the allocation after the restart")
			}
			out.readoptedWithQueuedJob = true
			out.jobInAllocation = true
		case actOutage:
			// An outage: the daemon can ask nothing, which is never an end.
			ids := running()
			if len(ids) == 0 {
				continue
			}
			r.cluster.SetFaults(func(string) error { return unreachable() })
			for range 1 + rng.IntN(3) {
				r.tick()
			}
			for _, id := range ids {
				if _, ok := view(t, a, id).Phase.(alloc.Running); !ok {
					t.Fatalf("allocation %s is %s after an outage", id, view(t, a, id).Stage())
				}
			}
			r.cluster.SetFaults(nil)
			out.outageSurvived = true
		case actRenew:
			// A renewal order, and the time to its lead: exactly one successor, which the old one's end
			// then leaves running.
			ids := running()
			if len(ids) == 0 {
				continue
			}
			id := ids[rng.IntN(len(ids))]
			if view(t, a, id).RenewedBy != "" {
				continue
			}
			v := view(t, a, id)
			left := v.Phase.(alloc.Running).EndsAt.Sub(r.clock.Now())
			lead := 10 * time.Minute
			if left <= lead+2*time.Minute {
				continue
			}
			order := alloc.RenewalOrder{Lead: lead, For: time.Hour, Until: alloc.UntilRunEnd{}}
			if _, err := a.RenewAllocation(r.ctx(), run, id, &order); err != nil {
				t.Fatalf("RenewAllocation: %v", err)
			}
			r.passes(left - lead + time.Minute)
			r.settle("the renewal made its successor", func() bool { return view(t, a, id).RenewedBy != "" })
			next := view(t, a, id).RenewedBy
			mine = append(mine, next)
			r.settle("the successor runs", func() bool {
				_, ok := view(t, a, next).Phase.(alloc.Running)
				return ok
			})
			r.passes(lead)
			out.renewalSuccessor = true
		case actEnd:
			// One ends once its jobs are done.
			if len(running()) < 2 && len(mine) < 3 {
				r.await(a, request(time.Hour), app.AllocUntil{Kind: app.UntilRunning})
			}
			ids := running()
			if len(ids) < 2 {
				continue
			}
			id := ids[rng.IntN(len(ids))]
			if _, err := a.EndAllocation(r.ctx(), run, id, true); err != nil {
				t.Fatalf("EndAllocation: %v", err)
			}
			r.settle("the allocation ended", func() bool { return view(t, a, id).Stage() == alloc.StageEnded })
			out.endedAfterJobs = true
		}
		check()
	}
	// The run ends: every allocation ends, and the cluster holds nothing.
	if _, err := a.EndRunAllocations(r.ctx(), run); err != nil {
		t.Fatalf("EndRunAllocations: %v", err)
	}
	r.settle("the run's allocations ended and were released", func() bool {
		views, _ := a.Allocations("", true)
		for _, v := range views {
			if end, ok := v.Phase.(alloc.Ended); !ok || !end.Released {
				return false
			}
		}
		return len(r.cluster.Live()) == 0
	})
	if left := r.cluster.Running(); len(left) != 0 {
		t.Fatalf("processes left on the cluster: %v", left)
	}
	return out
}

func unreachable() error {
	return &core.Failure{Kind: core.FailureUnavailable, Message: "the login node cannot be reached"}
}
