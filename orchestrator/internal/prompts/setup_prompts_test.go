package prompts_test

import (
	"math/rand/v2"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/prompts"
)

// What only an optimizing lead is told: the skill, its tools and its facts. A setup lead is never
// told any of it, whatever the handover's other fields hold.
var optimizeOnly = []string{"vibesys:optimize", "spawn_agent", "post_report", "--fanout", "subagent", "report", "limit", "Time:", "Cost:", "Jobs:"}

func TestASetupLeadIsNeverToldItIsOptimizing(t *testing.T) {
	r := rand.New(rand.NewPCG(51, 52))
	reportKinds := []core.ReportKind{core.ReportProgress, core.ReportFinal}
	statuses := []core.AgentStatus{core.AgentActive, core.AgentIdle, core.AgentEnded}
	for range 1000 {
		h := handover(r, reportKinds, statuses)
		h.Phase = core.PhaseSetup
		// The words of a field are the operator's and the agent's, not the template's: draw none.
		h.Task = ""
		if r.IntN(2) == 0 {
			h.Task = "task-" + strings.Repeat("a", r.IntN(5))
		}
		h.Previous.Model = ""
		got, err := prompts.Render(core.Message{ID: "m1", Body: h})
		if err != nil {
			t.Fatal(err)
		}
		for _, bad := range optimizeOnly {
			if strings.Contains(got, bad) {
				t.Fatalf("a setup handover says %q: %q", bad, got)
			}
		}
		for _, want := range []string{"vibesys:create-task", string(h.Previous.Harness), "setup"} {
			if !strings.Contains(got, want) {
				t.Fatalf("a setup handover lacks %q: %q", want, got)
			}
		}
		if named := strings.Contains(got, "The task is named `"+h.Task+"`"); named != (h.Task != "") {
			t.Fatalf("task %q, rendered %q", h.Task, got)
		}
		if strings.Contains(got, "No task has been named yet") != (h.Task == "") {
			t.Fatalf("task %q, rendered %q", h.Task, got)
		}
	}
}

// A run recorded before phases, or one that is optimizing, is handed over as it always was.
func TestAnOptimizingHandoverNamesTheOptimizeSkillAndNoSetup(t *testing.T) {
	for _, phase := range []core.RunPhase{"", core.PhaseOptimize} {
		got, err := prompts.Render(core.Message{ID: "m1", Body: core.LeadHandover{Task: "kv-store", FanoutLimit: 2, Phase: phase}})
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(got, "`vibesys:optimize` skill for the task `kv-store`") || strings.Contains(got, "create-task") {
			t.Fatalf("phase %q: %q", phase, got)
		}
	}
}

// The setup brief names the tools the lead has in setup and none it lacks, for any repository and note.
func TestTheSetupBriefNamesDeclareTaskAndNoOptimizeTools(t *testing.T) {
	r := rand.New(rand.NewPCG(53, 54))
	for range 500 {
		b := core.SetupBrief{Repository: text(r, "repo:", 4), Steering: optional(r, "steering:")}
		got, err := prompts.Render(core.Message{ID: "m1", Body: b})
		if err != nil {
			t.Fatal(err)
		}
		for _, want := range []string{"vibesys:create-task", "`declare_task`", strings.TrimSpace(b.Repository)} {
			if !strings.Contains(got, want) {
				t.Fatalf("the brief lacks %q: %q", want, got)
			}
		}
		for _, bad := range []string{"vibesys:optimize", "spawn_agent", "post_report"} {
			if strings.Contains(got, bad) {
				t.Fatalf("the setup brief says %q: %q", bad, got)
			}
		}
	}
}
