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
	}
	for kind, settings := range cases {
		if got := settings.Kind(); got != kind {
			t.Errorf("%T.Kind() = %s, want %s", settings, got, kind)
		}
	}
}
