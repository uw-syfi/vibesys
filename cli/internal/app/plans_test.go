package app_test

import (
	"errors"
	"math/rand/v2"
	"reflect"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func wantInvalid(t *testing.T, what string, err error) {
	t.Helper()
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
		t.Errorf("%s: err = %v, want invalid input", what, err)
	}
}

func wantNotFound(t *testing.T, what string, err error) {
	t.Helper()
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureNotFound {
		t.Errorf("%s: err = %v, want not found", what, err)
	}
}

func TestAPlanIsCreatedOpenAndNumberedInOrder(t *testing.T) {
	a := newWorld(t).newApp()
	ctx := ctxFor(t)
	for i := 1; i <= 3; i++ {
		p, err := a.CreatePlan(ctx, call, "title", "# body\n")
		if err != nil || p.Number != core.PlanNumber(i) || p.Status != core.PlanOpen || p.Body != "# body\n" {
			t.Fatalf("plan %d = %+v, %v", i, p, err)
		}
	}
}

// What an agent writes in a plan is what it reads back, and an edit, a comment and a close each
// change only what they name. The model is a plain slice of plans.
func TestPlansFollowAModelUnderRandomCalls(t *testing.T) {
	for seed := uint64(1); seed <= 20; seed++ {
		rnd := rand.New(rand.NewPCG(seed, seed))
		w := newWorld(t)
		a := w.newApp()
		ctx := ctxFor(t)
		var model []core.Plan
		for step := 0; step < 60; step++ {
			switch op := rnd.IntN(5); {
			case op == 0 || len(model) == 0:
				p, err := a.CreatePlan(ctx, call, "t", "b")
				if err != nil {
					t.Fatalf("seed %d: CreatePlan: %v", seed, err)
				}
				model = append(model, p)
			default:
				i := rnd.IntN(len(model))
				n := model[i].Number
				var (
					got core.Plan
					err error
				)
				switch op {
				case 1:
					title, body := "title"+string(rune('a'+step%26)), "body\n"+strings.Repeat("x", step)
					got, err = a.EditPlan(ctx, call, n, app.PlanChange{Title: &title, Body: &body})
					model[i].Title, model[i].Body = title, body
				case 2:
					got, err = a.CommentPlan(ctx, call, n, "decided")
					model[i].Comments = append(model[i].Comments, core.PlanComment{Text: "decided", Agent: call.Agent})
				case 3:
					status := core.PlanStatuses()[rnd.IntN(5)]
					got, err = a.EditPlan(ctx, call, n, app.PlanChange{Status: &status})
					model[i].Status = status
				default:
					status := core.PlanStatuses()[1+rnd.IntN(4)]
					got, err = a.ClosePlan(ctx, call, n, status, "because")
					model[i].Status = status
					model[i].Comments = append(model[i].Comments, core.PlanComment{Text: "because", Agent: call.Agent})
				}
				if err != nil {
					t.Fatalf("seed %d step %d op %d: %v", seed, step, op, err)
				}
				model[i].UpdatedAt = got.UpdatedAt
				for j := range model[i].Comments {
					model[i].Comments[j].At = got.Comments[j].At
				}
				if !reflect.DeepEqual(got, model[i]) {
					t.Fatalf("seed %d step %d: plan = %+v, want %+v", seed, step, got, model[i])
				}
			}
		}
		all, err := a.Plans(ctx, scope, core.PlanFilter{})
		if err != nil || !reflect.DeepEqual(all, model) {
			t.Fatalf("seed %d: Plans = %+v, %v; want %+v", seed, all, err, model)
		}
	}
}

