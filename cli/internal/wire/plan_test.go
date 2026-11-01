package wire_test

import (
	"reflect"
	"testing"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

func anyPlan(number int) core.Plan {
	rnd := newRand(uint64(number))
	p := core.Plan{
		Number:    core.PlanNumber(1 + number),
		Title:     pick(rnd, "cache the index", "ü title", "a b"),
		Body:      pick(rnd, "", "# Hypothesis\n\nbody", "ü"),
		Status:    pick(rnd, core.PlanStatuses()...),
		CreatedAt: epoch.Add(time.Duration(number) * time.Second),
		UpdatedAt: epoch.Add(time.Duration(number+1) * time.Second),
	}
	for i := range rnd.IntN(3) {
		p.Comments = append(p.Comments, core.PlanComment{Text: "c", At: epoch.Add(time.Duration(i) * time.Minute), Agent: pick(rnd, "", "lead")})
	}
	return p
}

func TestAPlanSurvivesTheContractAndBack(t *testing.T) {
	for i := range 500 {
		p := anyPlan(i)
		got, err := wire.ToPlan(wire.Plan(p))
		if err != nil {
			t.Fatalf("plan %+v: %v", p, err)
		}
		if !reflect.DeepEqual(got, p) {
			t.Fatalf("plan came back changed:\n got %+v\nwant %+v", got, p)
		}
	}
}

// Every status of the contract is one the domain names or is refused with the field named, and
// every status of the domain has a contract value.
func TestEveryPlanStatusIsTranslatedOrRefused(t *testing.T) {
	for n, name := range cliv1.PlanStatus_name {
		status, err := wire.ToPlanStatus("status", cliv1.PlanStatus(n))
		if name == "PLAN_STATUS_UNSPECIFIED" {
			if err == nil {
				t.Fatalf("%s was accepted as %q", name, status)
			}
			continue
		}
		if err != nil || wire.PlanStatus(status) != cliv1.PlanStatus(n) || !status.Valid() {
			t.Fatalf("%s: got %q, %v", name, status, err)
		}
	}
	if _, err := wire.ToPlanStatus("status", cliv1.PlanStatus(99)); err == nil {
		t.Fatal("a status from a newer build was accepted")
	}
	for _, s := range core.PlanStatuses() {
		if wire.PlanStatus(s) == cliv1.PlanStatus_PLAN_STATUS_UNSPECIFIED {
			t.Fatalf("status %q has no contract value", s)
		}
	}
}

func TestAPlanFilterRefusesAnUnknownStatusAndNamesTheField(t *testing.T) {
	_, err := wire.ToPlanFilter(&cliv1.ListPlansRequest{Statuses: []cliv1.PlanStatus{cliv1.PlanStatus_PLAN_STATUS_WON, cliv1.PlanStatus_PLAN_STATUS_UNSPECIFIED}})
	f, ok := wire.FailureOf(err)
	if !ok || f.Kind != core.FailureInvalidInput {
		t.Fatalf("err = %v, want invalid input", err)
	}
	got, err := wire.ToPlanFilter(&cliv1.ListPlansRequest{Statuses: []cliv1.PlanStatus{cliv1.PlanStatus_PLAN_STATUS_WON}, Text: "x"})
	if err != nil || !reflect.DeepEqual(got, core.PlanFilter{Statuses: []core.PlanStatus{core.PlanWon}, Text: "x"}) {
		t.Fatalf("filter = %+v, %v", got, err)
	}
}
