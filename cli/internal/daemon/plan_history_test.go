package daemon_test

import (
	"testing"
	"time"

	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// A plan records who made it, and who set each status it took, from the run and agent the call
// carried and the daemon's clock: a request has no field for either, so a caller states no other's.
// Over calls from many runs and agents, the origin is always the call's, the creation is the first
// transition, and a close from another call is that call's transition.
func TestAPlanRecordsItsOriginAndEachTransitionFromTheCall(t *testing.T) {
	r := newRig(t)
	ctx := ctxFor(t)
	r.taskWithAJob()
	as := func(run, agent string) *cliv1.CallContext {
		c := proto.Clone(call).(*cliv1.CallContext)
		c.RunId, c.Agent = run, agent
		return c
	}
	runs := []string{"", "run-1", "run-2", "r.3"}
	agents := []string{"", "lead", "planner-7"}
	var last time.Time
	for i, run := range runs {
		for j, agent := range agents {
			by := as(run, agent)
			created, err := r.client.CreatePlan(ctx, &cliv1.CreatePlanRequest{Context: by, Title: "plan"})
			if err != nil {
				t.Fatal(err)
			}
			plan := created.GetPlan()
			origin := plan.GetOrigin()
			if origin.GetRunId() != run || origin.GetAgent() != agent || !origin.GetAt().IsValid() {
				t.Fatalf("origin = %v, want run %q and agent %q from the call", origin, run, agent)
			}
			if at := origin.GetAt().AsTime(); !at.After(last) || !at.Equal(plan.GetCreatedAt().AsTime()) {
				t.Fatalf("origin time %v, created at %v, want the daemon's clock, moving on from %v", at, plan.GetCreatedAt().AsTime(), last)
			}
			if got := plan.GetTransitions(); len(got) != 1 || got[0].GetStatus() != cliv1.PlanStatus_PLAN_STATUS_OPEN || !proto.Equal(got[0].GetOrigin(), origin) {
				t.Fatalf("transitions = %v, want the creation as open, by the origin", got)
			}

			closer := as(runs[(i+1)%len(runs)], agents[(j+1)%len(agents)])
			closed, err := r.client.ClosePlan(ctx, &cliv1.ClosePlanRequest{Context: closer, Number: plan.GetNumber(), Status: cliv1.PlanStatus_PLAN_STATUS_WON})
			if err != nil {
				t.Fatal(err)
			}
			got := closed.GetPlan()
			if !proto.Equal(got.GetOrigin(), origin) {
				t.Fatalf("origin after a close = %v, want it unchanged: %v", got.GetOrigin(), origin)
			}
			ts := got.GetTransitions()
			if len(ts) != 2 || ts[1].GetStatus() != cliv1.PlanStatus_PLAN_STATUS_WON ||
				ts[1].GetOrigin().GetRunId() != closer.GetRunId() || ts[1].GetOrigin().GetAgent() != closer.GetAgent() {
				t.Fatalf("transitions = %v, want the creation, then won by the closing call (%q, %q)", ts, closer.GetRunId(), closer.GetAgent())
			}

			again, err := r.client.ClosePlan(ctx, &cliv1.ClosePlanRequest{Context: closer, Number: plan.GetNumber(), Status: cliv1.PlanStatus_PLAN_STATUS_WON, Comment: "still won"})
			if err != nil || len(again.GetPlan().GetTransitions()) != 2 {
				t.Fatalf("a close to the status the plan has = %v, %v; want no new transition", again, err)
			}
			last = again.GetPlan().GetUpdatedAt().AsTime()
		}
	}
}