func TestPlansFilterByStatusAndText(t *testing.T) {
	a := newWorld(t).newApp()
	ctx := ctxFor(t)
	one, _ := a.CreatePlan(ctx, call, "batch the reads", "use io_uring")
	two, _ := a.CreatePlan(ctx, call, "shard the index", "split by key")
	three, _ := a.CreatePlan(ctx, call, "inline hot path", "batch nothing")
	if _, err := a.ClosePlan(ctx, call, two.Number, core.PlanWon, ""); err != nil {
		t.Fatal(err)
	}
	if _, err := a.ClosePlan(ctx, call, three.Number, core.PlanRejected, "no gain"); err != nil {
		t.Fatal(err)
	}
	numbers := func(f core.PlanFilter) []core.PlanNumber {
		plans, err := a.Plans(ctx, scope, f)
		if err != nil {
			t.Fatal(err)
		}
		var out []core.PlanNumber
		for _, p := range plans {
			out = append(out, p.Number)
		}
		return out
	}
	for _, tc := range []struct {
		filter core.PlanFilter
		want   []core.PlanNumber
	}{
		{core.PlanFilter{}, []core.PlanNumber{1, 2, 3}},
		{core.PlanFilter{Statuses: []core.PlanStatus{core.PlanOpen}}, []core.PlanNumber{one.Number}},
		{core.PlanFilter{Statuses: []core.PlanStatus{core.PlanWon, core.PlanRejected}}, []core.PlanNumber{2, 3}},
		{core.PlanFilter{Text: "batch"}, []core.PlanNumber{1, 3}},
		{core.PlanFilter{Text: "batch", Statuses: []core.PlanStatus{core.PlanRejected}}, []core.PlanNumber{3}},
		{core.PlanFilter{Statuses: []core.PlanStatus{core.PlanParked}}, nil},
	} {
		if got := numbers(tc.filter); !reflect.DeepEqual(got, tc.want) {
			t.Errorf("filter %+v = %v, want %v", tc.filter, got, tc.want)
		}
	}
	_, err := a.Plans(ctx, scope, core.PlanFilter{Statuses: []core.PlanStatus{"stuck"}})
	wantInvalid(t, "an unknown status filter", err)
}

// A value the daemon refuses changes nothing: the plan reads back as it was, with its update time.
func TestARefusedPlanCallChangesNothing(t *testing.T) {
	a := newWorld(t).newApp()
	ctx := ctxFor(t)
	p, _ := a.CreatePlan(ctx, call, "title", "body")
	empty, long, bad := "", strings.Repeat("t", core.MaxPlanTitleBytes+1), "ok\xff"
	multi := "two\nlines"
	stuck := core.PlanStatus("stuck")

	_, err := a.CreatePlan(ctx, call, empty, "b")
	wantInvalid(t, "an empty title", err)
	_, err = a.CreatePlan(ctx, call, "   ", "b")
	wantInvalid(t, "a blank title", err)
	_, err = a.CreatePlan(ctx, call, multi, "b")
	wantInvalid(t, "a two-line title", err)
	_, err = a.CreatePlan(ctx, call, long, "b")
	wantInvalid(t, "a long title", err)
	_, err = a.CreatePlan(ctx, call, "t", bad)
	wantInvalid(t, "a body that is not UTF-8", err)
	_, err = a.CreatePlan(ctx, call, "t", strings.Repeat("b", core.MaxPlanBodyBytes+1))
	wantInvalid(t, "a long body", err)

	_, err = a.EditPlan(ctx, call, p.Number, app.PlanChange{})
	wantInvalid(t, "an edit that sets nothing", err)
	_, err = a.EditPlan(ctx, call, p.Number, app.PlanChange{Title: &empty})
	wantInvalid(t, "an edit to an empty title", err)
	_, err = a.EditPlan(ctx, call, p.Number, app.PlanChange{Body: &bad})
	wantInvalid(t, "an edit to a bad body", err)
	_, err = a.EditPlan(ctx, call, p.Number, app.PlanChange{Status: &stuck})
	wantInvalid(t, "an edit to an unknown status", err)
	_, err = a.CommentPlan(ctx, call, p.Number, "")
	wantInvalid(t, "an empty comment", err)
	_, err = a.CommentPlan(ctx, call, p.Number, "x\xff")
	wantInvalid(t, "a comment that is not UTF-8", err)
	for _, s := range []core.PlanStatus{core.PlanOpen, "stuck", ""} {
		_, err = a.ClosePlan(ctx, call, p.Number, s, "c")
		wantInvalid(t, "a close with status "+string(s), err)
	}
	_, err = a.ClosePlan(ctx, call, p.Number, core.PlanWon, "x\xff")
	wantInvalid(t, "a close with a bad comment", err)

	got, err := a.Plans(ctx, scope, core.PlanFilter{})
	if err != nil || len(got) != 1 || !reflect.DeepEqual(got[0], p) {
		t.Fatalf("plans after refusals = %+v, %v; want only %+v", got, err, p)
	}
}

