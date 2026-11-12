package app_test

import (
	"context"
	"runtime"
	"sync"
	"testing"
	"testing/synctest"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lease"
	"github.com/uw-syfi/vibesys/cli/internal/lease/fakelease"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// leaseSlow is the slow period of the daemons of these tests: a job refused a lease tries again when
// the test passes it. Every shorter wait ends at once, and every longer one never does.
const leaseSlow = 7 * time.Minute

// slowTimers end every wait shorter than leaseSlow at once, and the slow period when the test
// passes it. Passing it ends only the waits armed so far, as a clock that moves on would.
type slowTimers struct {
	mu   sync.Mutex
	slow []chan struct{}
}

func (s *slowTimers) Timeout(_ context.Context, d time.Duration) (<-chan struct{}, func()) {
	fired := make(chan struct{})
	switch {
	case d < leaseSlow:
		close(fired)
	case d == leaseSlow:
		s.mu.Lock()
		s.slow = append(s.slow, fired)
		s.mu.Unlock()
	}
	return fired, func() {}
}

func (s *slowTimers) pass() {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, c := range s.slow {
		close(c)
	}
	s.slow = nil
}

// daemonOf starts a daemon of a user of its own, with stores of its own, that leases the default
// placement as "machine" at the shared place, and runs a command whose script starts with "alone"
// as it would a measure.
func daemonOf(t *testing.T, place *fakelease.Place, user string, pid int) (*world, *app.App, *slowTimers) {
	t.Helper()
	w := newWorld(t)
	w.scheduler = app.Scheduler{Resources: []schedule.Resource{{ID: schedule.DefaultPlacement, Capacity: 4}}, Demand: commandsAlone{}}
	w.leasing = app.Leasing{
		Book:   lease.Map{string(schedule.DefaultPlacement): {Leases: place.Daemon(), Key: "machine"}},
		Holder: core.LeaseHolder{User: user, Host: "host-a", PID: pid},
	}
	timers := &slowTimers{}
	w.timers, w.slowPeriod = timers, leaseSlow
	return w, w.newApp(), timers
}

// waitOf is how the daemon says a job waits.
func waitOf(t *testing.T, a *app.App, sc core.Scope, id core.JobID) core.JobWait {
	t.Helper()
	w, found := a.Waits(sc, []core.JobID{id})[id]
	if !found {
		t.Fatalf("the daemon says nothing of how job %s waits", id)
	}
	return w
}

// TestTwoDaemonsTakeTurnsAtWhatTheyHoldAlone runs two users' daemons on one machine. While
// alice's measure holds the machine's lease, bob's waits, naming alice, and runs nothing, and his
// command that shares the machine keeps behind it. Once alice's measure ends, bob's takes the
// lease at its next try, then his command runs, and nobody holds a lease.
//
// It runs in a bubble, and looks at the daemons only once every goroutine of both is blocked: a
// refused job arms its next try on a goroutine of its own, after the daemon already says it waits
// for the lease, and passing the slow period before that try is armed passes nothing.
func TestTwoDaemonsTakeTurnsAtWhatTheyHoldAlone(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		place := &fakelease.Place{}
		wa, alice, _ := daemonOf(t, place, "alice", 101)
		wb, bob, bobSlow := daemonOf(t, place, "bob", 202)
		ctx := ctxFor(t)

		hers := submit(t, alice, "mark", "alone", ";", "hold", "a", ";", "exit", "0")
		if !wa.gates.Reached(ctx, "alone") {
			t.Fatalf("alice's measure did not start")
		}
		if st := alice.Status(); len(st.Leases) != 1 || st.Leases[0].Job != hers.ID || st.Leases[0].Key != "machine" {
			t.Fatalf("alice's daemon holds %+v, want the machine's lease for her measure", st.Leases)
		}
		his := submit(t, bob, "mark", "alone", ";", "exit", "0")
		synctest.Wait()
		w := waitOf(t, bob, scope, his.ID)
		if w.Kind != core.WaitLeased || w.Elsewhere == nil || w.Elsewhere.User != "alice" || w.Elsewhere.PID != 101 || w.Elsewhere.Job != hers.ID || w.Resource != string(schedule.DefaultPlacement) {
			t.Fatalf("bob's measure waits as %+v, want alice's measure named", w)
		}
		if n := wb.env.Active(); n != 0 {
			t.Fatalf("bob's daemon runs %d commands while alice's measure holds the machine", n)
		}
		// A command of bob's that shares the machine keeps behind his measure, which keeps its turn
		// while it waits for the lease: otherwise a stream of them would starve it.
		shared := submitIn(t, bob, elsewhere, "exit", "0")
		synctest.Wait()
		if w := waitOf(t, bob, scopeElsewhere, shared.ID); w.Kind != core.WaitBehind {
			t.Fatalf("bob's command waits as %+v, want behind his measure", w)
		}

		wa.gates.Release("a")
		awaitState(t, alice, hers.ID, core.JobSucceeded)
		synctest.Wait()
		if held := alice.Status().Leases; len(held) > 0 {
			t.Fatalf("alice's daemon still holds %+v after her measure ended", held)
		}
		bobSlow.pass()
		awaitState(t, bob, his.ID, core.JobSucceeded)
		awaitIn(t, bob, scopeElsewhere, shared.ID, core.JobSucceeded)
		if wa.env.Active() != 0 {
			t.Fatalf("alice's daemon runs a command beside bob's measure")
		}
		synctest.Wait()
		if held := place.Held(); len(held) > 0 {
			t.Fatalf("leases still held: %+v", held)
		}
	})
}

