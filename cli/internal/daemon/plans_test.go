package daemon_test

import (
	"testing"

	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

func wantKind(t *testing.T, what string, err error, kind core.FailureKind) {
	t.Helper()
	if f, ok := wire.FailureOf(err); !ok || f.Kind != kind {
		t.Errorf("%s: failure = %+v ok %v, want %s", what, f, ok, kind)
	}
}

// A plan goes through the service as the contract says: created open, edited field by field,
// commented, closed, and viewed with the jobs linked to it.
func TestAPlanIsManagedOverTheService(t *testing.T) {
	r := newRig(t)
	ctx := ctxFor(t)
	created, err := r.client.CreatePlan(ctx, &cliv1.CreatePlanRequest{Context: call, Title: "shard", Body: "# why"})
	if err != nil {
		t.Fatal(err)
	}
	plan := created.GetPlan()
	if plan.GetNumber() != 1 || plan.GetStatus() != cliv1.PlanStatus_PLAN_STATUS_OPEN || plan.GetBody() != "# why" {
		t.Fatalf("created = %v", plan)
	}

	edited, err := r.client.EditPlan(ctx, &cliv1.EditPlanRequest{Context: call, Number: 1, Title: proto.String("shard by key")})
	if err != nil || edited.GetPlan().GetTitle() != "shard by key" || edited.GetPlan().GetBody() != "# why" {
		t.Fatalf("edit = %v, %v; want the title changed and the body kept", edited, err)
	}
	if _, err := r.client.CommentPlan(ctx, &cliv1.CommentPlanRequest{Context: call, Number: 1, Text: "picked"}); err != nil {
		t.Fatal(err)
	}
	closed, err := r.client.ClosePlan(ctx, &cliv1.ClosePlanRequest{Context: call, Number: 1, Status: cliv1.PlanStatus_PLAN_STATUS_WON, Comment: "2x"})
	if err != nil {
		t.Fatal(err)
	}
	got := closed.GetPlan()
	if got.GetStatus() != cliv1.PlanStatus_PLAN_STATUS_WON || len(got.GetComments()) != 2 || got.GetComments()[1].GetText() != "2x" || got.GetComments()[1].GetAgent() != "agent-1" {
		t.Fatalf("closed = %v", got)
	}

	sub, err := r.client.Submit(ctx, &cliv1.SubmitRequest{Context: call, Plan: 1, Spec: &cliv1.JobSpec{
		Commit: string(commit), Environment: cliv1.EnvironmentKind_ENVIRONMENT_KIND_HOST,
		Settings: &cliv1.JobSpec_Command{Command: &cliv1.CommandSettings{Argv: []string{"exit", "0"}}},
	}})
	if err != nil {
		t.Fatal(err)
	}
	view, err := r.client.ViewPlan(ctx, &cliv1.ViewPlanRequest{Context: call, Number: 1})
	if err != nil {
		t.Fatal(err)
	}
	if jobs := view.GetView().GetJobs(); len(jobs) != 1 || jobs[0].GetJob().GetId() != sub.GetJob().GetId() {
		t.Fatalf("view jobs = %v, want the submitted job", jobs)
	}
	if !proto.Equal(view.GetView().GetPlan(), got) {
		t.Fatalf("view plan = %v, want %v", view.GetView().GetPlan(), got)
	}

	list, err := r.client.ListPlans(ctx, &cliv1.ListPlansRequest{Context: call, Statuses: []cliv1.PlanStatus{cliv1.PlanStatus_PLAN_STATUS_OPEN}})
	if err != nil || len(list.GetPlans()) != 0 {
		t.Fatalf("open plans = %v, %v; want none", list, err)
	}
	list, err = r.client.ListPlans(ctx, &cliv1.ListPlansRequest{Context: call, Text: "shard"})
	if err != nil || len(list.GetPlans()) != 1 {
		t.Fatalf("plans matching shard = %v, %v", list, err)
	}
}

// A status the contract does not name is refused where it enters, with the field named.
func TestPlanCallsRefuseWhatIsNotAStatus(t *testing.T) {
	r := newRig(t)
	ctx := ctxFor(t)
	if _, err := r.client.CreatePlan(ctx, &cliv1.CreatePlanRequest{Context: call, Title: "t"}); err != nil {
		t.Fatal(err)
	}
	for _, bad := range []cliv1.PlanStatus{cliv1.PlanStatus_PLAN_STATUS_UNSPECIFIED, cliv1.PlanStatus(42)} {
		_, err := r.client.EditPlan(ctx, &cliv1.EditPlanRequest{Context: call, Number: 1, Status: &bad})
		wantKind(t, "EditPlan", err, core.FailureInvalidInput)
		_, err = r.client.ClosePlan(ctx, &cliv1.ClosePlanRequest{Context: call, Number: 1, Status: bad})
		wantKind(t, "ClosePlan", err, core.FailureInvalidInput)
		_, err = r.client.ListPlans(ctx, &cliv1.ListPlansRequest{Context: call, Statuses: []cliv1.PlanStatus{bad}})
		wantKind(t, "ListPlans", err, core.FailureInvalidInput)
	}
	_, err := r.client.ClosePlan(ctx, &cliv1.ClosePlanRequest{Context: call, Number: 1, Status: cliv1.PlanStatus_PLAN_STATUS_OPEN})
	wantKind(t, "ClosePlan to open", err, core.FailureInvalidInput)
	_, err = r.client.ViewPlan(ctx, &cliv1.ViewPlanRequest{Context: call, Number: 9})
	wantKind(t, "ViewPlan of an unknown plan", err, core.FailureNotFound)
	_, err = r.client.CreatePlan(ctx, &cliv1.CreatePlanRequest{Context: &cliv1.CallContext{Repository: "/repo"}, Title: "t"})
	wantKind(t, "CreatePlan with no task", err, core.FailureInvalidInput)
}
