package core_test

import (
	"time"

	"google.golang.org/protobuf/types/known/durationpb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// The operator's updates, among everything else the simulation does: limits set, changed, cleared and
// passed, a fan-out lowered under the agents that are live, and a lead asked for on another spec, while
// turns end, sessions fall, the daemon crashes and mail arrives. What each must leave is held by the
// monitor (monitor_update_test.go) and by State.Violations.

// update has the operator change a live run: any of its limits, its fan-out and its lead.
func (w *sim) update() {
	run, ok := pick(w, w.liveRuns())
	if !ok {
		return
	}
	u := &pb.UpdateRun{RunId: string(run.ID)}
	for u.CostLimit == nil && u.TimeLimit == nil && u.FanoutLimit == nil && u.Lead == nil {
		if w.r.IntN(3) == 0 {
			u.CostLimit = w.drawCostUpdate()
		}
		if w.r.IntN(3) == 0 {
			u.TimeLimit = w.drawTimeUpdate(run)
		}
		if w.r.IntN(3) == 0 {
			fanout := uint32(1 + w.r.IntN(4))
			u.FanoutLimit = &fanout
		}
		if w.r.IntN(2) == 0 {
			u.Lead = w.drawLead(run)
		}
	}
	w.command(core.Operator{}, &pb.Command{Body: &pb.Command_UpdateRun{UpdateRun: u}})
}

func (w *sim) drawCostUpdate() *pb.CostLimitUpdate {
	if w.r.IntN(4) == 0 {
		return &pb.CostLimitUpdate{Change: &pb.CostLimitUpdate_Remove{Remove: true}}
	}
	return &pb.CostLimitUpdate{Change: &pb.CostLimitUpdate_SetUsd{SetUsd: []float64{0.25, 1, 3, 10}[w.r.IntN(4)]}}
}

// drawTimeUpdate sets a total ahead of the run's age, or behind it, which passes the limit at once.
func (w *sim) drawTimeUpdate(run core.Run) *pb.TimeLimitUpdate {
	if w.r.IntN(4) == 0 {
		return &pb.TimeLimitUpdate{Change: &pb.TimeLimitUpdate_Remove{Remove: true}}
	}
	age := w.now.Sub(run.CreatedAt)
	total := age + time.Duration(60+w.r.IntN(3000))*time.Second
	if w.r.IntN(4) == 0 {
		total = max(time.Second, age/2)
	}
	return &pb.TimeLimitUpdate{Change: &pb.TimeLimitUpdate_Set{Set: durationpb.New(total)}}
}

var harnessesByName = map[core.Harness]pb.Harness{core.ClaudeCode: pb.Harness_HARNESS_CLAUDE_CODE, core.Codex: pb.Harness_HARNESS_CODEX}

// drawLead asks for the lead it has a quarter of the time, which withdraws a change that waits or
// changes nothing, and otherwise for a spec drawn from a few.
func (w *sim) drawLead(run core.Run) *pb.AgentSpec {
	if current, ok := w.state.Agent(run.Lead); ok && w.r.IntN(4) == 0 {
		return &pb.AgentSpec{Harness: harnessesByName[current.Spec.Harness], Model: string(current.Spec.Model), ReasoningEffort: string(current.Spec.Effort)}
	}
	return &pb.AgentSpec{
		Harness: pb.Harness(1 + w.r.IntN(2)), Model: []string{"", "m", "x", "y"}[w.r.IntN(4)],
		ReasoningEffort: []string{"", "low", "high"}[w.r.IntN(3)],
	}
}
