package core_test

import (
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// These stories pin what a report is about. TestSimulation holds the same rule over random
// interleavings of reports, scheduled requests and catch-ups, against the monitor's own books of which
// report sums each run up.

// reportOf returns the report a recorded message holds.
func (w *script) reportOf(id core.MessageID) core.Report {
	w.t.Helper()
	for _, e := range slices.Backward(w.log) {
		if m, ok := e.Body.(core.Message); ok && m.ID == id {
			report, ok := m.Body.(core.Report)
			if !ok {
				w.t.Fatalf("message %s is %T, not a report", id, m.Body)
			}
			return report
		}
	}
	w.t.Fatalf("no message %s", id)
	return core.Report{}
}

// A report that answers Catch me up covers a span of the run and not the run, so it is not what the run
// is summed up by, and it is not the lead's scheduled report either: the hour goes on being counted from
// the last report about the whole run. It still takes its number among the run's reports.
func TestAReportThatAnswersACatchUpIsAboutItsWindowAndNotTheRun(t *testing.T) {
	w, run, leadID := started(t, 2, steers)
	whole := w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportProgress, Headline: "the run"}})
	if got := w.checkRun(run); got.SummaryReport != whole.Message || w.reportOf(whole.Message).Scope != core.ReportScopeRun {
		t.Fatalf("a report posted on the lead's own does not sum the run up: %+v", got)
	}
	w.spawnWorker(run, leadID)
	before := w.checkRun(run)
	if before.NextSummaryAt.IsZero() {
		t.Fatalf("work moved and no request is due: %+v", before)
	}

	_, c := w.press(ann, run)
	answer := w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportProgress, Headline: "since you looked"}})
	if w.checkIn(c.ID).Report != answer.Message || w.reportOf(answer.Message).Scope != core.ReportScopeWindow {
		t.Fatalf("the answer %q is %+v, check-in %+v", answer.Message, w.reportOf(answer.Message), w.checkIn(c.ID))
	}
	after := w.checkRun(run)
	if after.SummaryReport != whole.Message {
		t.Fatalf("the window report became the run's summary: %q, want %q", after.SummaryReport, whole.Message)
	}
	if !after.LastSummaryAt.Equal(before.LastSummaryAt) || !after.NextSummaryAt.Equal(before.NextSummaryAt) {
		t.Fatalf("a window report moved the schedule: last %v next %v, was last %v next %v",
			after.LastSummaryAt, after.NextSummaryAt, before.LastSummaryAt, before.NextSummaryAt)
	}
	if after.ReportsPosted != 2 || w.reportOf(answer.Message).Sequence != 2 {
		t.Fatalf("the window report is number %d of %d", w.reportOf(answer.Message).Sequence, after.ReportsPosted)
	}

	// A second report in the same turn answers nothing, so it is about the run again.
	second := w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportProgress, Headline: "the run, again"}})
	if got := w.checkRun(run); got.SummaryReport != second.Message || w.reportOf(second.Message).Scope != core.ReportScopeRun || got.LastSummaryAt.Before(after.LastSummaryAt) {
		t.Fatalf("a report that answers nothing does not sum the run up: %+v", got)
	}
}

// The lead's last word is about the run whatever asked for it, so a final report that answers a request
// is the run's summary, stands as the lead's finish, and is still linked to the check-in.
func TestAFinalReportThatAnswersACatchUpIsTheRunsReport(t *testing.T) {
	w, run, leadID := started(t, 2, steers)
	_, c := w.press(ann, run)
	answer := w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportFinal, Headline: "done"}})
	got := w.checkRun(run)
	if got.SummaryReport != answer.Message || !got.FinalReported || w.reportOf(answer.Message).Scope != core.ReportScopeRun || w.checkIn(c.ID).Report != answer.Message {
		t.Fatalf("run %+v, report %+v, check-in %+v", got, w.reportOf(answer.Message), w.checkIn(c.ID))
	}
}

// A report in a command carries no scope of its own that the core keeps: the core decides it from the
// turn the report is posted in, so a lead cannot make a report about a window or about the run by
// saying so.
func TestAReportsScopeIsDecidedByTheCoreAndNotTheCaller(t *testing.T) {
	w, run, leadID := started(t, 2, steers)
	claimed := w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportProgress, Headline: "h", Scope: core.ReportScopeWindow}})
	if got := w.checkRun(run); got.SummaryReport != claimed.Message || w.reportOf(claimed.Message).Scope != core.ReportScopeRun {
		t.Fatalf("a report claiming to be about a window outside a catch-up turn: %+v", w.reportOf(claimed.Message))
	}
}
