package daemon

import (
	"fmt"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
)

// nameSummaryReports gives every run that was recorded before the core named its summary report the name
// of it, and returns the state read again. It runs once when the host opens its store, before anything
// serves a request, so a snapshot never scans a mailbox under the engine's lock.
//
// The report is the newest report of the run that is not about a span of it: one of scope run, or one
// from before reports had a scope that did not answer a check-in. A run whose reports all cover a span
// has none and is read again at every open, which is the cost of not recording that it has none.
// A mailbox that cannot be read leaves its run unnamed until the next open: that run's summary goes
// without a report, and no other run is touched.
func nameSummaryReports(d EngineDeps, state core.State) (core.State, error) {
	var events []core.Event
	for _, run := range state.Runs() {
		if run.SummaryReport != "" || run.ReportsPosted == 0 {
			continue
		}
		id, err := newestWholeRunReport(d.Store, state, run)
		if err != nil {
			d.Log.Error("cannot find the summary report of a run recorded before it was named; its summary goes without", "run", run.ID, "err", err)
			continue
		}
		if id == "" {
			continue
		}
		run.SummaryReport = id
		events = append(events, core.Event{Run: run.ID, At: d.Clock.Now(), Body: run})
	}
	if len(events) == 0 {
		return state, nil
	}
	if _, err := d.Store.Commit(store.Result{Events: events}); err != nil {
		return state, fmt.Errorf("name the summary reports of %d runs: %w", len(events), err)
	}
	return d.Store.Recover()
}

// newestWholeRunReport scans the messages of a run once, for the newest report that is not about a span
// of it. A report from before reports had a scope is about a span when it answered a check-in.
func newestWholeRunReport(s store.Store, state core.State, run core.Run) (core.MessageID, error) {
	answers := map[core.MessageID]bool{}
	for _, c := range state.CheckIns(run.ID) {
		if c.Report != "" {
			answers[c.Report] = true
		}
	}
	var newest core.MessageID
	var seq uint64
	for after := uint64(0); ; {
		page, err := s.Messages(run.ID, after, store.MaxPageMessages)
		if err != nil {
			return "", fmt.Errorf("read the messages of %s: %w", run.ID, err)
		}
		for _, rec := range page {
			report, ok := rec.Message.Body.(core.Report)
			if ok && report.Scope != core.ReportScopeWindow && !(report.Scope == "" && answers[rec.Message.ID]) && rec.Seq >= seq {
				newest, seq = rec.Message.ID, rec.Seq
			}
		}
		if len(page) < store.MaxPageMessages {
			return newest, nil
		}
		after = page[len(page)-1].Seq
	}
}
