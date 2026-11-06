package core_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// An agent that dies in the middle of a turn keeps what it had reported, in its own tally and in the
// run's. Cost reported before a kill, a failure or a lost session is cost.
func TestWhatADyingAgentReportedIsCountedInTheRun(t *testing.T) {
	ends := map[string]func(w *script, a core.AgentID){
		"failed": func(w *script, a core.AgentID) { w.fact(a, core.SessionFailed{Detail: "boom"}) },
		"closed": func(w *script, a core.AgentID) { w.fact(a, core.SessionClosed{}) },
		"lost": func(w *script, a core.AgentID) {
			agent, _ := w.s.Agent(a)
			w.step(core.SessionLost{Session: core.Session{Agent: a, N: agent.Session}, Detail: "gone"})
		},
	}
	for name, end := range ends {
		t.Run(name, func(t *testing.T) {
			w, run, leadID := started(t, 2, steers)
			worker := w.spawn(run, leadID, steers)
			cost, in := 0.25, uint64(1000)
			w.fact(worker, core.UsageReported{Turn: w.turn(worker).ID, Usage: core.Usage{CostUSD: &cost, InputTokens: &in}})
			end(w, worker)
			got, _ := w.s.Agent(worker)
			if got.Usage.CostUSD == nil || *got.Usage.CostUSD != cost {
				t.Fatalf("the %s agent's usage is %+v, want the cost it reported", name, got.Usage)
			}
			if total, _ := w.s.Run(run); total.Usage.CostUSD == nil || *total.Usage.CostUSD != cost {
				t.Fatalf("the run's usage is %+v, want the dying agent's cost in it", total.Usage)
			}
		})
	}
}
