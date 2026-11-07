package core

import (
	"cmp"
	"fmt"
	"maps"
	"slices"
	"time"
)

// This file holds Catch me up. A viewer presses it on a run and the host records a check-in: a stored
// summary of everything that happened since that viewer's last check-in, with figures the host worked
// out and froze at the time. The viewer sees the check-in again, unchanged, in a list of the ones before
// it. On a run that is live the press also asks the lead for a report covering the same window.
//
// A viewer is the label of an install of a client (see Operator). The host trusts it for nothing but
// whose check-in this is: there is still one operator.
//
// The window. A viewer's check-ins in a run follow one another with no gap and no overlap: each one's
// window starts where the viewer's last one that did not fail ended, and the first starts with the run.
// A window ends at the press, or, when the run is over, at the end of the run (nothing happens after
// it, and a window that went on would take in what a later run did to the same repository and task). A
// press that is not after the window's start, because the clock did not move, is an empty window. A
// check-in that failed is left out of the chain, so the viewer's next press covers what the failed one
// would have, and no moment is skipped.
//
// The facts. They are not in the State, so the press cannot compute them: it commits the check-in
// pending, with a ComputeWindowFacts effect, and the shell answers with WindowFactsReady or
// WindowFactsFailed. The effect is in the same commit as the check-in, so a restart finds it and
// carries it out again, and an answer for a check-in that is no longer pending changes nothing. The
// facts are then in the check-in's event: a replay reads them and never works them out again, which is
// why a past check-in reads the same forever.
//
// The ask. A run that can be asked (see canBeAsked) also gets a CatchUpRequest sent to its lead
// through the ordinary delivery path. It never steers into a turn that is running: it starts a turn of
// its own, after the one that is running, so the lead is not interrupted and the report it posts in that
// turn is known to answer the request. That report is linked to the check-in. No agent but the lead is
// involved and nothing about it is special in cost: it is the lead's turn like any other.
//
// A press while the viewer's last check-in in the run is still pending answers with that check-in, so a
// retry, or a second press before the first is answered, makes no second window and sends no second
// request. A press by another viewer, or after the first is ready, while the lead has still not
// answered the last request, makes its own check-in and sends no request: each request costs the lead a
// turn, and the report in flight is the one to read.

// Viewer is the label a client chose for its install: an id that stays the same for the install and a
// name to show.
type Viewer struct {
	ID    ViewerID
	Label string
}

// CheckInOutcome is how far a check-in has come. The set is closed: a check-in is pending until its
// facts are worked out, then ready with them, or failed with the reason they could not be.
type CheckInOutcome interface{ checkInOutcome() }

// The outcomes.
type (
	// CheckInPending says the facts are being worked out.
	CheckInPending struct{}
	// CheckInReady says the facts are worked out, and holds them. They never change.
	CheckInReady struct{ Facts WindowFacts }
	// CheckInFailed says the facts could not be worked out, and why.
	CheckInFailed struct{ Detail string }
)

func (CheckInPending) checkInOutcome() {}
func (CheckInReady) checkInOutcome()   {}
func (CheckInFailed) checkInOutcome()  {}

// CheckIn is what one press of Catch me up recorded.
type CheckIn struct {
	ID     CheckInID
	Run    RunID
	Viewer Viewer
	// Since and At bound the window [Since, At).
	Since, At time.Time
	Outcome   CheckInOutcome
	// Ask is the message that asked the lead for a report, empty when the run could not be asked.
	Ask MessageID
	// Report is the report the lead posted in the turn the ask started, empty until it does.
	Report MessageID
}

// CheckIns returns a run's check-ins, oldest first.
func (s State) CheckIns(run RunID) []CheckIn {
	var out []CheckIn
	for _, c := range s.checkIns {
		if c.Run == run {
			out = append(out, c)
		}
	}
	slices.SortFunc(out, func(a, b CheckIn) int {
		return cmp.Or(a.At.Compare(b.At), a.Since.Compare(b.Since), cmp.Compare(a.ID, b.ID))
	})
	return out
}

// CheckIn returns a check-in.
func (s State) CheckIn(id CheckInID) (CheckIn, bool) {
	c, ok := s.checkIns[id]
	return c, ok
}

// canBeAsked says the lead of a run can be asked for a report now: the run is live, has reached no limit
// (its lead was told to finish) and its lead has not given its final report.
func (r Run) canBeAsked() bool {
	return r.Status.Live() && r.LimitReached == "" && !r.FinalReported
}

// ownTurn says a message starts a turn of its own and is never steered into one that is running.
func (m Message) ownTurn() bool {
	_, ok := m.Body.(CatchUpRequest)
	return ok
}

func callerViewer(c Command) Viewer {
	if op, ok := c.Caller.(Operator); ok {
		return op.Viewer
	}
	return Viewer{}
}

// viewerChain returns what a viewer's press in a run has to follow: the viewer's check-in that is
// pending, if any, and the end of the last one that did not fail, or the start of the run.
func (t *tx) viewerChain(run Run, viewer ViewerID) (pending CheckIn, isPending bool, since time.Time) {
	since = run.CreatedAt
	for _, c := range t.s.CheckIns(run.ID) {
		if c.Viewer.ID != viewer {
			continue
		}
		switch c.Outcome.(type) {
		case CheckInFailed:
			// A failed check-in is not part of the chain: the next window starts where it started.
		case CheckInPending:
			pending, isPending = c, true
		default:
			since = later(since, c.At)
		}
	}
	return pending, isPending, since
}

