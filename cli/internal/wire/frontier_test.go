package wire_test

import (
	"testing"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// Every frontier refusal kind has a wire value of its own, and a refusal keeps each reason in
// order with the member and the evidence it names.
func TestFrontierRefusalsCarryTheMemberAndTheEvidence(t *testing.T) {
	kinds := []core.FrontierRefusalKind{
		core.FrontierUnmeasured, core.FrontierCheckMissing, core.FrontierDominated, core.FrontierWithinNoise,
		core.FrontierInconclusive, core.FrontierStale, core.FrontierEnvironmentMismatch,
	}
	var refusal core.FrontierRefusal
	for i, k := range kinds {
		v := anyVerdict(newRand(uint64(i)))
		refusal.Reasons = append(refusal.Reasons, core.FrontierRefusalReason{
			Kind: k, Message: "m" + string(k), Member: commitN(i), Verdict: &v,
			Objectives: []core.ObjectiveVerdict{{Metric: core.Metric{Name: "tok_s", Direction: core.Maximize}, Verdict: v}},
		})
	}
	got := wire.FrontierDecision(refusal).GetRefused()
	seen := map[cliv1.FrontierRefusalKind]bool{}
	if len(got.GetReasons()) != len(kinds) {
		t.Fatalf("%d reasons became %d", len(kinds), len(got.GetReasons()))
	}
	for i, r := range got.GetReasons() {
		want := refusal.Reasons[i]
		if r.GetKind() == cliv1.FrontierRefusalKind_FRONTIER_REFUSAL_KIND_UNSPECIFIED || seen[r.GetKind()] {
			t.Errorf("kind %q became %s, which is unnamed or shared", want.Kind, r.GetKind())
		}
		seen[r.GetKind()] = true
		if r.GetMember() != string(want.Member) || r.GetMessage() != want.Message || r.GetVerdict().GetRatio() != want.Verdict.Ratio ||
			len(r.GetObjectives()) != 1 || r.GetObjectives()[0].GetMetric().GetDirection() != cliv1.Direction_DIRECTION_MAXIMIZE {
			t.Errorf("reason %d = %+v, want %+v", i, r, want)
		}
	}
}

func TestAnAdmissionCarriesTheFrontierAfterIt(t *testing.T) {
	at := time.Date(2026, 11, 2, 0, 0, 0, 0, time.UTC)
	members := []core.FrontierMember{{Commit: commitN(1), AddedAt: at}, {Commit: commitN(2), AddedAt: at.Add(time.Second)}}
	got := wire.FrontierDecision(core.AssumeAdmitted(4, members)).GetAdmitted()
	if got.GetAdded().GetCommit() != string(commitN(2)) || len(got.GetMembers()) != 2 || got.GetAlreadyMember() ||
		!got.GetMembers()[0].GetAddedAt().AsTime().Equal(at) {
		t.Fatalf("%+v", got)
	}
	entries := wire.FrontierEntries([]core.FrontierEntry{{Member: members[0], Values: []core.ObjectiveValue{
		{Metric: core.Metric{Name: "ttft", Direction: core.Minimize}, Value: 12.5, Samples: 5},
	}}})
	if len(entries) != 1 || entries[0].GetValues()[0].GetValue() != 12.5 || entries[0].GetValues()[0].GetSamples() != 5 ||
		entries[0].GetMember().GetCommit() != string(commitN(1)) {
		t.Fatalf("%+v", entries)
	}
}
