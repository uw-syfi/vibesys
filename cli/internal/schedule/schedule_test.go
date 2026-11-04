package schedule_test

import (
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/policy/fifo"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/scheduletest"
)

// The contract catches a broken scheduler: each mutant breaks one rule the way a bug in Step would,
// and the property that owns the rule must find it.
func TestTheContractCatchesABrokenScheduler(t *testing.T) {
	type step = func(schedule.State, schedule.Policy, ...schedule.Filter) []schedule.Decision
	mutants := map[string]struct {
		step     step
		policy   func() schedule.Policy
		fair     bool
		property string
	}{
		// Every job that can ever start starts now, whatever is held.
		"overcommits capacity": {
			step: func(s schedule.State, p schedule.Policy, f ...schedule.Filter) []schedule.Decision {
				return mapDecisions(schedule.Step(s, p, f...), func(d schedule.Decision) schedule.Decision {
					if w, ok := d.(schedule.Wait); ok && w.Reason.Kind == core.WaitFull {
						return schedule.Start{ID: w.ID}
					}
					return d
				})
			},
			policy: fifo.New, property: "no resource is given past its capacity",
		},
		// A job starts beside one that holds the resource alone.
		"shares an exclusive claim": {
			step: func(s schedule.State, p schedule.Policy, f ...schedule.Filter) []schedule.Decision {
				return mapDecisions(schedule.Step(s, p, f...), func(d schedule.Decision) schedule.Decision {
					if w, ok := d.(schedule.Wait); ok && (w.Reason.Kind == core.WaitHeldExclusively || w.Reason.Kind == core.WaitInUse) {
						return schedule.Start{ID: w.ID}
					}
					return d
				})
			},
			policy: fifo.New, property: "an exclusive claim shares its resource with no other",
		},
		// A job that waits is dropped from the decisions.
		"forgets a waiting job": {
			step: func(s schedule.State, p schedule.Policy, f ...schedule.Filter) []schedule.Decision {
				return slices.DeleteFunc(schedule.Step(s, p, f...), func(d schedule.Decision) bool {
					_, wait := d.(schedule.Wait)
					return wait
				})
			},
			policy: fifo.New, property: "every waiting job starts, waits for a true reason or is refused",
		},
		// A later job takes the resource an earlier one waits for.
		"jumps the queue": {
			step: func(s schedule.State, p schedule.Policy, f ...schedule.Filter) []schedule.Decision {
				return mapDecisions(schedule.Step(s, p, f...), func(d schedule.Decision) schedule.Decision {
					if w, ok := d.(schedule.Wait); ok && w.Reason.Kind == core.WaitBehind {
						return schedule.Start{ID: w.ID}
					}
					return d
				})
			},
			policy: fifo.New, property: "a job that waits for a resource keeps its turn on it",
		},
		// A job starts though another daemon holds the lease of its claim.
		"ignores a lease held elsewhere": {
			step: func(s schedule.State, p schedule.Policy, f ...schedule.Filter) []schedule.Decision {
				s.Waiting = slices.Clone(s.Waiting)
				for i := range s.Waiting {
					s.Waiting[i].Leased = nil
				}
				return schedule.Step(s, p, f...)
			},
			policy: fifo.New, property: "a job refused a lease waits for it, naming its holder",
		},
		// A policy that always serves the run that came first starves the others.
		"starves a run": {
			policy: fifo.New, fair: true, property: "a run with work waiting starts within one turn of every run",
		},
	}
	for name, m := range mutants {
		t.Run(name, func(t *testing.T) {
			broken := scheduletest.Broken(scheduletest.Contract{New: m.policy, Fair: m.fair, Step: m.step})
			msg, caught := broken[m.property]
			if !caught {
				t.Fatalf("the property %q found nothing wrong; the contract found %v", m.property, broken)
			}
			t.Logf("caught: %s", msg)
		})
	}
}

func mapDecisions(ds []schedule.Decision, f func(schedule.Decision) schedule.Decision) []schedule.Decision {
	out := make([]schedule.Decision, len(ds))
	for i, d := range ds {
		out[i] = f(d)
	}
	return out
}
