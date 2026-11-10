package alloc_test

import (
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/alloc"
)

// reach is an allocation of run-a in a stage: on gpu (12h), with a job running in it (j1) and one
// queued (j2) where it is started, unless bare.
type reach func(w *world) alloc.ID

func pending(w *world) alloc.ID {
	id := w.ask(gpu, 12*time.Hour)
	w.took(id)
	return id
}

func runningWithJobs(w *world) alloc.ID {
	id := w.running(gpu, 12*time.Hour)
	w.submit("j1", time.Hour, id)
	w.submit("j2", time.Hour, id)
	return id
}

func runningBare(w *world) alloc.ID { return w.running(gpu, 12*time.Hour) }

func draining(w *world) alloc.ID {
	id := runningWithJobs(w)
	w.accept(alloc.EndAsk{Run: "run-a", ID: id, AfterJobs: true})
	return id
}

// Every arrow of the state diagram, each from the event that makes it: the stage it ends in, the
// end reason, and the actions it asks for.
func TestEveryTransitionOfTheDiagram(t *testing.T) {
	cases := []struct {
		name   string
		from   reach
		event  func(id alloc.ID) alloc.Event
		stage  alloc.Stage
		reason alloc.EndReason
		want   func(id alloc.ID) []alloc.Action
	}{
		{
			name: "pending starts", from: pending,
			event: func(id alloc.ID) alloc.Event { return alloc.Observed{ID: id, Seen: alloc.SeenRunning{Node: "n"}} },
			stage: alloc.StageRunning,
			want: func(id alloc.ID) []alloc.Action {
				return []alloc.Action{alloc.RecordWait{Placement: gpu, Sample: alloc.WaitSample{RequestedAt: epoch, Waited: time.Hour, RequestedFor: 12 * time.Hour}}}
			},
		},
		{
			name: "pending is ended", from: pending,
			event: func(id alloc.ID) alloc.Event { return alloc.EndAsk{Run: "run-a", ID: id} },
			stage: alloc.StageEnded, reason: alloc.EndRequested,
			want: func(id alloc.ID) []alloc.Action { return []alloc.Action{alloc.Release{ID: id, Handle: handle(id)}} },
		},
		{
			name: "pending is ended after its jobs, of which it has none", from: pending,
			event: func(id alloc.ID) alloc.Event { return alloc.EndAsk{Run: "run-a", ID: id, AfterJobs: true} },
			stage: alloc.StageEnded, reason: alloc.EndRequested,
			want: func(id alloc.ID) []alloc.Action { return []alloc.Action{alloc.Release{ID: id, Handle: handle(id)}} },
		},
		{
			name: "pending is cancelled outside", from: pending,
			event: func(id alloc.ID) alloc.Event {
				return alloc.Observed{ID: id, Seen: alloc.SeenEnded{Cause: alloc.EndedOtherwise}}
			},
			stage: alloc.StageEnded, reason: alloc.EndExternal,
		},
		{
			name: "pending is refused by the backend", from: func(w *world) alloc.ID { return w.ask(gpu, time.Hour) },
			event: func(id alloc.ID) alloc.Event { return alloc.SubmitRefused{ID: id, Detail: "no such account"} },
			stage: alloc.StageEnded, reason: alloc.EndExternal,
		},
		{
			name: "running drains", from: runningWithJobs,
			event: func(id alloc.ID) alloc.Event { return alloc.EndAsk{Run: "run-a", ID: id, AfterJobs: true} },
			stage: alloc.StageDraining,
		},
		{
			name: "running with nothing to drain ends", from: runningBare,
			event: func(id alloc.ID) alloc.Event { return alloc.EndAsk{Run: "run-a", ID: id, AfterJobs: true} },
			stage: alloc.StageEnded, reason: alloc.EndRequested,
			want: func(id alloc.ID) []alloc.Action { return []alloc.Action{alloc.Release{ID: id, Handle: handle(id)}} },
		},
		{
			name: "running is ended", from: runningWithJobs,
			event: func(id alloc.ID) alloc.Event { return alloc.EndAsk{Run: "run-a", ID: id} },
			stage: alloc.StageEnded, reason: alloc.EndRequested,
			want: func(id alloc.ID) []alloc.Action {
				return []alloc.Action{
					alloc.LoseJob{Job: "j1", ID: id, Reason: alloc.EndRequested},
					alloc.DropJob{Job: "j2", ID: id, Reason: alloc.NotRunning{ID: id, Stage: alloc.StageEnded, EndReason: alloc.EndRequested}},
					alloc.Release{ID: id, Handle: handle(id)},
				}
			},
		},
		{
			name: "running reaches its time limit", from: runningWithJobs,
			event: func(id alloc.ID) alloc.Event {
				return alloc.Observed{ID: id, Seen: alloc.SeenEnded{Cause: alloc.EndedByTimeLimit}}
			},
			stage: alloc.StageEnded, reason: alloc.EndTimeLimit,
			want: func(id alloc.ID) []alloc.Action {
				return []alloc.Action{
					alloc.LoseJob{Job: "j1", ID: id, Reason: alloc.EndTimeLimit},
					alloc.DropJob{Job: "j2", ID: id, Reason: alloc.NotRunning{ID: id, Stage: alloc.StageEnded, EndReason: alloc.EndTimeLimit}},
				}
			},
		},
		{
			name: "running is cancelled outside", from: runningWithJobs,
			event: func(id alloc.ID) alloc.Event {
				return alloc.Observed{ID: id, Seen: alloc.SeenEnded{Cause: alloc.EndedOtherwise}}
			},
			stage: alloc.StageEnded, reason: alloc.EndExternal,
		},
		{
			name: "draining finishes its last job", from: func(w *world) alloc.ID {
				id := draining(w)
				w.do(alloc.JobFinished{Job: "j1"})
				return id
			},
			event: func(alloc.ID) alloc.Event { return alloc.JobFinished{Job: "j2"} },
			stage: alloc.StageEnded, reason: alloc.EndRequested,
			want: func(id alloc.ID) []alloc.Action { return []alloc.Action{alloc.Release{ID: id, Handle: handle(id)}} },
		},
		{
			name: "draining is ended at once", from: draining,
			event: func(id alloc.ID) alloc.Event { return alloc.EndAsk{Run: "run-a", ID: id} },
			stage: alloc.StageEnded, reason: alloc.EndRequested,
		},
		{
			name: "draining reaches its time limit", from: draining,
			event: func(id alloc.ID) alloc.Event {
				return alloc.Observed{ID: id, Seen: alloc.SeenEnded{Cause: alloc.EndedByTimeLimit}}
			},
			stage: alloc.StageEnded, reason: alloc.EndTimeLimit,
			want: func(id alloc.ID) []alloc.Action {
				return []alloc.Action{alloc.LoseJob{Job: "j1", ID: id, Reason: alloc.EndTimeLimit}}
			},
		},
		{
			name: "draining is cancelled outside", from: draining,
			event: func(id alloc.ID) alloc.Event {
				return alloc.Observed{ID: id, Seen: alloc.SeenEnded{Cause: alloc.EndedOtherwise}}
			},
			stage: alloc.StageEnded, reason: alloc.EndExternal,
		},
		{
			name: "the run ends while it runs", from: runningWithJobs,
			event: func(alloc.ID) alloc.Event { return alloc.RunEnded{Run: "run-a"} },
			stage: alloc.StageEnded, reason: alloc.EndRequested,
		},
		{
			name: "the run ends while it waits", from: pending,
			event: func(alloc.ID) alloc.Event { return alloc.RunEnded{Run: "run-a"} },
			stage: alloc.StageEnded, reason: alloc.EndRequested,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			w := newWorld(t)
			id := c.from(w)
			w.pass(time.Hour)
			actions := w.do(c.event(id))
			if got := w.stage(id); got != c.stage {
				t.Fatalf("%s is %s, want %s", id, got, c.stage)
			}
			if c.stage == alloc.StageEnded {
				if got := w.ended(id); got.Reason != c.reason || !got.At.Equal(w.now) {
					t.Fatalf("ended %+v, want for %s at %s", got, c.reason, w.now)
				}
			}
			if c.want != nil {
				for _, want := range c.want(id) {
					if !has(actions, want) {
						t.Errorf("actions %+v lack %+v", actions, want)
					}
				}
			}
		})
	}
}

