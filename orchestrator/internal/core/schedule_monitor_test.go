package core_test

import (
	"fmt"
	"slices"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// schedule is the monitor's own account of when one run's lead is next asked for a progress report. It
// is kept from the committed events and commands alone, from the rules of the contract and not from the
// core's code, and every step it is compared with the fields the core folds into the run.
type schedule struct {
	every time.Duration
	// base is the start of the run, the lead's last report or the last request, whichever is latest.
	base time.Time
	// due is when the lead is next asked, the zero time while nothing has moved since base.
	due time.Time
}

// scheduleCoverage counts what the simulations reached of the report schedule, so that its properties
// cannot hold by never being tested.
type scheduleCoverage struct {
	// asks are requests sent to a lead, swallowed ones a request that was due while the last was still
	// unread, stale the timers that fired for a time the run no longer held, restarted the reports a
	// lead posted while a request was waiting for its timer, and cancelled the requests that a limit, a
	// stop or a final report took away.
	asks, swallowed, stale, restarted, cancelled int
}

func (c *scheduleCoverage) add(o scheduleCoverage) {
	c.asks += o.asks
	c.swallowed += o.swallowed
	c.stale += o.stale
	c.restarted += o.restarted
	c.cancelled += o.cancelled
}

// movesWork says a message tells of work in its run: an agent was started (its brief), or a handoff,
// a finished job or a settled agent reached the lead.
func (m *monitor) movesWork(msg core.Message) bool {
	run := m.runs[msg.Run]
	switch msg.Body.(type) {
	case core.Brief:
		return msg.To != run.Lead
	case core.Handoff, core.JobFinished, core.SubagentSettled:
		return msg.To == run.Lead
	}
	return false
}

// askable says the lead of a run may be asked now: the run is live, has reached no limit and its lead
// has not finished.
func (m *monitor) askable(id core.RunID) bool {
	run := m.runs[id]
	return run.Status.Live() && run.LimitReached == "" && !m.finished[id]
}

// workMoved records that a message moved work in a run the lead may be asked in: the first movement
// since the base makes a request due, at the interval after the base or at once if that has passed.
func (m *monitor) workMoved(msg core.Message) {
	s := m.books[msg.Run]
	if s == nil || !m.movesWork(msg) || !m.askable(msg.Run) || !s.due.IsZero() {
		return
	}
	s.due = s.base.Add(s.every)
	if msg.At.After(s.due) {
		s.due = msg.At
	}
	m.arms[msg.Run] = append(m.arms[msg.Run], s.due)
}

// reported counts the schedule from a report the lead posted.
func (m *monitor) reported(msg core.Message) {
	s := m.books[msg.Run]
	if s == nil {
		return
	}
	if !s.due.IsZero() {
		m.covered.schedule.restarted++
	}
	s.base, s.due = msg.At, time.Time{}
}

// newSchedule opens the books of a run from its first event, and holds the interval the core recorded
// to what the command asked for: the default when it asked for none.
func (m *monitor) newSchedule(r core.Run) error {
	want := m.startEvery
	if want == 0 {
		want = core.DefaultSummaryEvery
	}
	if r.SummaryEvery != want {
		return fmt.Errorf("run %s records an interval of %v between reports, but its start asked for %v", r.ID, r.SummaryEvery, want)
	}
	m.books[r.ID] = &schedule{every: want, base: r.CreatedAt}
	return nil
}

// runChanged takes a request away from a run that can no longer be asked.
func (m *monitor) runChanged(r core.Run) {
	if s := m.books[r.ID]; s != nil && !s.due.IsZero() && !m.askable(r.ID) {
		s.due = time.Time{}
		m.covered.schedule.cancelled++
	}
}

// reportDueInTransit says a request for a report is still on its way to the run's lead.
func (m *monitor) reportDueInTransit(id core.RunID) bool {
	for _, record := range m.messages {
		if _, ok := record.body.(core.ReportDue); ok && record.run == id && !record.last.Final() {
			return true
		}
	}
	return false
}

// summaryTimer holds a fired summary timer to the books: a timer for the time the schedule holds asks
// the lead once, never sooner than the interval after the base, and any other timer does nothing.
func (m *monitor) summaryTimer(body core.InputBody, events []core.Event) error {
	fired, ok := body.(core.TimerFired)
	if !ok || fired.Timer.Kind != core.TimerSummaryDue {
		return nil
	}
	s := m.books[fired.Timer.Run]
	current := s != nil && !s.due.IsZero() && s.due.Equal(fired.Timer.FireAt)
	if asked := len(events) > 0; asked != current {
		return fmt.Errorf("the summary timer of run %s for %v fired against a schedule due at %v, and the core asked for a report: %t",
			fired.Timer.Run, fired.Timer.FireAt, due(s), asked)
	}
	if !current {
		m.covered.schedule.stale++
		return nil
	}
	at := events[0].At
	if at.Sub(s.base) < s.every {
		return fmt.Errorf("run %s asked for a report at %v, %v after the last, which is less than %v", fired.Timer.Run, at, at.Sub(s.base), s.every)
	}
	// A message appears in an event for each step of its delivery, so count the messages and not the events.
	requests := map[core.MessageID]bool{}
	for _, e := range events {
		if msg, ok := e.Body.(core.Message); ok {
			if _, ok := msg.Body.(core.ReportDue); ok {
				requests[msg.ID] = true
			}
		}
	}
	sent := len(requests)
	switch unread := m.reportDueInTransit(fired.Timer.Run); {
	case unread && sent != 0:
		return fmt.Errorf("run %s sent a second request for a report while the first was unread", fired.Timer.Run)
	case unread:
		m.covered.schedule.swallowed++
	case sent != 1:
		return fmt.Errorf("run %s sent %d requests for a report when one was due", fired.Timer.Run, sent)
	default:
		m.covered.schedule.asks++
	}
	s.base, s.due = at, time.Time{}
	return nil
}

func due(s *schedule) time.Time {
	if s == nil {
		return time.Time{}
	}
	return s.due
}

// reportDue holds a request for a report to its rules at the moment it is written: it goes to the lead
// of a run that may be asked, says the run's interval and how many reports the lead has posted, and is
// not turned away.
func (m *monitor) reportDue(msg core.Message, b core.ReportDue) error {
	run := m.runs[msg.Run]
	switch {
	case msg.To != run.Lead:
		return fmt.Errorf("request for a report %s went to %s, not the lead %s", msg.ID, msg.To, run.Lead)
	case !m.askable(msg.Run):
		return fmt.Errorf("request for a report %s in run %s, which is %s, has reached %q and has a final report: %t",
			msg.ID, run.ID, run.Status, run.LimitReached, m.finished[msg.Run])
	case msg.Delivery != core.DeliveryQueued && msg.Delivery != core.DeliverySteered:
		return fmt.Errorf("request for a report %s was %s on arrival", msg.ID, msg.Delivery)
	case b.Every != run.SummaryEvery.Truncate(time.Second) || b.Reports != run.ReportsPosted:
		return fmt.Errorf("request for a report %s says %+v in run %+v", msg.ID, b, run)
	}
	return nil
}

// summaryBooks holds the fields the core folded into every run to the books, and the timers it armed
// to the requests that became due: one timer for each time a request became due, and none besides. A
// request that became due and was taken away within one step, by a limit reached in the same step, still
// has its timer, which fires for a time no run holds and does nothing.
func (m *monitor) summaryBooks(effects []core.Effect) error {
	armed := map[core.RunID][]time.Time{}
	for _, e := range effects {
		if t, ok := e.(core.ArmTimer); ok && t.Kind == core.TimerSummaryDue {
			armed[t.Run] = append(armed[t.Run], t.FireAt)
		}
	}
	for id, s := range m.books {
		run := m.runs[id]
		if !s.due.Equal(run.NextSummaryAt) || !s.base.Equal(run.LastSummaryAt) || s.every != run.SummaryEvery {
			return fmt.Errorf("run %s holds its schedule as every %v from %v, next %v, and the books say every %v from %v, next %v",
				id, run.SummaryEvery, run.LastSummaryAt, run.NextSummaryAt, s.every, s.base, s.due)
		}
		if run.SummaryReport != m.summary[id] || m.windows[run.SummaryReport] {
			return fmt.Errorf("run %s is summed up by report %q, and the newest report about the run is %q (a window report: %t)",
				id, run.SummaryReport, m.summary[id], m.windows[run.SummaryReport])
		}
		if got, want := armed[id], m.arms[id]; !slices.EqualFunc(got, want, time.Time.Equal) {
			return fmt.Errorf("run %s armed summary timers for %v, and its requests became due at %v", id, got, want)
		}
	}
	return nil
}
