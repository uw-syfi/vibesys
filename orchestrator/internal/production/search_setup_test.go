package production_test

import (
	"math/rand/v2"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/production"
)

// A run in setup has no task yet, and follows the name its lead declared for the task it is making:
// the job log of that name is what the setup checks read. One with no name follows nothing, and one
// that left setup follows its task.
func TestARunInSetupFollowsTheTaskItsLeadDeclared(t *testing.T) {
	run := core.Run{ID: "a", Repository: "/repo", Status: core.RunRunning, Phase: core.PhaseSetup}
	if start, stop := production.Plan([]core.Run{run}, nil); len(start) != 0 || len(stop) != 0 {
		t.Fatalf("a run with no declared task followed %v, %v", start, stop)
	}
	run.SetupTask = "draft"
	start, stop := production.Plan([]core.Run{run}, nil)
	if len(start) != 1 || len(stop) != 0 || start[0].Task != "draft" || start[0].Repository != "/repo" {
		t.Fatalf("%v, %v", start, stop)
	}
	// The lead renames the task: the old name is let go and the new one followed.
	run.SetupTask = "renamed"
	next, stop := production.Plan([]core.Run{run}, start)
	if len(next) != 1 || next[0].Task != "renamed" || !slices.Equal(stop, start) {
		t.Fatalf("%v, %v", next, stop)
	}
	// It leaves setup: its task is the name it follows.
	run.Phase, run.Task = core.PhaseOptimize, "renamed"
	if again, stop := production.Plan([]core.Run{run}, next); len(again) != 0 || len(stop) != 0 {
		t.Fatalf("leaving setup changed the stream: %v, %v", again, stop)
	}
}

// Over generated runs: no stream is ever followed for an empty name, a run's stream is its task when it
// has one and its declared name in setup, and a run that is over follows nothing.
func TestEveryFollowedStreamIsNamedAndBelongsToALiveRun(t *testing.T) {
	r := rand.New(rand.NewPCG(7, 11))
	names := []string{"", "x", "y"}
	statuses := []core.RunStatus{core.RunStarting, core.RunRunning, core.RunStopping, core.RunEnded, core.RunFailed}
	for range 500 {
		var runs []core.Run
		want := map[string]bool{}
		for i := range r.IntN(6) {
			run := core.Run{
				ID: core.RunID(string(rune('a' + i))), Repository: "/repo", Status: statuses[r.IntN(len(statuses))],
				Task: names[r.IntN(3)], SetupTask: names[r.IntN(3)],
			}
			if r.IntN(2) == 0 {
				run.Phase = core.PhaseSetup
				run.Task = ""
			}
			runs = append(runs, run)
			name := run.Task
			if name == "" {
				name = run.SetupTask
			}
			if !run.Status.Final() && name != "" {
				want[name] = true
			}
		}
		start, stop := production.Plan(runs, nil)
		got := map[string]bool{}
		for _, k := range start {
			if k.Task == "" {
				t.Fatalf("an empty name was followed: %v for %+v", start, runs)
			}
			got[k.Task] = true
		}
		if len(stop) != 0 || len(got) != len(want) || len(start) != len(want) {
			t.Fatalf("followed %v, want %v, for %+v", got, want, runs)
		}
		for name := range want {
			if !got[name] {
				t.Fatalf("followed %v, want %v, for %+v", got, want, runs)
			}
		}
	}
}