func (t *tx) requestCatchUp(c Command, b RequestCatchUp, r *Receipt) RunID {
	run, ok := t.s.runs[b.Run]
	viewer := callerViewer(c)
	switch {
	case !ok:
		r.Refusal = RefuseRunNotFound
		return ""
	case viewer.ID == "" || viewer.Label == "":
		r.Run, r.Refusal = run.ID, RefuseNoViewer
		return ""
	}
	r.Run = run.ID
	pending, isPending, since := t.viewerChain(run, viewer.ID)
	if isPending {
		r.CheckIn, r.Message = pending.ID, pending.Ask
		return ""
	}
	// A window never ends before it starts, whatever the clock did, and one over a run that is over
	// ends where the run did.
	at := later(t.in.At, since)
	if run.Status.Final() && !run.EndedAt.IsZero() && run.EndedAt.Before(at) {
		at = later(run.EndedAt, since)
	}
	check := CheckIn{ID: CheckInID(t.mint("checkin")), Run: run.ID, Viewer: viewer, Since: since, At: at, Outcome: CheckInPending{}}
	if run.canBeAsked() && !t.catchUpInTransit(run.ID) {
		check.Ask = t.post(Message{Run: run.ID, To: run.Lead, Body: CatchUpRequest{CheckIn: check.ID, Since: since, Until: at}}).ID
		r.Message = check.Ask
	}
	t.emit(run.ID, check)
	t.effect(ComputeWindowFacts{CheckIn: check.ID, Run: run.ID, Since: since, Until: at})
	r.CheckIn = check.ID
	return run.ID
}

// catchUpInTransit says a request for a report is on its way to the run's lead, or being read by it.
// Another press does not send a second: the lead's report answers the first, and the later check-in's
// window is shown with the reports posted inside it, as one of a run that could not be asked is.
func (t *tx) catchUpInTransit(id RunID) bool {
	for _, m := range t.s.messages {
		if _, ok := m.Body.(CatchUpRequest); ok && m.Run == id {
			return true
		}
	}
	return false
}

// windowFactsReady freezes the facts of a pending check-in.
func (t *tx) windowFactsReady(b WindowFactsReady) {
	t.settleCheckIn(b.CheckIn, CheckInReady{Facts: b.Facts})
}

// windowFactsFailed fails a pending check-in.
func (t *tx) windowFactsFailed(b WindowFactsFailed) {
	t.settleCheckIn(b.CheckIn, CheckInFailed{Detail: b.Detail})
}

func (t *tx) settleCheckIn(id CheckInID, outcome CheckInOutcome) {
	check, ok := t.s.checkIns[id]
	if _, pending := check.Outcome.(CheckInPending); !ok || !pending {
		return
	}
	check.Outcome = outcome
	t.emit(check.Run, check)
}

// catchUpAsked returns the check-in a report the lead posts now would answer: the one whose request
// started the turn the lead is in, when no report has answered it yet. A report posted in any other turn,
// or a second one in that turn, answers nothing, and it is by this that a report is known to be about a
// window and not the run.
func (t *tx) catchUpAsked(lead agentState) (CheckIn, bool) {
	if lead.turn == nil {
		return CheckIn{}, false
	}
	ask, ok := t.s.messages[lead.turn.Message]
	if !ok {
		return CheckIn{}, false
	}
	body, ok := ask.Body.(CatchUpRequest)
	if !ok {
		return CheckIn{}, false
	}
	check, ok := t.s.checkIns[body.CheckIn]
	if !ok || check.Report != "" || check.Ask != ask.ID {
		return CheckIn{}, false
	}
	return check, true
}

// linkCatchUp links the report that answers a check-in to it.
func (t *tx) linkCatchUp(check CheckIn, report MessageID) {
	check.Report = report
	t.emit(check.Run, check)
}

// checkInBreaches says how the check-ins of a run disagree with the rules above. A viewer's check-ins
// that did not fail follow one another from the start of the run with no gap and no overlap, only one
// of them is pending, and a report is only linked to a check-in that asked for one.
func (s State) checkInBreaches(run Run) []string {
	var found []string
	byViewer := map[ViewerID][]CheckIn{}
	for _, c := range s.CheckIns(run.ID) {
		if _, failed := c.Outcome.(CheckInFailed); !failed {
			byViewer[c.Viewer.ID] = append(byViewer[c.Viewer.ID], c)
		}
		if c.At.Before(c.Since) {
			found = append(found, fmt.Sprintf("has check-in %s that ends at %v, before it starts at %v", c.ID, c.At, c.Since))
		}
		if c.Report != "" && c.Ask == "" {
			found = append(found, fmt.Sprintf("has check-in %s with report %s and no request for one", c.ID, c.Report))
		}
		if c.Viewer.ID == "" {
			found = append(found, fmt.Sprintf("has check-in %s of no viewer", c.ID))
		}
	}
	for _, viewer := range slices.Sorted(maps.Keys(byViewer)) {
		chain := byViewer[viewer]
		slices.SortFunc(chain, func(a, b CheckIn) int {
			return cmp.Or(a.Since.Compare(b.Since), a.At.Compare(b.At), cmp.Compare(a.ID, b.ID))
		})
		next, pending := run.CreatedAt, 0
		for _, c := range chain {
			if !c.Since.Equal(next) {
				found = append(found, fmt.Sprintf("has viewer %s's check-in %s starting at %v where the last ended at %v", viewer, c.ID, c.Since, next))
			}
			next = c.At
			if _, ok := c.Outcome.(CheckInPending); ok {
				pending++
			}
		}
		if pending > 1 {
			found = append(found, fmt.Sprintf("has %d check-ins of viewer %s pending at once", pending, viewer))
		}
	}
	return found
}
