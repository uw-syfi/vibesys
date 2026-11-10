package core_test

import (
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func TestFinalStatesAreExactlyTheFourEndings(t *testing.T) {
	final := []core.JobState{core.JobSucceeded, core.JobFailed, core.JobCancelled, core.JobLost}
	for _, state := range core.JobStates() {
		if got, want := state.Final(), slices.Contains(final, state); got != want {
			t.Errorf("%s.Final() = %v, want %v", state, got, want)
		}
	}
	if got := len(core.JobStates()); got != 6 {
		t.Errorf("JobStates() has %d states, want the six the contract names", got)
	}
}

func TestSettingsKnowTheirKind(t *testing.T) {
	cases := map[core.JobKind]core.Settings{
		core.JobCheck:   core.CheckSettings{},
		core.JobCommand: core.CommandSettings{},
		core.JobMeasure: core.MeasureSettings{},
		core.JobTrace:   core.TraceSettings{},
	}
	for kind, settings := range cases {
		if got := settings.Kind(); got != kind {
			t.Errorf("%T.Kind() = %s, want %s", settings, got, kind)
		}
	}
}

// A trace is run again after its supervisor is proved gone, as a check and a measure are, since it
// saves nothing until it passes; and it holds its placement alone, as a measure does, since it
// deploys and loads the system. A check and a command share their placement.
func TestATraceIsRerunAndHoldsItsPlacementAlone(t *testing.T) {
	cases := []struct {
		kind  core.JobKind
		rerun core.RerunPolicy
		alone bool
	}{
		{core.JobCheck, core.RerunAfterProvenDeath, false},
		{core.JobCommand, core.RerunBeforeStartOnly, false},
		{core.JobMeasure, core.RerunAfterProvenDeath, true},
		{core.JobTrace, core.RerunAfterProvenDeath, true},
	}
	for _, c := range cases {
		if got := core.RerunPolicyOf(c.kind); got != c.rerun {
			t.Errorf("RerunPolicyOf(%s) = %s, want %s", c.kind, got, c.rerun)
		}
		if got := core.HoldsPlacementAlone(c.kind); got != c.alone {
			t.Errorf("HoldsPlacementAlone(%s) = %v, want %v", c.kind, got, c.alone)
		}
	}
	for _, s := range []core.JobStage{core.StageBuild, core.StageCheck, core.StageMeasure, core.StageTrace} {
		if !s.Valid() {
			t.Errorf("stage %q is not valid", s)
		}
	}
	if core.JobStage("").Valid() || core.JobStage("benchmark").Valid() {
		t.Error("a stage that is not one of the stages is valid")
	}
}
