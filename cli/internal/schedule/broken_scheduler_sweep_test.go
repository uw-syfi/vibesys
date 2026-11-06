//go:build !race

package schedule_test

import (
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/policy/fifo"
	"github.com/uw-syfi/vibesys/cli/internal/schedule/scheduletest"
)

// The contract catches a broken scheduler: each mutant breaks one rule the way a bug in Step would,
// and the property that owns the rule must find it.
func TestTheContractCatchesABrokenScheduler(t *testing.T) {
	type step = func(schedule.State, schedule.Rules) []schedule.Decision
	mutants := map[string]struct {
		step     step
		policy   func() schedule.Policy
		fair     bool
		property string
	}{
		// Every job that can ever start starts now, whatever is held.
		"overcommits capacity": {
			step: func(s schedule.State, rules schedule.Rules) []schedule.Decision {
				return mapDecisions(schedule.Step(s, rules), func(d schedule.Decision) schedule.Decision {
					if w, ok := d.(schedule.Wait); ok && w.Reason.Kind == core.WaitFull {
						return schedule.Start{ID: w.ID, Option: optionOf(s, w.ID, w.Reason.Resource)}
					}
					return d
				})
			},
			policy: fifo.New, property: "no resource is given past its capacity",
		},
		// A job starts beside one that holds the resource alone.
		"shares an exclusive claim": {
			step: func(s schedule.State, rules schedule.Rules) []schedule.Decision {
				return mapDecisions(schedule.Step(s, rules), func(d schedule.Decision) schedule.Decision {
					if w, ok := d.(schedule.Wait); ok && (w.Reason.Kind == core.WaitHeldExclusively || w.Reason.Kind == core.WaitInUse) {
						return schedule.Start{ID: w.ID, Option: optionOf(s, w.ID, w.Reason.Resource)}
					}
					return d
				})
			},
			policy: fifo.New, property: "an exclusive claim shares its resource with no other",
		},
		// A job that waits is dropped from the decisions.
		"forgets a waiting job": {
			step: func(s schedule.State, rules schedule.Rules) []schedule.Decision {
				return slices.DeleteFunc(schedule.Step(s, rules), func(d schedule.Decision) bool {
					_, wait := d.(schedule.Wait)
					return wait
				})
			},
			policy: fifo.New, property: "every waiting job starts, waits for a true reason or is refused",
		},
		// A later job takes the resource an earlier one waits for.
		"jumps the queue": {
			step: func(s schedule.State, rules schedule.Rules) []schedule.Decision {
				return mapDecisions(schedule.Step(s, rules), func(d schedule.Decision) schedule.Decision {
					if w, ok := d.(schedule.Wait); ok && w.Reason.Kind == core.WaitBehind {
						return schedule.Start{ID: w.ID, Option: optionOf(s, w.ID, w.Reason.Resource)}
					}
					return d
				})
			},
			policy: fifo.New, property: "a job that waits for a resource keeps its turn on it",
		},
		// A job starts though another daemon holds the lease of its claim.
		"ignores a lease held elsewhere": {
			step: func(s schedule.State, rules schedule.Rules) []schedule.Decision {
				s.Waiting = slices.Clone(s.Waiting)
				for i := range s.Waiting {
					s.Waiting[i].Leased = nil
				}
				return schedule.Step(s, rules)
			},
			policy: fifo.New, property: "a job refused a lease waits for it, naming its holder",
		},
		// A job starts on a placement it may not run on: the first option of the next job.
		"starts a job where it may not run": {
			step: func(s schedule.State, rules schedule.Rules) []schedule.Decision {
				ds := schedule.Step(s, rules)
				return mapDecisions(ds, func(d schedule.Decision) schedule.Decision {
					st, ok := d.(schedule.Start)
					if !ok {
						return d
					}
					for _, w := range s.Waiting {
						if w.ID != st.ID && len(w.Options) > 0 && w.Options[0].Placement != st.Option.Placement && reflect.DeepEqual(w.Options[0].Claims, st.Option.Claims) {
							return schedule.Start{ID: st.ID, Option: w.Options[0]}
						}
					}
					return d
				})
			},
			policy: fifo.New, property: "a job starts on one of its options, and never one it cannot hold",
		},
		// A held job starts in its turn, as if no one held it.
		"ignores a hold": {
			step: func(s schedule.State, rules schedule.Rules) []schedule.Decision {
				s.Waiting = slices.Clone(s.Waiting)
				for i := range s.Waiting {
					s.Waiting[i].Hold = nil
				}
				return schedule.Step(s, rules)
			},
			policy: fifo.New, property: "a held job, and any but a First one while the daemon drains, waits and keeps no turn",
		},
		// A draining daemon starts jobs as if it were not draining.
		"ignores a drain": {
			step: func(s schedule.State, rules schedule.Rules) []schedule.Decision {
				s.Drain = nil
				return schedule.Step(s, rules)
			},
			policy: fifo.New, property: "a held job, and any but a First one while the daemon drains, waits and keeps no turn",
		},
		// A held job keeps its turn, so the jobs after it that need what it would run on wait behind
		// it.
		"a held job keeps its turn": {
			step: func(s schedule.State, rules schedule.Rules) []schedule.Decision {
				reserved := map[schedule.ResourceID]schedule.ID{}
				return mapDecisions(schedule.Step(s, rules), func(d schedule.Decision) schedule.Decision {
					i := slices.IndexFunc(s.Waiting, func(c schedule.Candidate) bool { return c.ID == d.Target() })
					switch d := d.(type) {
					case schedule.Wait:
						if s.Waiting[i].Hold != nil && d.Reason.Kind == core.WaitHeld {
							for _, o := range s.Waiting[i].Options {
								for _, c := range o.Claims {
									if _, ok := reserved[c.Resource]; !ok {
										reserved[c.Resource] = d.ID
									}
								}
							}
						}
					case schedule.Start:
						for _, c := range d.Option.Claims {
							if held, ok := reserved[c.Resource]; ok {
								return schedule.Wait{ID: d.ID, Reason: schedule.WaitReason{Kind: core.WaitBehind, Resource: c.Resource, Blockers: []schedule.ID{held}}}
							}
						}
					}
					return d
				})
			},
			policy: fifo.New, property: "a held job, and any but a First one while the daemon drains, waits and keeps no turn",
		},
		// Priority is left out: the jobs are considered in the policy's order alone.
		"ignores priority": {
			step: func(s schedule.State, rules schedule.Rules) []schedule.Decision {
				s.Waiting = slices.Clone(s.Waiting)
				for i := range s.Waiting {
					s.Waiting[i].Priority = ""
				}
				s.Now = time.Time{}
				return schedule.Step(s, rules)
			},
			policy: fifo.New, property: "jobs are considered by aged priority, and in the policy's order within one",
		},
		// A low job never counts higher, however long it waits.
		"never ages a priority": {
			step: func(s schedule.State, rules schedule.Rules) []schedule.Decision {
				s.Now = time.Time{}
				return schedule.Step(s, rules)
			},
			policy: fifo.New, property: "jobs are considered by aged priority, and in the policy's order within one",
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

// A placer decides where a job runs, never whether it may: one that ranks the options the wrong
// way round, drops them or makes some up leaves every rule of the contract holding.
func TestABrokenPlacerCannotBreakTheRules(t *testing.T) {
	placers := map[string]schedule.Placer{
		"most loaded first":  reversed{},
		"drops every option": dropping{},
		"makes options up":   inventing{},
	}
	for name, p := range placers {
		t.Run(name, func(t *testing.T) {
			scheduletest.Run(t, scheduletest.Contract{New: fifo.New, Placer: func() schedule.Placer { return p }})
		})
	}
}
