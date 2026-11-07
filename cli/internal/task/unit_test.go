package task_test

import (
	"fmt"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

const unitObjectives = `
[[objective]]
name = "latency_ms"
direction = "min"
unit = %s

[[objective]]
name = "throughput"
direction = "max"
unit = "tok/s"

[[objective]]
name = "quality"
direction = "max"
`

// An objective's unit is read as written onto the scored metric and onto each other metric, and an
// objective that declares none has none (unknown), never one borrowed from another.
func TestAnObjectiveDeclaresItsUnit(t *testing.T) {
	got, err := task.Read(repo(taskFiles("p", protocol2Manifest, fmt.Sprintf(unitObjectives, `"ms"`))), "p")
	if err != nil {
		t.Fatal(err)
	}
	if got.Metric != (core.Metric{Name: "latency_ms", Direction: core.Minimize, Unit: "ms"}) {
		t.Errorf("metric %+v", got.Metric)
	}
	want := []core.Metric{{Name: "throughput", Direction: core.Maximize, Unit: "tok/s"}, {Name: "quality", Direction: core.Maximize}}
	if !slices.Equal(got.Others, want) {
		t.Errorf("others %+v, want %+v", got.Others, want)
	}
	other, err := task.Read(repo(taskFiles("p", protocol2Manifest, fmt.Sprintf(unitObjectives, `"s"`))), "p")
	if err != nil || other.Version == got.Version {
		t.Errorf("changing a unit must change the task version: %v", err)
	}
}

// A unit that is not one line of text without whitespace at its edges, or is over 64 bytes, is
// refused naming the objective's key.
func TestAnInvalidObjectiveUnitIsRefusedNamingTheKey(t *testing.T) {
	for name, unit := range map[string]string{
		"padded":    `" ms"`,
		"trailing":  `"ms "`,
		"two lines": `"ms\nms"`,
		"too long":  `"` + strings.Repeat("u", 65) + `"`,
		"a number":  `3`,
	} {
		_, err := task.Read(repo(taskFiles("p", protocol2Manifest, fmt.Sprintf(unitObjectives, unit))), "p")
		f := failureOf(t, err)
		if f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "unit") {
			t.Errorf("%s: %v, want invalid input naming unit", name, err)
		}
	}
}