func handle(id alloc.ID) alloc.Handle { return alloc.Handle{Alloc: id, Ref: "ref-" + string(id)} }

// What must not move an allocation: an unreachable backend in every stage, an observation of an
// earlier stage, and anything said of an ended one.
func TestWhatLeavesTheStageAsItIs(t *testing.T) {
	stays := map[string]struct {
		from  reach
		event func(id alloc.ID) alloc.Event
	}{
		"pending, unreachable":   {pending, unreachable},
		"running, unreachable":   {runningWithJobs, unreachable},
		"draining, unreachable":  {draining, unreachable},
		"running, seen pending":  {runningWithJobs, seen(alloc.SeenPending{Reason: "Priority"})},
		"draining, seen running": {draining, seen(alloc.SeenRunning{Node: "n"})},
		"draining, seen pending": {draining, seen(alloc.SeenPending{})},
		"ended, seen running": {func(w *world) alloc.ID {
			id := runningBare(w)
			w.accept(alloc.EndAsk{Run: "run-a", ID: id})
			return id
		}, seen(alloc.SeenRunning{})},
		"ended, seen pending": {func(w *world) alloc.ID {
			id := pending(w)
			w.accept(alloc.EndAsk{Run: "run-a", ID: id})
			return id
		}, seen(alloc.SeenPending{})},
		"ended, unreachable": {func(w *world) alloc.ID {
			id := runningBare(w)
			w.do(alloc.Observed{ID: id, Seen: alloc.SeenEnded{Cause: alloc.EndedByTimeLimit}})
			return id
		}, unreachable},
		"pending, submit refused after it was taken": {pending, func(id alloc.ID) alloc.Event { return alloc.SubmitRefused{ID: id} }},
	}
	for name, c := range stays {
		t.Run(name, func(t *testing.T) {
			w := newWorld(t)
			id := c.from(w)
			before := w.get(id)
			w.pass(30 * time.Minute)
			w.do(c.event(id))
			if got := w.get(id); got.Stage() != before.Stage() {
				t.Fatalf("%s went from %s to %s", id, before.Stage(), got.Stage())
			}
		})
	}
}

