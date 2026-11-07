package task_test

import (
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// The objectives a task declares are the scored metric and then every other metric its benchmark
// reports, each with the direction the task gives it. A bound is declared for one of them.
func TestDeclaredIsTheScoredMetricThenTheOtherObjectives(t *testing.T) {
	single, err := task.Read(repo(taskFiles("t", jsonManifest, objectives)), "t")
	if err != nil {
		t.Fatal(err)
	}
	if want := []core.Metric{{Name: "ops_per_sec", Direction: core.Maximize}}; !slices.Equal(single.Declared(), want) {
		t.Errorf("a one-metric task declares %+v, want %+v", single.Declared(), want)
	}

	two, err := task.Read(repo(taskFiles("t", protocol2Manifest, objectives2)), "t")
	if err != nil {
		t.Fatal(err)
	}
	want := []core.Metric{{Name: "latency_ms", Direction: core.Minimize}, {Name: "throughput", Direction: core.Maximize}}
	if !slices.Equal(two.Declared(), want) {
		t.Errorf("a two-objective task declares %+v, want %+v", two.Declared(), want)
	}
}
