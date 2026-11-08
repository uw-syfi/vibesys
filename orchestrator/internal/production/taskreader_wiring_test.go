package production

import (
	"context"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// A host without a job tool has no task reader, and one with a job tool reads tasks through it.
// Neither is a reader that always fails, which would refuse every start.
func TestTheTaskReaderIsWiredOnlyForAHostWithAJobTool(t *testing.T) {
	if got := tasksOf(nil); got != nil {
		t.Fatalf("no job tool: reader %v, want none", got)
	}
	tool := func(context.Context) (core.JobToolEndpoint, error) { return core.JobToolEndpoint{}, nil }
	if _, ok := tasksOf(tool).(CLITasks); !ok {
		t.Fatalf("with a job tool: reader %T, want CLITasks", tasksOf(tool))
	}
}
