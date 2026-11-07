package core_test

import (
	"errors"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// failureOf is the *core.Failure an error is, or nil.
func failureOf(err error) *core.Failure {
	var f *core.Failure
	if errors.As(err, &f) {
		return f
	}
	return nil
}

func TestPlanLinkRefusesBothFlagsAndNamesBoth(t *testing.T) {
	_, err := core.NewPlanLink(3, true)
	f := failureOf(err)
	if f == nil || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "--plan") || !strings.Contains(f.Message, "--no-plan") {
		t.Fatalf("NewPlanLink(3, true) = %v, want invalid input naming --plan and --no-plan", err)
	}
}

// The link a caller's two statements make says what they said, over every pair: a plan, none,
// nothing, and never both.
func TestPlanLinkSaysExactlyWhatTheCallerSaid(t *testing.T) {
	for _, plan := range []core.PlanNumber{0, 1, 7, 1 << 31} {
		for _, noPlan := range []bool{false, true} {
			link, err := core.NewPlanLink(plan, noPlan)
			if both := plan != 0 && noPlan; both {
				if err == nil {
					t.Errorf("NewPlanLink(%d, %v) accepted both statements", plan, noPlan)
				}
				continue
			}
			if err != nil {
				t.Errorf("NewPlanLink(%d, %v): %v", plan, noPlan, err)
				continue
			}
			if link.Plan() != plan || link.Said() != (plan != 0 || noPlan) {
				t.Errorf("NewPlanLink(%d, %v) = plan %d, said %v", plan, noPlan, link.Plan(), link.Said())
			}
		}
	}
}

// Over every kind of job, caller and statement, a submission is refused exactly when it says
// --no-plan on a job that is not a measure, or says nothing on a measure from a run, and the
// refusal names the flags the caller should have given.
func TestAPlanLinkIsCheckedAgainstTheKindOfJobAndTheCaller(t *testing.T) {
	kinds := []core.JobKind{core.JobCheck, core.JobMeasure, core.JobCommand}
	for _, kind := range kinds {
		for _, run := range []core.RunID{"", "run-1"} {
			for _, plan := range []core.PlanNumber{0, 4} {
				for _, noPlan := range []bool{false, true} {
					link, err := core.NewPlanLink(plan, noPlan)
					if err != nil {
						continue // both statements: refused before a kind matters
					}
					err = link.Check(kind, run)
					var names []string
					switch {
					case noPlan && kind != core.JobMeasure:
						names = []string{"--no-plan"}
					case kind == core.JobMeasure && run != "" && plan == 0 && !noPlan:
						names = []string{"--plan", "--no-plan"}
					}
					f := failureOf(err)
					if names == nil {
						if err != nil {
							t.Errorf("%s job, run %q, plan %d, no-plan %v: err = %v, want accepted", kind, run, plan, noPlan, err)
						}
						continue
					}
					if f == nil || f.Kind != core.FailureInvalidInput {
						t.Errorf("%s job, run %q, plan %d, no-plan %v: err = %v, want invalid input", kind, run, plan, noPlan, err)
						continue
					}
					for _, name := range names {
						if !strings.Contains(f.Message, name) {
							t.Errorf("%s job, run %q, plan %d, no-plan %v: %q does not name %s", kind, run, plan, noPlan, f.Message, name)
						}
					}
				}
			}
		}
	}
}
