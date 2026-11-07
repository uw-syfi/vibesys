package core_test

import (
	"fmt"
	"reflect"
	"slices"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// checkInCoverage counts what the simulations reached of Catch me up, so that its properties cannot
// hold by never being tested.
type checkInCoverage struct {
	// made are check-ins pressed, repeated the presses answered with a pending one, unviewed the
	// presses that named no viewer, asked the requests sent to a lead when the facts were ready, inFlight
	// the answers that sent none because a request was on its way (behindSchedule: the scheduled one),
	// cannot those that sent none because the run could not be asked, ready and failed the answers, linked
	// the reports that answered a request, overRuns the presses on a run that was over, and empty the
	// windows with nothing in them. windowReports are the linked reports that were about the window alone
	// (progress), and finalAnswers those the lead gave as its last word, which are about the run.
	made, repeated, unviewed, asked, inFlight, behindSchedule, cannot, ready, failed, linked, overRuns, empty int
	windowReports, finalAnswers                                                                               int
}

func (c *checkInCoverage) add(o checkInCoverage) {
	c.made += o.made
	c.repeated += o.repeated
	c.unviewed += o.unviewed
	c.asked += o.asked
	c.inFlight += o.inFlight
	c.behindSchedule += o.behindSchedule
	c.cannot += o.cannot
	c.ready += o.ready
	c.failed += o.failed
	c.linked += o.linked
	c.overRuns += o.overRuns
	c.empty += o.empty
	c.windowReports += o.windowReports
	c.finalAnswers += o.finalAnswers
}

// checkInRecord is the monitor's own account of one check-in, kept from the committed events.
type checkInRecord struct {
	id          core.CheckInID
	run         core.RunID
	viewer      core.ViewerID
	since, at   time.Time
	state       string
	facts       core.WindowFacts
	failure     string
	ask, report core.MessageID
}

// checkInBooks is every check-in the monitor has seen, in the order made. The rules it holds the core
// to are written here from the contract and not from the core's code.
type checkInBooks struct {
	records map[core.CheckInID]*checkInRecord
	order   []core.CheckInID
}

func newCheckInBooks() *checkInBooks {
	return &checkInBooks{records: map[core.CheckInID]*checkInRecord{}}
}

// pending is the viewer's check-in in a run that is still being worked out.
func (b *checkInBooks) pending(run core.RunID, viewer core.ViewerID) *checkInRecord {
	for _, id := range b.order {
		if r := b.records[id]; r.run == run && r.viewer == viewer && r.state == "pending" {
			return r
		}
	}
	return nil
}

// lastEnd is where the viewer's next window starts: the end of the last check-in that did not fail, or
// the run's start.
func (b *checkInBooks) lastEnd(run core.Run, viewer core.ViewerID) time.Time {
	start := run.CreatedAt
	for _, id := range b.order {
		if r := b.records[id]; r.run == run.ID && r.viewer == viewer && r.state != "failed" && r.at.After(start) {
			start = r.at
		}
	}
	return start
}

func outcomeState(c core.CheckIn) (state string, facts core.WindowFacts, failure string) {
	switch o := c.Outcome.(type) {
	case core.CheckInPending:
		return "pending", core.WindowFacts{}, ""
	case core.CheckInReady:
		return "ready", o.Facts, ""
	case core.CheckInFailed:
		return "failed", core.WindowFacts{}, o.Detail
	}
	return "unknown", core.WindowFacts{}, ""
}

// checkIns holds one transaction to the rules of Catch me up, before the events are folded into the
// monitor's books of runs and messages, so that a rule about what was true when the input came reads
// that and not what the events made of it.
func (m *monitor) checkIns(body core.InputBody, events []core.Event, effects []core.Effect) error {
	if len(events) == 0 {
		return nil // a repeat decides nothing
	}
	var changed []core.CheckIn
	var asks []core.Message
	var receipt *core.Receipt
	for _, e := range events {
		switch b := e.Body.(type) {
		case core.CheckIn:
			changed = append(changed, b)
		case core.Message:
			if _, ok := b.Body.(core.CatchUpRequest); ok {
				if b.Delivery == core.DeliverySteered {
					return fmt.Errorf("the request %s was steered into a running turn", b.ID)
				}
				// One message is written again as it moves on, in a transaction as in a run.
				if m.messages[b.ID] == nil && !slices.ContainsFunc(asks, func(a core.Message) bool { return a.ID == b.ID }) {
					asks = append(asks, b)
				}
			}
		case core.Receipt:
			r := b
			receipt = &r
		}
	}
	var windows []core.ComputeWindowFacts
	for _, e := range effects {
		if w, ok := e.(core.ComputeWindowFacts); ok {
			windows = append(windows, w)
		}
	}
	var err error
	switch in := body.(type) {
	case core.Command:
		switch c := in.Body.(type) {
		case core.RequestCatchUp:
			err = m.press(in, c, receipt, changed, asks, windows, events)
		case core.PostReport:
			err = m.answer(in, receipt, changed)
		default:
			err = m.nothingAbout(changed, asks, windows, body)
		}
	case core.WindowFactsReady:
		err = m.facts(in.CheckIn, "ready", changed, asks, func(r *checkInRecord, c core.CheckIn) error {
			if _, facts, _ := outcomeState(c); !reflect.DeepEqual(facts, in.Facts) {
				return fmt.Errorf("check-in %s froze facts %+v, which are not the ones answered: %+v", c.ID, facts, in.Facts)
			}
			return m.askWhenReady(r, c, in.Facts, asks)
		})
	case core.WindowFactsFailed:
		err = m.facts(in.CheckIn, "failed", changed, asks, func(r *checkInRecord, c core.CheckIn) error {
			if _, _, failure := outcomeState(c); failure != in.Detail {
				return fmt.Errorf("check-in %s failed saying %q, want %q", c.ID, failure, in.Detail)
			}
			if len(asks) != 0 || c.Ask != "" {
				return fmt.Errorf("check-in %s failed and asked the lead %+v", c.ID, asks)
			}
			return nil
		})
	default:
		err = m.nothingAbout(changed, asks, windows, body)
	}
	if err != nil {
		return err
	}
	return m.keepCheckIns(changed)
}

// nothingAbout holds every other input to leaving check-ins, requests for a report and the effects that
// work facts out alone.
func (m *monitor) nothingAbout(changed []core.CheckIn, asks []core.Message, windows []core.ComputeWindowFacts, body core.InputBody) error {
	if len(changed) > 0 || len(asks) > 0 || len(windows) > 0 {
		return fmt.Errorf("%T changed check-ins %+v, sent requests %+v or asked for facts %+v", body, changed, asks, windows)
	}
	return nil
}

// press holds a press of Catch me up to its rules.
func (m *monitor) press(in core.Command, c core.RequestCatchUp, receipt *core.Receipt, changed []core.CheckIn, asks []core.Message, windows []core.ComputeWindowFacts, events []core.Event) error {
	if receipt == nil {
		return fmt.Errorf("a press left no receipt")
	}
	refused := func(want core.Refusal) error {
		if receipt.Refusal != want || receipt.CheckIn != "" || len(changed) > 0 || len(asks) > 0 || len(windows) > 0 {
			return fmt.Errorf("a press that must be refused as %s was answered %+v with check-ins %+v, requests %+v and effects %+v", want, *receipt, changed, asks, windows)
		}
		return nil
	}
	op, operator := in.Caller.(core.Operator)
	run, known := m.runs[c.Run]
	switch {
	case !operator:
		return refused(core.RefuseNotPermitted)
	case !known:
		return refused(core.RefuseRunNotFound)
	case op.Viewer.ID == "" || op.Viewer.Label == "":
		m.covered.checkIns.unviewed++
		return refused(core.RefuseNoViewer)
	}
	books := m.checkInBooks
	if pending := books.pending(run.ID, op.Viewer.ID); pending != nil {
		m.covered.checkIns.repeated++
		if receipt.Refusal != "" || receipt.CheckIn != pending.id || receipt.Message != pending.ask || len(changed) > 0 || len(asks) > 0 || len(windows) > 0 {
			return fmt.Errorf("a press while check-in %+v is pending was answered %+v with new check-ins %+v, requests %+v and effects %+v", *pending, *receipt, changed, asks, windows)
		}
		return nil
	}
	if receipt.Refusal != "" || len(changed) != 1 || len(windows) != 1 {
		return fmt.Errorf("a press was answered %+v with check-ins %+v and effects %+v", *receipt, changed, windows)
	}
	check := changed[0]
	since := books.lastEnd(run, op.Viewer.ID)
	at := latest(events[len(events)-1].At, since)
	if run.Status.Final() && !run.EndedAt.IsZero() && run.EndedAt.Before(at) {
		at = latest(run.EndedAt, since)
	}
	if _, isPending := check.Outcome.(core.CheckInPending); !isPending || check.Run != run.ID || check.Viewer != op.Viewer ||
		!check.Since.Equal(since) || !check.At.Equal(at) || receipt.CheckIn != check.ID || receipt.Run != run.ID {
		return fmt.Errorf("a press on %+v by %+v made %+v, want a pending window %v to %v (receipt %+v)", run, op.Viewer, check, since, at, *receipt)
	}
	if w := windows[0]; w != (core.ComputeWindowFacts{CheckIn: check.ID, Run: run.ID, Since: since, Until: at}) {
		return fmt.Errorf("the facts of %+v were asked for as %+v", check, w)
	}
	if run.Status.Final() {
		m.covered.checkIns.overRuns++
	}
	if since.Equal(at) {
		m.covered.checkIns.empty++
	}
	m.covered.checkIns.made++
	if receipt.Message != "" || check.Ask != "" || len(asks) != 0 {
		return fmt.Errorf("a press asked the lead before the facts were frozen: receipt %+v, check-in %+v, requests %+v", *receipt, check, asks)
	}
	return nil
}

// askWhenReady holds the request for a report to its rules: when the facts freeze it is sent, with them,
// to a lead that can be asked and that has no other request on its way, once, for the window of the
// check-in, and not otherwise. A request on its way is a viewer's or the schedule's.
func (m *monitor) askWhenReady(rec *checkInRecord, check core.CheckIn, facts core.WindowFacts, asks []core.Message) error {
	run := m.runs[rec.run]
	scheduled, viewers := m.requestsInTransit(run.ID)
	canBeAsked := run.Status.Live() && run.LimitReached == "" && !m.finished[run.ID]
	switch {
	case canBeAsked && !scheduled && !viewers:
		if len(asks) != 1 || check.Ask != asks[0].ID {
			return fmt.Errorf("a lead that could be asked was sent requests %+v for %+v", asks, check)
		}
		want := core.CatchUpRequest{CheckIn: check.ID, Since: check.Since, Until: check.At}
		if got := asks[0]; !reflect.DeepEqual(got.Body, want) || got.To != run.Lead || got.From != "" || got.Run != run.ID {
			return fmt.Errorf("the request was %+v, want %+v to %s", got, want, run.Lead)
		}
		m.covered.checkIns.asked++
	default:
		if len(asks) != 0 || check.Ask != "" {
			return fmt.Errorf("a run that was not to be asked (live %t, limit %q, finished %t, request in flight: scheduled %t, a viewer's %t) was sent %+v for %+v", run.Status.Live(), run.LimitReached, m.finished[run.ID], scheduled, viewers, asks, check)
		}
		switch {
		case canBeAsked && scheduled:
			m.covered.checkIns.behindSchedule++
		case canBeAsked:
			m.covered.checkIns.inFlight++
		default:
			m.covered.checkIns.cannot++
		}
	}
	return nil
}

// facts holds an answer to the work of a check-in: a pending check-in takes it once, and it is the only
// thing that changes; any other answer, such as a repeat, changes nothing.
func (m *monitor) facts(id core.CheckInID, want string, changed []core.CheckIn, asks []core.Message, check func(*checkInRecord, core.CheckIn) error) error {
	rec := m.checkInBooks.records[id]
	if rec == nil || rec.state != "pending" {
		if len(changed) > 0 || len(asks) > 0 {
			return fmt.Errorf("an answer for check-in %s, which is not pending, changed %+v and sent %+v", id, changed, asks)
		}
		return nil
	}
	if len(changed) != 1 {
		return fmt.Errorf("an answer for pending check-in %s changed %+v", id, changed)
	}
	if state, _, _ := outcomeState(changed[0]); state != want {
		return fmt.Errorf("check-in %s is %s after an answer of %s", id, state, want)
	}
	if want == "ready" {
		m.covered.checkIns.ready++
	} else {
		m.covered.checkIns.failed++
	}
	return check(rec, changed[0])
}

// answer holds a report the lead posts to its rule: it answers the request the lead's running turn was
// started by, and only the first report of that turn does; any other report changes no check-in.
func (m *monitor) answer(in core.Command, receipt *core.Receipt, changed []core.CheckIn) error {
	var want *checkInRecord
	if caller, ok := in.Caller.(core.AgentCaller); ok && receipt != nil && receipt.Refusal == "" {
		if turn, running := m.running[caller.Agent]; running {
			asked := m.turns[turn].Message
			if r := m.messages[asked]; r != nil {
				if cu, ok := r.body.(core.CatchUpRequest); ok {
					if rec := m.checkInBooks.records[cu.CheckIn]; rec != nil && rec.ask == asked && rec.report == "" {
						want = rec
					}
				}
			}
		}
	}
	if want == nil {
		if len(changed) > 0 {
			return fmt.Errorf("a report that answers no request changed check-ins %+v", changed)
		}
		return nil
	}
	if len(changed) != 1 || changed[0].ID != want.id || changed[0].Report != receipt.Message || receipt.Message == "" {
		return fmt.Errorf("a report %q posted in the turn of request %s should have answered check-in %s, and changed %+v", receipt.Message, want.ask, want.id, changed)
	}
	m.covered.checkIns.linked++
	// A progress report that answers a request is about the window it covers; the lead's last word is about
	// the run, whatever asked for it.
	if post, ok := in.Body.(core.PostReport); ok && post.Report.Kind == core.ReportProgress {
		m.windows[receipt.Message] = true
		m.covered.checkIns.windowReports++
	} else {
		m.covered.checkIns.finalAnswers++
	}
	return nil
}

// keepCheckIns folds check-in events into the books, and holds each to what may change in one: a check-in
// is made pending and settled once, its window and its request never change, its facts are frozen, and a
// report is linked once and only where a request was sent.
func (m *monitor) keepCheckIns(changed []core.CheckIn) error {
	books := m.checkInBooks
	for _, c := range changed {
		state, facts, failure := outcomeState(c)
		rec := books.records[c.ID]
		if c.At.Before(c.Since) || c.Viewer.ID == "" || (c.Report != "" && c.Ask == "") || state == "unknown" {
			return fmt.Errorf("check-in %+v breaks its own shape", c)
		}
		if rec == nil {
			if state != "pending" || c.Report != "" || c.Ask != "" {
				return fmt.Errorf("check-in %+v began as %s with a report or a request", c, state)
			}
			books.records[c.ID] = &checkInRecord{id: c.ID, run: c.Run, viewer: c.Viewer.ID, since: c.Since, at: c.At, state: state}
			books.order = append(books.order, c.ID)
			continue
		}
		// The request is made once, in the same commit as the facts freeze, and is never changed after.
		asked := rec.ask != "" || (rec.state == "pending" && state == "ready")
		switch {
		case c.Run != rec.run || c.Viewer.ID != rec.viewer || !c.Since.Equal(rec.since) || !c.At.Equal(rec.at) || (rec.ask != "" && c.Ask != rec.ask) || (c.Ask != "" && !asked):
			return fmt.Errorf("check-in %s changed its window or its request: %+v, was %+v", c.ID, c, *rec)
		case rec.state != "pending" && (state != rec.state || !reflect.DeepEqual(facts, rec.facts) || failure != rec.failure):
			return fmt.Errorf("check-in %s changed after it was %s: %+v, was %+v", c.ID, rec.state, c, *rec)
		case rec.state == "pending" && state == "pending" && c.Report == rec.report:
			return fmt.Errorf("check-in %s was written again with no change", c.ID)
		case rec.report != "" && c.Report != rec.report:
			return fmt.Errorf("check-in %s changed its report from %q to %q", c.ID, rec.report, c.Report)
		}
		rec.state, rec.facts, rec.failure, rec.report, rec.ask = state, facts, failure, c.Report, c.Ask
	}
	return nil
}

// checkInsFinal holds what must be true when everything has settled: no check-in waits for its facts, and
// each viewer's follow one another from the start of the run with no gap and no overlap.
func (m *monitor) checkInsFinal() error {
	books := m.checkInBooks
	chains := map[[2]string][]*checkInRecord{}
	for _, id := range books.order {
		r := books.records[id]
		if r.state == "pending" {
			return fmt.Errorf("check-in %s never got its answer", id)
		}
		if r.state != "failed" {
			key := [2]string{string(r.run), string(r.viewer)}
			chains[key] = append(chains[key], r)
		}
	}
	for key, chain := range chains {
		run := m.runs[core.RunID(key[0])]
		next := run.CreatedAt
		for _, r := range chain {
			if !r.since.Equal(next) || r.at.Before(r.since) {
				return fmt.Errorf("viewer %s's check-ins in run %s do not follow one another: %+v starts where %v ended", key[1], key[0], *r, next)
			}
			next = r.at
		}
	}
	return nil
}

// drawnFacts is a set of facts a host could have worked out, with every field the contract holds set
// to something that varies, so that a check-in that kept less than it was given is seen.
func drawnFacts(n func(int) int) core.WindowFacts {
	best := func() *core.Best {
		return &core.Best{Value: float64(n(64)) / 4, Job: core.JobID(fmt.Sprintf("j%d", n(9))), Commit: core.Commit(fmt.Sprintf("c%d", n(9))), At: epoch.Add(time.Duration(n(1000)) * time.Second)}
	}
	cost := float64(n(40)) / 8
	tokens := uint64(1 + n(900))
	f := core.WindowFacts{
		Spend:         core.Spend{Turns: uint32(1 + n(9)), Usage: core.Usage{InputTokens: &tokens}},
		AgentsStarted: uint32(n(5)), AgentsEnded: uint32(n(5)), JobsMeasured: uint32(n(7)),
	}
	f.Spend.Unpriced = uint32(n(int(f.Spend.Turns) + 1))
	if n(2) == 0 {
		f.Spend.Usage.CostUSD = &cost
	}
	for _, role := range []core.AgentRole{core.RoleLead, core.RoleWorker, core.RoleJudge} {
		if n(2) == 0 {
			f.Spend.Roles = append(f.Spend.Roles, core.RoleSpend{Role: role, Turns: uint32(1 + n(3)), Usage: core.Usage{CostUSD: &cost}})
		}
	}
	for _, metric := range []string{"lat", "tput"} {
		if n(2) == 0 {
			move := core.ObjectiveMove{Metric: metric, Direction: []core.Direction{core.Maximize, core.Minimize}[n(2)], End: best()}
			if n(2) == 0 {
				move.Start = best()
			}
			f.Objectives = append(f.Objectives, move)
		}
	}
	for i := range n(4) {
		f.PlansCreated = append(f.PlansCreated, uint32(1+i))
	}
	for i := range n(3) {
		f.PlansChanged = append(f.PlansChanged, uint32(10+i))
	}
	for i := range n(3) {
		f.Reports = append(f.Reports, core.MessageID(fmt.Sprintf("m%d", n(50)+i*50)))
	}
	if n(3) == 0 {
		f = core.WindowFacts{}
	}
	slices.Sort(f.PlansCreated)
	return f
}
