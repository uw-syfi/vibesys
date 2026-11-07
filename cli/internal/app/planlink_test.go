package app_test

import (
	"fmt"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// A submission says what plan its job tests: --plan N, --no-plan, or nothing. These tests hold the
// daemon to one table of answers, over every kind of job, caller and plan the flags can combine.

// planLinkCase is one submission: the kind of job, whether the caller is in a run, what it said
// about the plan, and whether the plan it named exists.
type planLinkCase struct {
	kind   core.JobKind
	inRun  bool
	plan   bool // --plan is given
	noPlan bool // --no-plan is given
	exists bool // the plan --plan names is in the task
}

func (c planLinkCase) String() string {
	return fmt.Sprintf("%s job, in a run %v, --plan %v (exists %v), --no-plan %v", c.kind, c.inRun, c.plan, c.exists, c.noPlan)
}

// planLinkAnswer is what the table says a submission gets: accepted, or refused with the failure
// kind and the flags its message names.
type planLinkAnswer struct {
	refused bool
	kind    core.FailureKind
	names   []string
}

// answerOf is the table, written out from the rules and not from the code under test: the two flags
// together are refused first, then --no-plan on anything but a measure job, then a plan that is not
// there. Everything else is accepted.
func answerOf(c planLinkCase) planLinkAnswer {
	switch {
	case c.plan && c.noPlan:
		return planLinkAnswer{true, core.FailureInvalidInput, []string{"--plan", "--no-plan"}}
	case c.noPlan && c.kind != core.JobMeasure:
		return planLinkAnswer{true, core.FailureInvalidInput, []string{"--no-plan"}}
	case c.plan && !c.exists:
		return planLinkAnswer{true, core.FailureNotFound, []string{"--plan"}}
	}
	return planLinkAnswer{}
}

// submitting runs one case on a daemon of its own and returns the job it made, or the refusal.
func submitting(t *testing.T, c planLinkCase) (core.Job, error) {
	t.Helper()
	w := newWorld(t)
	w.withTask(passes, scores)
	a := w.newApp()
	caller := call
	if !c.inRun {
		caller.Run = ""
	}
	ctx := ctxFor(t)
	existing, err := a.CreatePlan(ctx, caller, "an idea", "body", nil, 0)
	if err != nil {
		t.Fatalf("CreatePlan: %v", err)
	}
	req := app.SubmitRequest{Context: caller, NoPlan: c.noPlan}
	switch c.kind {
	case core.JobCheck:
		req.Spec = flowSpec(core.CheckSettings{})
	case core.JobMeasure:
		req.Spec = flowSpec(core.MeasureSettings{Samples: 1})
	default:
		req.Spec = command("exit", "0")
	}
	if c.plan {
		req.Plan = existing.Number
		if !c.exists {
			req.Plan = existing.Number + 41
		}
	}
	out, err := a.Submit(ctx, req)
	return out.Job, err
}

// Every combination of the flags is answered as the table says, and what is accepted has the link
// that was asked for and nothing more.
func TestAPlanLinkIsAcceptedOrRefusedAsTheTableSays(t *testing.T) {
	for _, kind := range []core.JobKind{core.JobCheck, core.JobMeasure, core.JobCommand} {
		for bits := range 16 {
			c := planLinkCase{kind: kind, inRun: bits&1 != 0, plan: bits&2 != 0, noPlan: bits&4 != 0, exists: bits&8 != 0}
			if !c.plan && !c.exists {
				continue // whether a plan exists matters only to a caller that names one
			}
			t.Run(c.String(), func(t *testing.T) {
				job, err := submitting(t, c)
				want := answerOf(c)
				if want.refused {
					f := wantFailure(t, "the submission", err, want.kind)
					for _, name := range want.names {
						if !strings.Contains(f.Message, name) {
							t.Errorf("the refusal %q does not name %s", f.Message, name)
						}
					}
					return
				}
				if err != nil {
					t.Fatalf("Submit: %v, want accepted", err)
				}
				wantPlans := []core.PlanNumber(nil)
				if c.plan {
					wantPlans = []core.PlanNumber{1}
				}
				if !slices.Equal(job.Plans, wantPlans) {
					t.Errorf("the job is linked to plans %v, want %v", job.Plans, wantPlans)
				}
			})
		}
	}
}

// The named cases of the table, as the people who wrote the flags would say them.
func TestPlanLinkExamples(t *testing.T) {
	cases := []struct {
		name  string
		c     planLinkCase
		fails core.FailureKind
		names []string
	}{
		{"a measure from a run linked to its plan", planLinkCase{kind: core.JobMeasure, inRun: true, plan: true, exists: true}, "", nil},
		{"a baseline measure that says it tests no plan", planLinkCase{kind: core.JobMeasure, inRun: true, noPlan: true}, "", nil},
		{"a person measuring at a terminal", planLinkCase{kind: core.JobMeasure}, "", nil},
		{"both flags", planLinkCase{kind: core.JobMeasure, inRun: true, plan: true, exists: true, noPlan: true}, core.FailureInvalidInput, []string{"--plan", "--no-plan"}},
		{"a plan that is not there", planLinkCase{kind: core.JobMeasure, inRun: true, plan: true}, core.FailureNotFound, []string{"--plan"}},
		{"no plan on a check", planLinkCase{kind: core.JobCheck, inRun: true, noPlan: true}, core.FailureInvalidInput, []string{"--no-plan"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := submitting(t, c.c)
			if c.fails == "" {
				if err != nil {
					t.Fatalf("Submit: %v, want accepted", err)
				}
				return
			}
			f := wantFailure(t, c.name, err, c.fails)
			for _, name := range c.names {
				if !strings.Contains(f.Message, name) {
					t.Errorf("the refusal %q does not name %s", f.Message, name)
				}
			}
		})
	}
}
