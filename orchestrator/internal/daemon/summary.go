package daemon

import (
	"fmt"
	"maps"
	"slices"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// This file works out a run's RunSummary: what a client shows of a run without holding its mailbox or
// its agents. The host serves it so that a client derives none of it, and so that the figures are one
// definition however many screens show them.
//
//   - The report is the one the core named when the lead posted it (Run.SummaryReport), read by id. A
//     run recorded before the core named it is given the name once, when the host opens its store (see
//     nameSummaryReports), so no request ever scans a mailbox.
//   - The agents are counted with core.AgentStatus.Working, by type of agent.
//   - Where the run stands on each objective, its head and its best, is search.Standings.
//
// A summary is always worked out. A run whose report cannot be served (the record is missing, the read
// failed, or the report is larger than MaxSummaryBytes) is summed up without it and says so in the data
// (RunSummary.report_unavailable), so one bad run never costs a stream its other runs.

// MaxSummaryBytes bounds one run's summary, so that a run's frames are small next to the budgets that
// list runs, and a report too large to send degrades its own run and nothing else.
const MaxSummaryBytes = MaxSnapshotBytes / 4

// reportCache keeps the message a run is summed up by, so that a snapshot or a change costs a lookup
// only when the lead has posted a new report. A report never changes once it is recorded, so a message
// kept under its id is the message.
type reportCache map[core.RunID]*pb.Message

// runSummary works out the summary of a run. It is called with the engine's lock held, so the store, the
// search views and the state are one instant. It never fails: see the top of this file.
func (e *Engine) runSummary(run core.Run, agents []core.Agent) *pb.RunSummary {
	out := &pb.RunSummary{RunId: string(run.ID), Objectives: search.Standings(e.searchState(run.ID))}
	report, err := e.summaryReport(run)
	if err != nil {
		e.Log.Error("cannot serve the summary report of a run; its summary goes without", "run", run.ID, "err", err)
		out.ReportUnavailable = true
	}
	out.Report = report
	out.AgentsTotal = uint32(len(agents))
	roles := map[pb.AgentRole]*pb.RoleAgents{}
	for _, a := range agents {
		msg := wire.AgentMessage(a)
		entry := roles[msg.GetRole()]
		if entry == nil {
			entry = &pb.RoleAgents{Role: msg.GetRole()}
			roles[msg.GetRole()] = entry
		}
		entry.Total++
		if a.Status.Working() {
			entry.Working++
			out.AgentsWorking++
		}
	}
	for _, role := range slices.Sorted(maps.Keys(roles)) {
		out.Roles = append(out.Roles, roles[role])
	}
	if size := proto.Size(out); size > MaxSummaryBytes && out.Report != nil {
		e.Log.Error("the summary report of a run is too large to send; its summary goes without", "run", run.ID, "bytes", size, "limit", MaxSummaryBytes)
		out.Report, out.ReportUnavailable = nil, true
	}
	return out
}

// summaryReport is the message the run is summed up by, or nil when its lead has posted no report about
// the whole run.
func (e *Engine) summaryReport(run core.Run) (*pb.Message, error) {
	if run.SummaryReport == "" {
		return nil, nil
	}
	if kept := e.reports[run.ID]; kept != nil && kept.GetId() == string(run.SummaryReport) {
		return kept, nil
	}
	msg, ok, err := e.Store.Message(run.SummaryReport)
	switch {
	case err != nil:
		return nil, fmt.Errorf("read the summary report %s of %s: %w", run.SummaryReport, run.ID, err)
	case !ok:
		return nil, fmt.Errorf("run %s names %s as its summary report, which the store does not hold", run.ID, run.SummaryReport)
	}
	out := wire.MailboxMessage(msg)
	e.reports[run.ID] = out
	return out, nil
}
