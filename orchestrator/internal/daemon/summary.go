package daemon

import (
	"fmt"
	"maps"
	"slices"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// This file works out a run's RunSummary: what a client shows of a run without holding its mailbox or
// its agents. The host serves it so that a client derives none of it, and so that the figures are one
// definition however many screens show them.
//
//   - The report is the one the core named when the lead posted it (Run.SummaryReport), read by id. A
//     run recorded before the core named it is found once in its messages (see legacyReport).
//   - The agents are counted with core.AgentStatus.Working, by type of agent.
//   - Where the run stands on each objective, its head and its best, is search.Standings.

// reportCache keeps the message a run is summed up by, so that a snapshot or a change costs a lookup
// only when the lead has posted a new report. A report never changes once it is recorded, so a message
// kept under its id is the message.
type reportCache struct {
	byRun  map[core.RunID]*pb.Message
	legacy map[core.RunID]legacyReport
}

// legacyReport is what one scan of a run's messages found for a run whose record does not name its
// summary report, and the number of reports the run had when it did: a later report asks again.
type legacyReport struct {
	posted uint32
	report *pb.Message
}

func newReportCache() reportCache {
	return reportCache{byRun: map[core.RunID]*pb.Message{}, legacy: map[core.RunID]legacyReport{}}
}

// runSummary works out the summary of a run as of state. It is called with the engine's lock held, so
// the store, the search views and the state are one instant.
func (e *Engine) runSummary(state core.State, run core.Run, agents []core.Agent) (*pb.RunSummary, error) {
	report, err := e.summaryReport(state, run)
	if err != nil {
		return nil, err
	}
	out := &pb.RunSummary{RunId: string(run.ID), Report: report, Objectives: search.Standings(e.searchState(run.ID))}
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
	return out, nil
}

// summaryReport is the message the run is summed up by, or nil when its lead has posted no report about
// the whole run.
func (e *Engine) summaryReport(state core.State, run core.Run) (*pb.Message, error) {
	if run.SummaryReport == "" {
		return e.legacySummaryReport(state, run)
	}
	if kept := e.reports.byRun[run.ID]; kept != nil && kept.GetId() == string(run.SummaryReport) {
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
	e.reports.byRun[run.ID] = out
	return out, nil
}

// legacySummaryReport finds the summary report of a run that was recorded before the core named it, or
// that has posted none about the whole run: the newest report of the run that did not answer a check-in
// (what a report was before it had a scope). It reads the run's messages once for each number of reports
// the run has had, and a run with no report is never read.
func (e *Engine) legacySummaryReport(state core.State, run core.Run) (*pb.Message, error) {
	if run.ReportsPosted == 0 {
		return nil, nil
	}
	if kept, ok := e.reports.legacy[run.ID]; ok && kept.posted == run.ReportsPosted {
		return kept.report, nil
	}
	answers := map[core.MessageID]bool{}
	for _, c := range state.CheckIns(run.ID) {
		if c.Report != "" {
			answers[c.Report] = true
		}
	}
	var newest *core.Message
	var seq uint64
	for after := uint64(0); ; {
		page, err := e.Store.Messages(run.ID, after, store.MaxPageMessages)
		if err != nil {
			return nil, fmt.Errorf("read the messages of %s: %w", run.ID, err)
		}
		for _, rec := range page {
			report, ok := rec.Message.Body.(core.Report)
			if ok && report.Scope != core.ReportScopeWindow && !(report.Scope == "" && answers[rec.Message.ID]) && rec.Seq >= seq {
				m := rec.Message
				newest, seq = &m, rec.Seq
			}
		}
		if len(page) < store.MaxPageMessages {
			break
		}
		after = page[len(page)-1].Seq
	}
	var out *pb.Message
	if newest != nil {
		out = wire.MailboxMessage(*newest)
	}
	e.reports.legacy[run.ID] = legacyReport{posted: run.ReportsPosted, report: out}
	return out, nil
}