func TestAnUnknownPlanIsNotFound(t *testing.T) {
	a := newWorld(t).newApp()
	ctx := ctxFor(t)
	title := "t"
	_, err := a.ViewPlan(ctx, scope, 1)
	wantNotFound(t, "view", err)
	_, err = a.EditPlan(ctx, call, 7, app.PlanChange{Title: &title})
	wantNotFound(t, "edit", err)
	_, err = a.CommentPlan(ctx, call, 0, "c")
	wantNotFound(t, "comment", err)
	_, err = a.ClosePlan(ctx, call, 3, core.PlanWon, "")
	wantNotFound(t, "close", err)
}

// Plans belong to a task: another task of the same repository has its own numbering.
func TestPlansBelongToATask(t *testing.T) {
	a := newWorld(t).newApp()
	ctx := ctxFor(t)
	other := call
	other.Task = "other"
	if _, err := a.CreatePlan(ctx, call, "mine", ""); err != nil {
		t.Fatal(err)
	}
	p, err := a.CreatePlan(ctx, other, "theirs", "")
	if err != nil || p.Number != 1 {
		t.Fatalf("plan of another task = %+v, %v; want its own number 1", p, err)
	}
	mine, _ := a.Plans(ctx, scope, core.PlanFilter{})
	if len(mine) != 1 || mine[0].Title != "mine" {
		t.Fatalf("plans = %+v, want only this task's", mine)
	}
}

func TestAPlanViewListsTheJobsLinkedToIt(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	ctx := ctxFor(t)
	p1, _ := a.CreatePlan(ctx, call, "first", "")
	p2, _ := a.CreatePlan(ctx, call, "second", "")

	link := func(plan core.PlanNumber, script ...string) core.Job {
		out, err := a.Submit(ctx, app.SubmitRequest{Context: call, Spec: command(script...), Plan: plan})
		if err != nil {
			t.Fatalf("Submit: %v", err)
		}
		return out.Job
	}
	j1 := link(p1.Number, "exit", "0")
	j2 := link(p2.Number, "exit", "1")
	j3 := link(p1.Number, "exit", "2")
	unlinked := submit(t, a, "exit", "0")
	for _, j := range []core.Job{j1, j2, j3, unlinked} {
		awaitFinal(t, a, j.ID)
	}

	ids := func(v core.PlanView) []core.JobID {
		var out []core.JobID
		for _, j := range v.Jobs {
			out = append(out, j.ID)
		}
		return out
	}
	v1, err := a.ViewPlan(ctx, scope, p1.Number)
	if err != nil || !reflect.DeepEqual(ids(v1), []core.JobID{j1.ID, j3.ID}) || v1.Plan.Number != p1.Number {
		t.Fatalf("view 1 = %v, %v; want jobs %s and %s", ids(v1), err, j1.ID, j3.ID)
	}
	v2, err := a.ViewPlan(ctx, scope, p2.Number)
	if err != nil || !reflect.DeepEqual(ids(v2), []core.JobID{j2.ID}) {
		t.Fatalf("view 2 = %v, %v", ids(v2), err)
	}
	if v1.Jobs[0].State != core.JobSucceeded || v2.Jobs[0].State != core.JobFailed {
		t.Errorf("a linked job is shown as it stands: %s, %s", v1.Jobs[0].State, v2.Jobs[0].State)
	}

	_, err = a.Submit(ctx, app.SubmitRequest{Context: call, Spec: command("exit", "0"), Plan: 99})
	wantNotFound(t, "a submission linked to a plan that does not exist", err)
}

// Plans survive the daemon: a restart finds them, and numbering goes on.
func TestPlansSurviveARestart(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	ctx := ctxFor(t)
	p, _ := a.CreatePlan(ctx, call, "kept", "body")
	if _, err := a.CommentPlan(ctx, call, p.Number, "noted"); err != nil {
		t.Fatal(err)
	}
	a.Close()
	b := w.newApp()
	v, err := b.ViewPlan(ctx, scope, p.Number)
	if err != nil || v.Plan.Title != "kept" || len(v.Plan.Comments) != 1 {
		t.Fatalf("view after restart = %+v, %v", v, err)
	}
	next, err := b.CreatePlan(ctx, call, "next", "")
	if err != nil || next.Number != 2 {
		t.Fatalf("next plan = %+v, %v; want number 2", next, err)
	}
}
