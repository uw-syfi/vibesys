package flow_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
)

// The stage of a job's record is named for the steps that say something about the candidate or the
// check, and for no other: a checkout or a server that did not start says nothing of whether the
// candidate built or the check judged it.
func TestOnlyTheBuildCheckAndBenchmarkStepsNameAStageOfTheJob(t *testing.T) {
	want := map[flow.Stage]core.JobStage{
		flow.StageBuild:       core.StageBuild,
		flow.StageCandidate:   core.StageBuild,
		flow.StageCheck:       core.StageCheck,
		flow.StageBenchmark:   core.StageMeasure,
		flow.StageTrace:       core.StageTrace,
		flow.StageCheckout:    "",
		flow.StageObserve:     "",
		flow.StageService:     "",
		flow.StageReset:       "",
		"a stage added later": "",
	}
	for stage, stageOfJob := range want {
		if got := stage.JobStage(); got != stageOfJob {
			t.Errorf("%q names job stage %q, want %q", stage, got, stageOfJob)
		}
	}
}
