package core_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Over every state, kind and relation to the head: a job is superseded exactly when it is a measure
// that has not ended and its commit has diverged from the head. A commit that contains the head or
// that the head contains is never flagged, and a job that ended is never flagged, whatever its
// commit.
func TestOnlyAMeasureStillToFinishOfADivergedCommitIsSuperseded(t *testing.T) {
	settings := []core.Settings{core.CheckSettings{}, core.CommandSettings{}, core.MeasureSettings{Samples: 3}}
	relations := []core.HeadRelation{core.ContainsHead, core.InHead, core.DivergedFromHead}
	for _, state := range core.JobStates() {
		for _, s := range settings {
			for _, relation := range relations {
				j := core.Job{ID: "j1", State: state, Spec: core.JobSpec{Settings: s}}
				want := s.Kind() == core.JobMeasure && !state.Final() && relation == core.DivergedFromHead
				if got := core.Superseded(j, relation); got != want {
					t.Errorf("%s %s job of a commit %s: superseded = %v, want %v", state, s.Kind(), relation, got, want)
				}
				if core.Superseded(j, relation) && !core.Supersedable(j) {
					t.Errorf("%s %s job flagged without being supersedable", state, s.Kind())
				}
			}
		}
	}
}

// The relation is read from the two ancestry answers: the head itself (both true) contains the
// head, and only a commit with neither has diverged.
func TestRelationToHeadIsDivergedOnlyWhenNeitherContainsTheOther(t *testing.T) {
	cases := []struct {
		containsHead, inHead bool
		want                 core.HeadRelation
	}{
		{true, true, core.ContainsHead},
		{true, false, core.ContainsHead},
		{false, true, core.InHead},
		{false, false, core.DivergedFromHead},
	}
	for _, c := range cases {
		if got := core.RelationToHead(c.containsHead, c.inHead); got != c.want {
			t.Errorf("RelationToHead(%v, %v) = %s, want %s", c.containsHead, c.inHead, got, c.want)
		}
	}
}