// awaitHeldNothing waits until no lease is held at the place.
func awaitHeldNothing(t *testing.T, place *fakelease.Place) {
	t.Helper()
	ctx := ctxFor(t)
	for len(place.Held()) > 0 {
		if ctx.Err() != nil {
			t.Fatalf("leases still held: %+v", place.Held())
		}
		runtime.Gosched()
	}
}

// TestLeasesAreGivenBackOnEveryEnd ends jobs that hold the machine's lease in each way a job ends:
// it succeeds, fails, is cancelled while it runs, and cannot launch. After each, nobody holds the
// lease, and another daemon's measure takes it.
func TestLeasesAreGivenBackOnEveryEnd(t *testing.T) {
	place := &fakelease.Place{}
	w, a, _ := daemonOf(t, place, "alice", 101)
	ctx := ctxFor(t)
	other := place.Daemon()
	takeable := func(how string) {
		t.Helper()
		awaitHeldNothing(t, place)
		l, err := other.Acquire(ctx, "machine", core.LeaseHolder{User: "bob"})
		if err != nil {
			t.Fatalf("after a job that %s, the lease is held: %v", how, err)
		}
		if err := l.Release(ctx); err != nil {
			t.Fatal(err)
		}
	}

	ok := submit(t, a, "mark", "alone", ";", "exit", "0")
	awaitState(t, a, ok.ID, core.JobSucceeded)
	takeable("succeeded")

	failed := submit(t, a, "mark", "alone", ";", "exit", "3")
	awaitState(t, a, failed.ID, core.JobFailed)
	takeable("failed")

	cancelled := submit(t, a, "mark", "alone", ";", "hold", "never", ";", "exit", "0")
	if !w.gates.Reached(ctx, "alone") {
		t.Fatalf("the measure did not start")
	}
	if _, err := a.Cancel(ctx, call, cancelled.ID, "stop"); err != nil {
		t.Fatal(err)
	}
	awaitFinal(t, a, cancelled.ID)
	takeable("was cancelled")

	// The daemon reads the task of a docker command, which this daemon has no environment to run.
	w.tasks.Put(string(call.Scope.Repository()), commit, tasktest.Scripted("task", passes, scores))
	out, err := a.Submit(ctx, app.SubmitRequest{Context: call, Spec: core.JobSpec{
		Commit: commit, Environment: core.EnvironmentDocker, Settings: core.CommandSettings{Image: core.ImageHarness, Argv: []string{"mark", "alone"}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	awaitFinal(t, a, out.Job.ID)
	takeable("could not launch")
}

// TestARestartedDaemonTakesTheLeasesOfWhatItAdopts stops a daemon while its measure runs, which
// gives back the lease as its death would. The next daemon adopts the measure and takes the lease
// again; when another daemon took it in between, the measure goes on and the daemon reports the
// conflict until the measure ends.
func TestARestartedDaemonTakesTheLeasesOfWhatItAdopts(t *testing.T) {
	for _, taken := range []bool{false, true} {
		name := "free"
		if taken {
			name = "taken meanwhile"
		}
		t.Run(name, func(t *testing.T) {
			place := &fakelease.Place{}
			w, first, _ := daemonOf(t, place, "alice", 101)
			ctx := ctxFor(t)
			job := submit(t, first, "mark", "alone", ";", "hold", "g", ";", "exit", "0")
			if !w.gates.Reached(ctx, "alone") {
				t.Fatalf("the measure did not start")
			}
			first.Close()
			awaitHeldNothing(t, place)
			bob := core.LeaseHolder{User: "bob", PID: 202}
			if taken {
				l, err := place.Daemon().Acquire(ctx, "machine", bob)
				if err != nil {
					t.Fatal(err)
				}
				defer l.Release(ctx)
			}
			second := w.newApp()
			awaitState(t, second, job.ID, core.JobRunning)
			for {
				st := second.Status()
				if !taken && len(st.Leases) == 1 && st.Leases[0].Job == job.ID {
					break
				}
				if taken && len(st.Conflicts) == 1 && st.Conflicts[0].Job == job.ID && st.Conflicts[0].Holder.User == "bob" {
					break
				}
				if ctx.Err() != nil {
					t.Fatalf("the restarted daemon holds %+v with the conflicts %+v", st.Leases, st.Conflicts)
				}
				runtime.Gosched()
			}
			w.gates.Release("g")
			awaitState(t, second, job.ID, core.JobSucceeded)
			for st := second.Status(); len(st.Leases)+len(st.Conflicts) > 0; st = second.Status() {
				if ctx.Err() != nil {
					t.Fatalf("after the measure ended the daemon holds %+v with the conflicts %+v", st.Leases, st.Conflicts)
				}
				runtime.Gosched()
			}
		})
	}
}
