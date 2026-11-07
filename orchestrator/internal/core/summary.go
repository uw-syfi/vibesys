package core

import "time"

// This file holds the schedule of a run's progress reports. The operator wants a summary of the run
// that is about an hour old at worst, and the only author of one is the lead, so the core asks the
// lead for it: a ReportDue message, answered with the lead's own post_report. There is no other agent
// and no other way to write one.
//
// The schedule is three fields of the run, all folded from events like every other fact:
//
//   - SummaryEvery, how often, fixed when the run starts;
//   - LastSummaryAt, the time the next request is counted from: the start, the lead's last report or
//     the last request, whichever is latest;
//   - NextSummaryAt, when the lead is next asked. It is the zero time while nothing has moved since
//     LastSummaryAt, so a quiet run is never asked, and the timer is armed only when the work moves.
//
// Work moves when a message about work reaches the run (see moved). The first one after LastSummaryAt
// sets NextSummaryAt to LastSummaryAt plus SummaryEvery, or to that moment when it is already past, and
// arms a timer for it. When the timer fires for the time the run still holds, the lead is asked once,
// the request becomes LastSummaryAt and the run is quiet again until the work next moves. A report
// from the lead about the whole run, of either kind, makes its own time LastSummaryAt and clears
// NextSummaryAt, so the lead that reports unasked has the interval counted from its report and the timer
// armed for the old time fires harmlessly. A report that answers a check-in (see checkin.go) is about a
// window of the run and not the run: the lead was asked for it by a viewer, not by this schedule, so it
// moves neither LastSummaryAt nor NextSummaryAt, and the hour goes on being counted from the last report
// of the whole run. Reaching a limit or beginning to stop clears it too, and a lead that has given its
// final report is not asked until it starts new work.
//
// What this guarantees, which the simulation holds the core to: two requests to one lead are at least
// SummaryEvery apart, and each follows movement since the last report or request; none is made once the
// run is not live, has reached a limit or has a final report standing; and a repeated, stale or
// restarted timer sends nothing, because a timer is answered only for the time the run still holds.
// The spacing assumes what the shell promises of every timer, that it is offered once it is due. The
// core does not compare the input's time with the timer's: a timer that was ignored for coming early
// would not be offered again, and the schedule would be lost.

// moved says a message tells of work in its run: an agent was started, or an agent's result, a job's
// end or an agent's stopping reached the lead. What the lead does itself, what the operator says, what
// a turn costs and the reminders the core sends are not work moving. A job starting is not either: its
// end is, and the lead is told of that.
func moved(m Message) bool {
	switch m.Body.(type) {
	case Brief, Handoff, JobFinished, SubagentSettled:
		return true
	}
	return false
}

// asksForReports says the run is one the lead may be asked in: it has a schedule, it is live, it has
// reached no limit (the lead was told to finish, which is the one report that matters) and its lead has
// not finished.
func (r Run) asksForReports() bool {
	return r.SummaryEvery > 0 && r.Status.Live() && r.LimitReached == "" && !r.FinalReported
}

// markMoved records that work moved in a run. The first movement since the lead last reported or was
// asked arms the timer for the next request; later ones change nothing until that is answered.
func (t *tx) markMoved(id RunID) {
	run := t.s.runs[id]
	if !run.asksForReports() || !run.NextSummaryAt.IsZero() {
		return
	}
	run.NextSummaryAt = later(t.in.At, run.LastSummaryAt.Add(run.SummaryEvery))
	t.emit(id, run)
	t.effect(ArmTimer{Kind: TimerSummaryDue, Run: id, FireAt: run.NextSummaryAt})
}

// summaryDue answers the timer armed when work moved. It asks the lead once, for the time the run
// still holds. A timer for any other time is a stale one: the lead reported unasked, the run reached a
// limit or began stopping, or the timer is repeated, and it does nothing.
//
// A request that the lead has not yet read, the schedule's or a viewer's (see requestInTransit), is not
// repeated: it will report when the lead reads it, so the movement counts as asked and no second message
// waits behind the first.
func (t *tx) summaryDue(b TimerFired) {
	run, ok := t.s.runs[b.Timer.Run]
	if !ok || run.NextSummaryAt.IsZero() || !run.NextSummaryAt.Equal(b.Timer.FireAt) {
		return
	}
	run.LastSummaryAt, run.NextSummaryAt = t.in.At, time.Time{}
	t.emit(run.ID, run)
	if t.requestInTransit(run.ID) {
		return
	}
	t.post(Message{Run: run.ID, To: run.Lead, Body: ReportDue{Every: run.SummaryEvery.Truncate(time.Second), Reports: run.ReportsPosted}})
	t.sweep(run.ID)
}

// reported counts the schedule from a report about the whole run that the lead posted at at, and cancels
// the request that was waiting for its timer.
func (r Run) reported(at time.Time) Run {
	r.LastSummaryAt, r.NextSummaryAt = at, time.Time{}
	return r
}

// unscheduled cancels the request waiting for its timer, for a run that can no longer be asked.
func (r Run) unscheduled() Run {
	r.NextSummaryAt = time.Time{}
	return r
}

// later returns the later of two times.
func later(a, b time.Time) time.Time {
	if b.After(a) {
		return b
	}
	return a
}