func unreachable(id alloc.ID) alloc.Event {
	return alloc.Unreachable{ID: id, Detail: "ssh: connection refused"}
}

func seen(o alloc.Observation) func(alloc.ID) alloc.Event {
	return func(id alloc.ID) alloc.Event { return alloc.Observed{ID: id, Seen: o} }
}

// Every refusal, from the request that earns it, with what it says.
func TestEveryRefusal(t *testing.T) {
	cases := []struct {
		name  string
		setup func(w *world) alloc.ID
		event func(id alloc.ID, w *world) alloc.Event
		want  alloc.Refusal
		says  string
	}{
		{
			name: "a placement with no allocations section",
			event: func(alloc.ID, *world) alloc.Event {
				return alloc.Ask{Run: "run-a", Placement: off, For: time.Hour}
			},
			want: alloc.NotEnabled{Placement: off, Key: "placements.off.allocations"}, says: "placements.off.allocations",
		},
		{
			name:  "no time",
			event: func(alloc.ID, *world) alloc.Event { return alloc.Ask{Run: "run-a", Placement: gpu} },
			want:  alloc.ForNotPositive{},
		},
		{
			name: "longer than max_for",
			event: func(alloc.ID, *world) alloc.Event {
				return alloc.Ask{Run: "run-a", Placement: gpu, For: 25 * time.Hour}
			},
			want: alloc.ForExceedsMaxFor{For: 25 * time.Hour, MaxFor: 24 * time.Hour, Key: "placements.gpu.allocations.max_for"},
			says: "placements.gpu.allocations.max_for = 24h0m0s",
		},
		{
			name: "max_held reached",
			setup: func(w *world) alloc.ID {
				w.ask(gpu, time.Hour)
				return w.running(gpu, time.Hour)
			},
			event: func(alloc.ID, *world) alloc.Event { return alloc.Ask{Run: "run-a", Placement: gpu, For: time.Hour} },
			want:  alloc.MaxHeldReached{Placement: gpu, Held: 2, MaxHeld: 2}, says: "max_held is 2",
		},
		{
			name:  "an ask of a run that ended",
			setup: func(w *world) alloc.ID { w.do(alloc.RunEnded{Run: "run-a"}); return "" },
			event: func(alloc.ID, *world) alloc.Event { return alloc.Ask{Run: "run-a", Placement: gpu, For: time.Hour} },
			want:  alloc.RunNotLive{Run: "run-a"},
		},
		{
			name:  "an end of no allocation",
			event: func(alloc.ID, *world) alloc.Event { return alloc.EndAsk{Run: "run-a", ID: "a9"} },
			want:  alloc.UnknownAllocation{ID: "a9"},
		},
		{
			name:  "an end of another run's",
			setup: runningBare,
			event: func(id alloc.ID, _ *world) alloc.Event { return alloc.EndAsk{Run: "run-b", ID: id} },
			want:  alloc.NotOwnedByRun{ID: "a1", Owner: "run-a", Run: "run-b"},
		},
		{
			name:  "a renewal of another run's",
			setup: runningBare,
			event: func(id alloc.ID, _ *world) alloc.Event {
				return alloc.RenewAsk{Run: "run-b", ID: id, Order: order(time.Hour, 6*time.Hour)}
			},
			want: alloc.NotOwnedByRun{ID: "a1", Owner: "run-a", Run: "run-b"},
		},
		{
			name:  "a renewal off of no allocation",
			event: func(alloc.ID, *world) alloc.Event { return alloc.RenewOffAsk{Run: "run-a", ID: "a3"} },
			want:  alloc.UnknownAllocation{ID: "a3"},
		},
		{
			name:  "a job into a pending one",
			setup: pending,
			event: func(id alloc.ID, _ *world) alloc.Event { return job("j1", time.Hour, id) },
			want:  alloc.NotRunning{ID: "a1", Stage: alloc.StagePending}, says: "pending",
		},
		{
			name:  "a job into a draining one",
			setup: draining,
			event: func(id alloc.ID, _ *world) alloc.Event { return job("j3", time.Hour, id) },
			want:  alloc.NotRunning{ID: "a1", Stage: alloc.StageDraining},
		},
		{
			name: "a job into an ended one",
			setup: func(w *world) alloc.ID {
				id := runningBare(w)
				w.do(alloc.Observed{ID: id, Seen: alloc.SeenEnded{Cause: alloc.EndedByTimeLimit}})
				return id
			},
			event: func(id alloc.ID, _ *world) alloc.Event { return job("j1", time.Hour, id) },
			want:  alloc.NotRunning{ID: "a1", Stage: alloc.StageEnded, EndReason: alloc.EndTimeLimit}, says: "time_limit",
		},
		{
			name:  "a job into no allocation",
			event: func(alloc.ID, *world) alloc.Event { return job("j1", time.Hour, "a4") },
			want:  alloc.UnknownAllocation{ID: "a4"},
		},
		{
			name:  "a job into another run's",
			setup: runningBare,
			event: func(id alloc.ID, _ *world) alloc.Event {
				return alloc.JobSubmitted{Run: "run-b", Job: "j1", Bound: time.Hour, Into: id, Eligible: everywhere}
			},
			want: alloc.NotOwnedByRun{ID: "a1", Owner: "run-a", Run: "run-b"},
		},
		{
			name:  "a job its placement does not serve",
			setup: runningBare,
			event: func(id alloc.ID, _ *world) alloc.Event {
				return alloc.JobSubmitted{Run: "run-a", Job: "j1", Bound: time.Hour, Into: id, Eligible: []string{local}}
			},
			want: alloc.NotEligible{ID: "a1", Placement: gpu},
		},
		{
			name:  "a job that does not fit",
			setup: runningWithJobs, // 12h, j1 and j2 of 1h each ahead.
			event: func(id alloc.ID, _ *world) alloc.Event { return job("j3", 10*time.Hour, id) },
			want:  alloc.DoesNotFit{ID: "a1", TimeLeft: 12 * time.Hour, Ahead: 2 * time.Hour, Bound: 10 * time.Hour, Margin: 5 * time.Minute},
			says:  "12h0m0s left",
		},
		{
			name:  "a job of no bound",
			setup: runningBare,
			event: func(id alloc.ID, _ *world) alloc.Event { return job("j1", 0, id) },
			want:  alloc.BoundNotPositive{Job: "j1"},
		},
		{
			name:  "a job held already",
			setup: runningWithJobs,
			event: func(alloc.ID, *world) alloc.Event { return job("j2", time.Hour, "") },
			want:  alloc.DuplicateJob{Job: "j2", ID: "a1"},
		},
		{
			name:  "a job of a run that ended",
			setup: func(w *world) alloc.ID { w.do(alloc.RunEnded{Run: "run-a"}); return "" },
			event: func(alloc.ID, *world) alloc.Event { return job("j1", time.Hour, "") },
			want:  alloc.RunNotLive{Run: "run-a"},
		},
		{
			name:  "a renewal of a draining one",
			setup: draining,
			event: func(id alloc.ID, _ *world) alloc.Event { return renew(id, order(time.Hour, 6*time.Hour)) },
			want:  alloc.NotRenewable{ID: "a1", Stage: alloc.StageDraining},
		},
		{
			name:  "a renewal of one with no end",
			setup: func(w *world) alloc.ID { return w.running(local, time.Hour) },
			event: func(id alloc.ID, _ *world) alloc.Event { return renew(id, order(time.Hour, 6*time.Hour)) },
			want:  alloc.NoEnd{ID: "a1"},
		},
		{
			name:  "a renewal with no lead",
			setup: runningBare,
			event: func(id alloc.ID, _ *world) alloc.Event { return renew(id, order(0, 6*time.Hour)) },
			want:  alloc.LeadNotPositive{},
		},
		{
			name:  "a renewal whose lead is not shorter than its for",
			setup: runningBare,
			event: func(id alloc.ID, _ *world) alloc.Event { return renew(id, order(6*time.Hour, 6*time.Hour)) },
			want:  alloc.LeadNotShorterThanFor{Lead: 6 * time.Hour, For: 6 * time.Hour},
		},
		{
			name:  "a renewal longer than max_for",
			setup: runningBare,
			event: func(id alloc.ID, _ *world) alloc.Event { return renew(id, order(time.Hour, 48*time.Hour)) },
			want:  alloc.ForExceedsMaxFor{For: 48 * time.Hour, MaxFor: 24 * time.Hour, Key: "placements.gpu.allocations.max_for"},
		},
		{
			name:  "a renewal of no time",
			setup: runningBare,
			event: func(id alloc.ID, _ *world) alloc.Event { return renew(id, order(time.Hour, 0)) },
			want:  alloc.ForNotPositive{},
		},
		{
			name:  "a renewal whose until has passed",
			setup: runningBare,
			event: func(id alloc.ID, w *world) alloc.Event {
				o := order(time.Hour, 6*time.Hour)
				o.Until = alloc.UntilTime{At: w.now}
				return renew(id, o)
			},
			want: alloc.UntilPassed{At: epoch, Now: epoch},
		},
		{
			name:  "a renewal with no until",
			setup: runningBare,
			event: func(id alloc.ID, _ *world) alloc.Event {
				return renew(id, alloc.RenewalOrder{Lead: time.Hour, For: 6 * time.Hour})
			},
			want: alloc.NoUntil{},
		},
		{
			name: "a renewal of one already renewed",
			setup: func(w *world) alloc.ID {
				id := w.running(gpu, 2*time.Hour)
				w.accept(renew(id, order(3*time.Hour, 6*time.Hour)))
				return id
			},
			event: func(id alloc.ID, _ *world) alloc.Event { return renew(id, order(time.Hour, 6*time.Hour)) },
			want:  alloc.AlreadyRenewed{ID: "a1", By: "a2"},
		},
		{
			name:  "a renewal of a run that ended",
			setup: func(w *world) alloc.ID { id := runningBare(w); w.do(alloc.RunEnded{Run: "run-a"}); return id },
			event: func(id alloc.ID, _ *world) alloc.Event { return renew(id, order(time.Hour, 6*time.Hour)) },
			want:  alloc.RunNotLive{Run: "run-a"},
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			w := newWorld(t)
			var id alloc.ID
			if c.setup != nil {
				id = c.setup(w)
			}
			got := w.refuse(c.event(id, w), c.want)
			if got != c.want {
				t.Errorf("refused with %#v, want %#v", got, c.want)
			}
			if c.says != "" && !contains(got.Message(), c.says) {
				t.Errorf("%q does not say %q", got.Message(), c.says)
			}
		})
	}
}

func job(name string, bound time.Duration, into alloc.ID) alloc.JobSubmitted {
	return alloc.JobSubmitted{Run: "run-a", Job: coreJob(name), Bound: bound, Into: into, Eligible: everywhere}
}

func order(lead, d time.Duration) alloc.RenewalOrder {
	return alloc.RenewalOrder{Lead: lead, For: d, Until: alloc.UntilRunEnd{}}
}

func renew(id alloc.ID, o alloc.RenewalOrder) alloc.RenewAsk {
	return alloc.RenewAsk{Run: "run-a", ID: id, Order: o}
}
