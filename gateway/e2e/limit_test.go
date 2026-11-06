//go:build e2e

package e2e

import (
	"strings"
	"testing"
)

// limitedAgents makes the scripted lead wait until its run reaches a limit and then post its final
// report, and every other agent do nothing: the question is what the orchestrator does at a limit.
func (r *rig) limitedAgents() { r.holderEnv = append(r.holderEnv, "VIBESYS_E2E_SCRIPT=limit") }

// A run with a cost limit is told to finish when the cost of its finished turns reaches the limit, the
// lead posts its final report, and the run ends saying it ended by its cost limit. Every wait returns:
// the one for the report with the report, the one for the end with the end.
func TestARunEndsAtItsCostLimitWhenTheLeadFinishes(t *testing.T) {
	r := newRig(t)
	r.useCLI()
	r.limitedAgents()
	repository := newToy(t, r.dir)
	r.serve()

	// A scripted Claude turn costs $0.01, so the third finished turn brings the run to $0.03.
	started := r.receipt("run", "start", "--repository", repository.path, "--task", toyTask, "--detach", "--max-cost", "0.025", "--json")
	r.client("run", "wait", started.RunID, "--until", "stalled", "--timeout", "80s")
	r.client("run", "steer", started.RunID, "go on")
	r.client("run", "wait", started.RunID, "--until", "stalled", "--timeout", "80s")
	r.client("run", "steer", started.RunID, "go on")

	if out := r.client("run", "wait", started.RunID, "--until", "report", "--timeout", "80s"); !strings.Contains(out, "final report") {
		t.Errorf("wait --until report printed %q", out)
	}
	r.client("run", "wait", started.RunID, "--until", "ended", "--timeout", "80s")
	got := r.client("run", "get", started.RunID)
	// Three turns brought the cost to $0.03, over the limit; the fourth, in which the lead finished,
	// cost one more.
	for _, want := range []string{"ended by cost limit", "cost $0.025 (reported so far $0.04)", "the cost limit was reached", "the lead was told to finish"} {
		if !strings.Contains(got, want) {
			t.Errorf("run get does not say %q:\n%s", want, got)
		}
	}
}

// The same for a time limit: the lead is told, finishes, and the run ends by its time limit. Whether
// the limit comes before the lead's first turn or after does not change what the run does, so
// nothing here depends on how long a launch takes.
func TestARunEndsAtItsTimeLimitWhenTheLeadFinishes(t *testing.T) {
	r := newRig(t)
	r.useCLI()
	r.limitedAgents()
	repository := newToy(t, r.dir)
	r.serve()

	started := r.receipt("run", "start", "--repository", repository.path, "--task", toyTask, "--detach", "--max-time", "1s", "--json")
	if out := r.client("run", "wait", started.RunID, "--until", "report", "--timeout", "80s"); !strings.Contains(out, "final report") {
		t.Errorf("wait --until report printed %q", out)
	}
	r.client("run", "wait", started.RunID, "--until", "ended", "--timeout", "80s")
	got := r.client("run", "get", started.RunID)
	for _, want := range []string{"ended by time limit", "the time limit was reached"} {
		if !strings.Contains(got, want) {
			t.Errorf("run get does not say %q:\n%s", want, got)
		}
	}
}
