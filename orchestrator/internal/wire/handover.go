package wire

import (
	"time"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// This file maps the message that opens a replacement lead: see core.LeadHandover.

func leadHandoverOut(h core.LeadHandover) *pb.LeadHandover {
	out := &pb.LeadHandover{
		Task: h.Task, FanoutLimit: h.FanoutLimit,
		PreviousHarness: harnesses.out(h.Previous.Harness), PreviousModel: string(h.Previous.Model), PreviousReasoningEffort: string(h.Previous.Effort),
		ElapsedSeconds: uint32(h.Elapsed / time.Second), SpentUsd: h.Spent, MaxCostUsd: limitOut(h.MaxCost),
		LimitReached: runLimits.out(h.LimitReached), ReportsPosted: h.ReportsPosted,
		AgentsEnded: h.AgentsEnded, ActiveJobs: h.ActiveJobs,
	}
	if h.MaxTime > 0 {
		seconds := uint32(h.MaxTime / time.Second)
		out.MaxTimeSeconds = &seconds
	}
	if h.LatestReport != nil {
		out.LatestReport = reportOut(*h.LatestReport)
	}
	for _, a := range h.Agents {
		out.Agents = append(out.Agents, &pb.HandoverAgent{
			AgentId: string(a.ID), Role: roles.out(a.Role), Status: agentStatuses.out(a.Status), PlanNumber: a.PlanNumber,
		})
	}
	return out
}

func leadHandoverIn(h *pb.LeadHandover, errs *errorList) core.LeadHandover {
	out := core.LeadHandover{
		Task: h.GetTask(), FanoutLimit: h.GetFanoutLimit(),
		Previous: core.AgentSpec{
			Harness: harnesses.in(h.GetPreviousHarness(), errs), Model: core.ModelName(h.GetPreviousModel()), Effort: core.EffortName(h.GetPreviousReasoningEffort()),
		},
		Elapsed: time.Duration(h.GetElapsedSeconds()) * time.Second, Spent: h.SpentUsd, MaxCost: h.GetMaxCostUsd(),
		MaxTime:      time.Duration(h.GetMaxTimeSeconds()) * time.Second,
		LimitReached: runLimits.in(h.GetLimitReached(), errs), ReportsPosted: h.GetReportsPosted(),
		AgentsEnded: h.GetAgentsEnded(), ActiveJobs: h.GetActiveJobs(),
	}
	if h.LatestReport != nil {
		report := reportIn(h.LatestReport, errs)
		out.LatestReport = &report
	}
	for _, a := range h.GetAgents() {
		out.Agents = append(out.Agents, core.HandoverAgent{
			ID: core.AgentID(a.GetAgentId()), Role: roles.in(a.GetRole(), errs), Status: agentStatuses.in(a.GetStatus(), errs), PlanNumber: a.GetPlanNumber(),
		})
	}
	return out
}
