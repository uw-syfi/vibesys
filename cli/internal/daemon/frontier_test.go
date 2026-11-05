package daemon_test

import (
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

var latencyTask = core.Objectives{
	Metrics:       []core.Metric{{Name: "tok_s", Direction: core.Maximize}, {Name: "ttft", Direction: core.Minimize}},
	RelativeNoise: 0.01,
}

// measuredOn saves a measurement of a multi-objective commit: five samples of each objective.
func (r *resultsRig) measuredOn(c core.Commit, tok, ttft float64) {
	r.t.Helper()
	st, err := r.stores.Create(ctxFor(r.t), mustScope(r.t, "/repo", "task"))
	if err != nil {
		r.t.Fatal(err)
	}
	five := func(v float64) []float64 { return []float64{v, v, v, v, v} }
	storetest.SeedMeasurement(r.t, st, storetest.Measurement{
		Commit: c, Samples: five(tok), At: r.clk.Now(), Objectives: latencyTask, TaskVersion: r.taskVersion,
		Others: []core.Observation{{Name: "ttft", Values: five(ttft)}},
	})
}

// Over the wire a multi-objective comparison carries the verdict on each objective, a tradeoff is a
// refusal response, and frontier add and list answer what the frontier holds.
func TestAParetoTaskIsComparedPromotedAndKeptOverTheWire(t *testing.T) {
	r := newResultsRig(t)
	r.measuredOn(cm(0), 100, 100)
	r.measuredOn(cm(1), 200, 200)
	r.measuredOn(cm(2), 300, 100)

	cmp, err := r.client.Compare(ctxFor(t), &cliv1.CompareRequest{Context: call, Commit: string(cm(1))})
	if err != nil || cmp.GetComparison().GetVerdict().GetKind() != cliv1.VerdictKind_VERDICT_KIND_TRADEOFF ||
		len(cmp.GetComparison().GetObjectives()) != 2 ||
		cmp.GetComparison().GetObjectives()[1].GetVerdict().GetKind() != cliv1.VerdictKind_VERDICT_KIND_WORSE ||
		cmp.GetComparison().GetObjectives()[1].GetMetric().GetName() != "ttft" {
		t.Fatalf("Compare = %+v, %v; want a tradeoff with a verdict on each objective", cmp, err)
	}

	refused, err := r.client.Promote(ctxFor(t), &cliv1.PromoteRequest{Context: call, Commit: string(cm(1))})
	if err != nil || refused.GetRefused().GetReasons()[0].GetKind() != cliv1.PromoteRefusalKind_PROMOTE_REFUSAL_KIND_TRADEOFF ||
		len(refused.GetRefused().GetReasons()[0].GetObjectives()) != 2 || refused.GetFrontier() != nil {
		t.Fatalf("Promote of a tradeoff = %+v, %v", refused, err)
	}
	added, err := r.client.AddToFrontier(ctxFor(t), &cliv1.AddToFrontierRequest{Context: call, Commit: string(cm(1))})
	if err != nil || added.GetDecision().GetAdmitted().GetAdded().GetCommit() != string(cm(1)) {
		t.Fatalf("AddToFrontier = %+v, %v", added, err)
	}

	promoted, err := r.client.Promote(ctxFor(t), &cliv1.PromoteRequest{Context: call, Commit: string(cm(2))})
	if err != nil || promoted.GetPromoted().GetCommit() != string(cm(2)) || len(promoted.GetPromoted().GetObjectives()) != 2 {
		t.Fatalf("Promote of a win = %+v, %v", promoted, err)
	}
	if a := promoted.GetFrontier().GetAdmitted(); a == nil || len(a.GetEvicted()) != 1 || a.GetEvicted()[0] != string(cm(1)) {
		t.Fatalf("the frontier made %+v of the promoted commit, want it admitted evicting cm(1)", promoted.GetFrontier())
	}

	listed, err := r.client.ListFrontier(ctxFor(t), &cliv1.ListFrontierRequest{Context: call})
	if err != nil || len(listed.GetMembers()) != 1 || listed.GetMembers()[0].GetMember().GetCommit() != string(cm(2)) {
		t.Fatalf("ListFrontier = %+v, %v", listed, err)
	}
	values := listed.GetMembers()[0].GetValues()
	if len(values) != 2 || values[0].GetValue() != 300 || values[1].GetValue() != 100 || values[1].GetSamples() != 5 {
		t.Fatalf("the member's numbers are %+v", values)
	}

	again, err := r.client.AddToFrontier(ctxFor(t), &cliv1.AddToFrontierRequest{Context: call, Commit: string(cm(0))})
	reasons := again.GetDecision().GetRefused().GetReasons()
	if err != nil || len(reasons) != 1 || reasons[0].GetKind() != cliv1.FrontierRefusalKind_FRONTIER_REFUSAL_KIND_DOMINATED ||
		reasons[0].GetMember() != string(cm(2)) {
		t.Fatalf("AddToFrontier of a beaten commit = %+v, %v; want dominated by cm(2)", again, err)
	}
}

// A commit measured on one metric cannot join a frontier, and the refusal is a typed failure.
func TestASingleMetricCommitIsInvalidForTheFrontier(t *testing.T) {
	r := newResultsRig(t)
	r.measured(cm(1), winning...)
	_, err := r.client.AddToFrontier(ctxFor(t), &cliv1.AddToFrontierRequest{Context: call, Commit: string(cm(1))})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("AddToFrontier of a single-metric commit = %v, want invalid argument", err)
	}
}
