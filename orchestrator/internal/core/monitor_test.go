package core_test

import (
	"fmt"
	"reflect"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// coverage counts what a simulation reached, so that a property cannot pass by never being tested.
type coverage struct {
	handoffs, handoffTurns, steers, resumes, refusedSpawns, stops, lost, rejected, jobs int
	retriedDeliveries, operatorStops, deadlineStops, runsFailed, leadsOutOfResumes      int
	costStops, limitMessages, limitRefusals, finishedStops                              int
	startsFailed, startsTimedOut                                                        int
	stalls, leadReminders, handoffReminders, reports                                    int
	schedule                                                                            scheduleCoverage
	checkIns                                                                            checkInCoverage
}

func (c *coverage) add(o coverage) {
	c.handoffs += o.handoffs
	c.handoffTurns += o.handoffTurns
	c.steers += o.steers
	c.resumes += o.resumes
	c.refusedSpawns += o.refusedSpawns
	c.stops += o.stops
	c.lost += o.lost
	c.rejected += o.rejected
	c.jobs += o.jobs
	c.retriedDeliveries += o.retriedDeliveries
	c.operatorStops += o.operatorStops
	c.deadlineStops += o.deadlineStops
	c.costStops += o.costStops
	c.limitMessages += o.limitMessages
	c.limitRefusals += o.limitRefusals
	c.finishedStops += o.finishedStops
	c.runsFailed += o.runsFailed
	c.leadsOutOfResumes += o.leadsOutOfResumes
	c.startsFailed += o.startsFailed
	c.startsTimedOut += o.startsTimedOut
	c.stalls += o.stalls
	c.leadReminders += o.leadReminders
	c.handoffReminders += o.handoffReminders
	c.reports += o.reports
	c.schedule.add(o.schedule)
	c.checkIns.add(o.checkIns)
}

func (c coverage) require(t *testing.T) {
	t.Helper()
	t.Logf("coverage: %+v", c)
	for name, n := range map[string]int{
		"handoffs": c.handoffs, "turns on a handoff": c.handoffTurns, "steers": c.steers, "resumes": c.resumes,
		"refused spawns": c.refusedSpawns, "stops": c.stops, "lost agents": c.lost, "rejected messages": c.rejected,
		"finished jobs": c.jobs, "redelivered messages": c.retriedDeliveries, "operator stops": c.operatorStops,
		"time limit stops": c.deadlineStops, "cost limit stops": c.costStops, "messages that a limit was reached": c.limitMessages,
		"spawns refused for a limit": c.limitRefusals, "stops of a lead that had finished": c.finishedStops, "failed runs": c.runsFailed, "leads out of resumes": c.leadsOutOfResumes,
		"sessions that failed to start": c.startsFailed, "sessions that timed out opening": c.startsTimedOut,
		"stalled runs": c.stalls, "reminders to a lead": c.leadReminders, "reminders to hand off": c.handoffReminders,
		"reports": c.reports, "requests for a report": c.schedule.asks, "requests for a report that were still unread": c.schedule.swallowed,
		"summary timers that were stale": c.schedule.stale, "reports that restarted the interval": c.schedule.restarted,
		"requests for a report that a limit, a stop or a final report took away": c.schedule.cancelled,
		"check-ins pressed": c.checkIns.made, "presses answered with a pending check-in": c.checkIns.repeated,
		"presses with no viewer": c.checkIns.unviewed, "requests for a catch-up report": c.checkIns.asked,
		"answers with a viewer's request in flight": c.checkIns.inFlight, "answers with the schedule's request in flight": c.checkIns.behindSchedule,
		"scheduled requests held back by a viewer's": c.schedule.behindCatchUp, "answers on a run that could not be asked": c.checkIns.cannot,
		"check-ins made ready": c.checkIns.ready, "check-ins failed": c.checkIns.failed,
		"reports that answered a catch-up": c.checkIns.linked, "presses on a run that was over": c.checkIns.overRuns,
		"empty windows": c.checkIns.empty,
	} {
		if n == 0 {
			t.Errorf("the simulation never reached: %s", name)
		}
	}
}

// messageRecord is the monitor's own account of one message.
type messageRecord struct {
	run      core.RunID
	to       core.AgentID
	body     core.MessageBody
	last     core.Delivery
	previous core.Delivery
	settled  int
	turns    int
	running  int
	finished int
}

// toldJob is a job as one run's lead is told of it: job ids repeat across runs.
type toldJob struct {
	run core.RunID
	job core.JobID
}

// monitor checks the guarantees from the committed log alone, keeping its own books, so it shares
// no code and no bug with the core.
type monitor struct {
	covered  coverage
	turns    map[core.TurnID]core.Turn
	running  map[core.AgentID]core.TurnID
	ordinals map[core.AgentID]uint32
	items    map[core.AgentID]uint32
	open     map[core.TurnID]map[core.ItemID]bool
	agents   map[core.AgentID]core.Agent
	runs     map[core.RunID]core.Run
	messages map[core.MessageID]*messageRecord
	stalled  map[core.RunID]bool
	jobs     map[toldJob]core.MessageID
	receipts map[core.CommandID]core.Receipt
	offsets  map[core.Session]uint64
	// handed holds the agents whose handoff stands: nothing has given them new work since.
	handed map[core.AgentID]bool
	// failures counts each agent's sessions lost in a row, with no completed turn between them.
	failures map[core.AgentID]uint32
	// leadReminders counts the reminders each run's lead got, handoffReminders those each session got,
	// and reports the reports each run recorded.
	leadReminders    map[core.RunID]int
	handoffReminders map[core.Session]int
	reports          map[core.RunID]uint32
	// finished holds the runs whose lead's latest report is final and which have started no new work
	// since: no agent spawned, no more jobs running, no progress report.
	finished map[core.RunID]bool
	// limited holds the runs that were told their limit was reached, with the message that told them.
	limited map[core.RunID]core.MessageID
	// books holds each run's report schedule as the monitor works it out, and startEvery the interval
	// the start being checked asked for, zero for none.
	books      map[core.RunID]*schedule
	startEvery time.Duration
	// checkInBooks holds the check-ins as the monitor works them out.
	checkInBooks *checkInBooks
	// arms holds, for the commit being checked, each time a request became due in a run.
	arms map[core.RunID][]time.Time

	// The rest lasts one transaction.
	refusedSteer core.MessageID
	idled        map[core.AgentID]bool
	over         map[core.AgentID]bool
	noticed      map[core.AgentID]int
	resumed      map[core.AgentID]bool
}

func newMonitor() *monitor {
	return &monitor{
		turns: map[core.TurnID]core.Turn{}, running: map[core.AgentID]core.TurnID{}, ordinals: map[core.AgentID]uint32{},
		items: map[core.AgentID]uint32{}, open: map[core.TurnID]map[core.ItemID]bool{}, agents: map[core.AgentID]core.Agent{},
		runs: map[core.RunID]core.Run{}, messages: map[core.MessageID]*messageRecord{}, jobs: map[toldJob]core.MessageID{},
		receipts: map[core.CommandID]core.Receipt{}, offsets: map[core.Session]uint64{}, handed: map[core.AgentID]bool{},
		failures: map[core.AgentID]uint32{}, leadReminders: map[core.RunID]int{}, handoffReminders: map[core.Session]int{},
		reports: map[core.RunID]uint32{}, finished: map[core.RunID]bool{}, stalled: map[core.RunID]bool{},
		limited: map[core.RunID]core.MessageID{}, books: map[core.RunID]*schedule{}, checkInBooks: newCheckInBooks(),
	}
}

// commit checks one transaction, in order: what an input may change, then each event, then what
// must have come of them taken together.
func (m *monitor) commit(body core.InputBody, events []core.Event, effects []core.Effect) error {
	m.refusedSteer = ""
	m.idled, m.over, m.noticed, m.resumed = map[core.AgentID]bool{}, map[core.AgentID]bool{}, map[core.AgentID]int{}, map[core.AgentID]bool{}
	if fact, ok := body.(core.SessionFact); ok {
		if outcome, ok := fact.Body.(core.SteerOutcome); ok && !outcome.Accepted {
			m.refusedSteer = outcome.Message
		}
		if err := m.fact(fact, events); err != nil {
			return err
		}
	}
	var graceTimer *core.ArmTimer
	if fired, ok := body.(core.TimerFired); ok && fired.Timer.Kind == core.TimerLimitGrace {
		pre := m.runs[fired.Timer.Run]
		if pre.Status.Live() && pre.LimitReached != "" && pre.LimitReachedAt.Add(core.LimitGrace).Equal(fired.Timer.FireAt) {
			graceTimer = &fired.Timer
		}
	}
	var launchTimer *core.Session
	if fired, ok := body.(core.TimerFired); ok && fired.Timer.Kind == core.TimerLaunchDeadline && len(events) > 0 {
		session := fired.Timer.Session
		if pre := m.agents[session.Agent]; pre.Status != core.AgentLaunching || pre.Session != session.N {
			return fmt.Errorf("the launch timer of %v ended %+v, which was not launching that session", session, pre)
		}
		launchTimer = &session
	}
	m.startEvery = 0
	if c, ok := body.(core.Command); ok {
		if start, ok := c.Body.(core.StartRun); ok {
			m.startEvery = start.SummaryEvery
		}
	}
	m.arms = map[core.RunID][]time.Time{}
	if err := m.summaryTimer(body, events); err != nil {
		return err
	}
	if err := m.checkIns(body, events, effects); err != nil {
		return err
	}
	for _, e := range events {
		if err := m.observe(e); err != nil {
			return err
		}
	}
	if err := m.summaryBooks(effects); err != nil {
		return err
	}
	if graceTimer != nil {
		if run := m.runs[graceTimer.Run]; run.Status.Live() {
			return fmt.Errorf("the grace after a limit passed, and run %s is still %s", run.ID, run.Status)
		}
	}
	if launchTimer != nil {
		// A lead that can resume goes straight into a new launch, so the failure is read from the
		// events the timer produced, not from where the agent stands afterwards.
		timedOut := false
		for _, e := range events {
			if a, ok := e.Body.(core.Agent); ok && a.ID == launchTimer.Agent && a.Session == launchTimer.N {
				timedOut = timedOut || (a.Status == core.AgentFailed && a.StartFailure == core.StartTimedOut)
			}
		}
		if !timedOut {
			return fmt.Errorf("the launch timer of %v ended no event failing it as timed out; agent now %+v", *launchTimer, m.agents[launchTimer.Agent])
		}
		m.covered.startsTimedOut++
	}
	return m.settled()
}

// fact holds a sidecar to its session: only the current one has a say, and each offset counts once.
func (m *monitor) fact(f core.SessionFact, events []core.Event) error {
	if len(events) == 0 {
		return nil
	}
	a := m.agents[f.Session.Agent]
	if a.Session != f.Session.N || a.Status.Over() {
		return fmt.Errorf("a fact of session %v changed state, and the agent is at session %d, %s", f.Session, a.Session, a.Status)
	}
	if f.Offset <= m.offsets[f.Session] {
		return fmt.Errorf("offset %d of session %v applied after offset %d", f.Offset, f.Session, m.offsets[f.Session])
	}
	m.offsets[f.Session] = f.Offset
	return nil
}

// settled holds what no single event shows: no subagent stops working unheard, and no message stays
// steered at a session that is gone.
func (m *monitor) settled() error {
	for agent := range m.idled {
		a := m.agents[agent]
		if a.Status.Resting() && a.Stop == core.StopNone && !m.owedMail(agent) {
			m.over[agent] = true
		}
	}
	for agent := range m.over {
		a := m.agents[agent]
		if a.Role == core.RoleLead || m.handed[agent] || m.runs[a.Run].Status != core.RunRunning || a.Status == core.AgentLaunching {
			continue
		}
		if m.noticed[agent] != 1 {
			return fmt.Errorf("agent %s stopped working (%s) with no handoff, and the lead heard %d times", agent, a.Status, m.noticed[agent])
		}
	}
	if err := m.stalls(); err != nil {
		return err
	}
	for agent := range m.resumed {
		for id, record := range m.messages {
			if record.to == agent && record.last == core.DeliverySteered {
				return fmt.Errorf("message %s stayed steered at a session of %s that is gone", id, agent)
			}
		}
	}
	return nil
}

// stalls holds each run's StalledSince to the monitor's own account of when a run stands still: it
// is running, no job is going, no message is owed to one of its agents, and every live agent rests,
// the lead included.
func (m *monitor) stalls() error {
	for id, run := range m.runs {
		want := run.Status == core.RunRunning && run.ActiveJobs == 0 && m.restingRun(id) && !m.mailInRun(id) && !m.finished[id]
		if got := !run.StalledSince.IsZero(); got != want {
			return fmt.Errorf("run %s has stalled since %v, but standing still is %t", id, run.StalledSince, want)
		}
		if want && !m.stalled[id] {
			m.stalled[id] = true
			m.covered.stalls++
		} else if !want {
			delete(m.stalled, id)
		}
	}
	return nil
}

// restingRun says every live agent of the run rests with no stop under way, and its lead is live.
func (m *monitor) restingRun(run core.RunID) bool {
	lead, ok := m.agents[m.runs[run].Lead]
	if !ok || !lead.Status.Resting() {
		return false
	}
	for _, a := range m.agents {
		if a.Run == run && !a.Status.Over() && (!a.Status.Resting() || a.Stop != core.StopNone) {
			return false
		}
	}
	return true
}

// mailInRun says a message addressed to an agent of the run is still on its way.
func (m *monitor) mailInRun(run core.RunID) bool {
	for _, record := range m.messages {
		if record.to != "" && !record.last.Final() && m.agents[record.to].Run == run {
			return true
		}
	}
	return false
}

func (m *monitor) owedMail(agent core.AgentID) bool {
	for _, record := range m.messages {
		if record.to == agent && (record.last == core.DeliveryQueued || record.last == core.DeliverySteered) {
			return true
		}
	}
	return false
}

func (m *monitor) observe(e core.Event) error {
	switch b := e.Body.(type) {
	case core.Run:
		return m.run(b)
	case core.Agent:
		return m.agent(b)
	case core.Turn:
		return m.turn(b)
	case core.Item:
		return m.item(b)
	case core.Message:
		return m.message(b)
	case core.Receipt:
		return m.receipt(b)
	case core.CheckIn:
		return nil // held to its rules by checkIns, before the events are folded in
	}
	return fmt.Errorf("unknown event %T", e.Body)
}

func (m *monitor) run(r core.Run) error {
	if _, known := m.runs[r.ID]; !known {
		if err := m.newSchedule(r); err != nil {
			return err
		}
	}
	if old, ok := m.runs[r.ID]; ok && r.ActiveJobs > old.ActiveJobs {
		delete(m.finished, r.ID)
	}
	defer m.runChanged(r)
	if limited := r.StopReason == core.StopTimeLimit || r.StopReason == core.StopCostLimit; limited {
		want := map[core.RunLimit]core.RunStopReason{core.LimitTime: core.StopTimeLimit, core.LimitCost: core.StopCostLimit}
		if want[r.LimitReached] != r.StopReason {
			return fmt.Errorf("run %s stopped for %q having reached limit %q", r.ID, r.StopReason, r.LimitReached)
		}
		if m.finished[r.ID] {
			m.covered.finishedStops++
		}
	}
	if old, ok := m.runs[r.ID]; ok && old.Status.Final() {
		// The one thing a finished run still learns is which working copies were kept.
		old.Kept, r.Kept = nil, nil
		if !reflect.DeepEqual(old, r) {
			return fmt.Errorf("run %s changed after it finished: %+v", r.ID, r)
		}
		m.runs[r.ID] = r
		return nil
	}
	switch {
	case r.Status == core.RunFailed && r.StopReason != core.StopLeadGone:
		return fmt.Errorf("run %s failed for %q", r.ID, r.StopReason)
	case r.Status == core.RunFailed:
		m.covered.runsFailed++
	case r.Status == core.RunStopping && r.StopReason == core.StopLeadGone:
		lead := m.agents[r.Lead]
		if lead.Capabilities.Resumes && lead.ResumeToken != "" && m.failures[lead.ID] <= core.MaxLeadResumes {
			return fmt.Errorf("run %s gave up on a lead with %d failures that could resume", r.ID, m.failures[lead.ID])
		}
		if lead.Capabilities.Resumes {
			m.covered.leadsOutOfResumes++
		}
	case r.Status == core.RunEnded && r.StopReason == core.StopTimeLimit:
		m.covered.deadlineStops++
	case r.Status == core.RunEnded && r.StopReason == core.StopCostLimit:
		m.covered.costStops++
	case r.Status == core.RunEnded:
		m.covered.operatorStops++
	}
	m.runs[r.ID] = r
	return nil
}

func (m *monitor) agent(a core.Agent) error {
	old, known := m.agents[a.ID]
	m.agents[a.ID] = a
	if known && a.Session != old.Session {
		m.covered.resumes++
		if a.Session != old.Session+1 || !old.Status.Over() {
			return fmt.Errorf("agent %s went from %+v to session %d", a.ID, old, a.Session)
		}
		if a.Role == core.RoleLead && m.failures[a.ID] > core.MaxLeadResumes {
			return fmt.Errorf("lead %s resumed after %d failures in a row", a.ID, m.failures[a.ID])
		}
		delete(m.handed, a.ID)
		m.resumed[a.ID] = true
	}
	switch {
	case !a.Status.Over() && a.StartFailure != "":
		return fmt.Errorf("agent %s is %s with start failure %q", a.ID, a.Status, a.StartFailure)
	case a.Status.Over() && !old.Status.Over() && old.Status == core.AgentLaunching && a.Status != core.AgentEnded:
		if a.StartFailure == "" {
			return fmt.Errorf("agent %s failed while launching with no start failure: %+v", a.ID, a)
		}
		m.covered.startsFailed++
	case a.Status.Over() && !old.Status.Over() && old.Status != core.AgentLaunching && a.StartFailure != "":
		return fmt.Errorf("agent %s was open and ended with start failure %q", a.ID, a.StartFailure)
	}
	if a.Status.Over() && !old.Status.Over() {
		m.over[a.ID] = true
		if old.Stop == core.StopNone {
			m.failures[a.ID]++
		}
	}
	if a.Status == core.AgentLost {
		m.covered.lost++
	}
	if a.Stop != core.StopNone && old.Stop == core.StopNone {
		m.covered.stops++
	}
	if a.Role == core.RoleLead {
		return nil
	}
	if !known {
		delete(m.finished, a.Run)
		if limit := m.runs[a.Run].LimitReached; limit != "" {
			return fmt.Errorf("agent %s was started in run %s after it reached its %s limit", a.ID, a.Run, limit)
		}
	}
	var live uint32
	for _, other := range m.agents {
		if other.Run == a.Run && other.Role != core.RoleLead && !other.Status.Over() {
			live++
		}
	}
	if limit := m.runs[a.Run].FanoutLimit; live > limit {
		return fmt.Errorf("run %s has %d live subagents over its limit of %d", a.Run, live, limit)
	}
	return nil
}

func (m *monitor) turn(t core.Turn) error {
	running, busy := m.running[t.Agent]
	record := m.messages[t.Message]
	if record == nil {
		return fmt.Errorf("turn %s is for unknown message %s", t.ID, t.Message)
	}
	old, known := m.turns[t.ID]
	m.turns[t.ID] = t
	switch {
	case !known:
		if busy {
			return fmt.Errorf("agent %s started turn %s while turn %s runs", t.Agent, t.ID, running)
		}
		if agent := m.agents[t.Agent]; agent.Status == core.AgentLaunching || agent.Status.Over() {
			return fmt.Errorf("turn %s started on agent %s, which is %s: a session works only once it has opened", t.ID, t.Agent, agent.Status)
		}
		if t.Status != core.TurnRunning || t.Ordinal != m.ordinals[t.Agent]+1 {
			return fmt.Errorf("turn %s starts as %+v after ordinal %d", t.ID, t, m.ordinals[t.Agent])
		}
		m.ordinals[t.Agent] = t.Ordinal
		m.running[t.Agent] = t.ID
		record.turns++
		record.running++
		if record.running > 1 {
			return fmt.Errorf("message %s started two running turns", t.Message)
		}
		if record.finished > 0 {
			return fmt.Errorf("message %s started a turn after one completed it", t.Message)
		}
		if _, ok := record.body.(core.Handoff); ok {
			m.covered.handoffTurns++
		}
		if record.turns > 1 {
			m.covered.retriedDeliveries++
		}
		delete(m.handed, t.Agent)
		delete(m.idled, t.Agent)
	case old.Status != core.TurnRunning:
		return fmt.Errorf("turn %s changed after it ended: %+v", t.ID, t)
	case t.Status != core.TurnRunning:
		delete(m.running, t.Agent)
		record.running--
		if t.Status == core.TurnCompleted {
			record.finished++
			m.failures[t.Agent] = 0
		}
		if len(m.open[t.ID]) > 0 {
			return fmt.Errorf("turn %s ended with items open: %v", t.ID, m.open[t.ID])
		}
		delete(m.open, t.ID)
		m.idled[t.Agent] = true
	}
	return nil
}

func (m *monitor) item(i core.Item) error {
	if m.running[i.Agent] != i.Turn {
		return fmt.Errorf("item %s of turn %s, which is not running", i.ID, i.Turn)
	}
	if i.Ordinal > m.items[i.Agent]+1 {
		return fmt.Errorf("item %s skips to ordinal %d from %d", i.ID, i.Ordinal, m.items[i.Agent])
	}
	m.items[i.Agent] = max(m.items[i.Agent], i.Ordinal)
	if m.open[i.Turn] == nil {
		m.open[i.Turn] = map[core.ItemID]bool{}
	}
	if i.Status == core.ItemRunning {
		m.open[i.Turn][i.ID] = true
	} else {
		delete(m.open[i.Turn], i.ID)
	}
	return nil
}

func (m *monitor) message(msg core.Message) error {
	record := m.messages[msg.ID]
	if record == nil {
		record = &messageRecord{run: msg.Run, to: msg.To, body: msg.Body}
		m.messages[msg.ID] = record
		m.workMoved(msg)
		switch b := msg.Body.(type) {
		case core.ReportDue:
			if err := m.reportDue(msg, b); err != nil {
				return err
			}
		case core.Handoff:
			m.covered.handoffs++
			m.handed[msg.From] = true
		case core.SubagentSettled:
			m.noticed[b.Agent]++
		case core.LeadIdleReminder:
			if err := m.leadReminder(msg); err != nil {
				return err
			}
		case core.RunLimitReached:
			m.covered.limitMessages++
			run := m.runs[msg.Run]
			if first, told := m.limited[msg.Run]; told {
				return fmt.Errorf("run %s told to finish twice: %s and %s", msg.Run, first, msg.ID)
			}
			m.limited[msg.Run] = msg.ID
			if msg.To != run.Lead || b.Limit != run.LimitReached || run.LimitReached == "" || run.FinalReported {
				return fmt.Errorf("limit message %+v to %s in run %+v", b, msg.To, run)
			}
		case core.HandoffReminder:
			if err := m.handoffReminder(msg); err != nil {
				return err
			}
		case core.Report:
			m.covered.reports++
			if b.Kind != core.ReportProgress && b.Kind != core.ReportFinal {
				return fmt.Errorf("report %s has kind %q", msg.ID, b.Kind)
			}
			m.reports[msg.Run]++
			m.finished[msg.Run] = b.Kind == core.ReportFinal
			m.reported(msg)
			if b.Sequence != m.reports[msg.Run] || m.runs[msg.Run].ReportsPosted != b.Sequence {
				return fmt.Errorf("report %s is number %d, the run counts %d, want %d", msg.ID, b.Sequence, m.runs[msg.Run].ReportsPosted, m.reports[msg.Run])
			}
		case core.JobFinished:
			m.covered.jobs++
			key := toldJob{run: msg.Run, job: b.Job}
			if first, told := m.jobs[key]; told {
				return fmt.Errorf("job %s of run %s told twice: %s and %s", b.Job, msg.Run, first, msg.ID)
			}
			m.jobs[key] = msg.ID
		}
	}
	if record.last.Final() {
		return fmt.Errorf("message %s changed after it settled: %+v", msg.ID, msg)
	}
	if msg.Delivery == core.DeliverySteered && record.last != core.DeliverySteered {
		m.covered.steers++
		if a := m.agents[msg.To]; a.Status != core.AgentActive || a.Stop != core.StopNone || !a.Capabilities.Steers {
			return fmt.Errorf("message %s steered at %+v", msg.ID, a)
		}
	}
	if msg.Delivery == core.DeliveryConsumed && msg.ID == m.refusedSteer {
		return fmt.Errorf("message %s consumed by a steer its sidecar refused", msg.ID)
	}
	if msg.Delivery.Final() {
		record.settled++
	}
	if msg.Delivery == core.DeliveryRejected {
		m.covered.rejected++
		if msg.Rejection == "" {
			return fmt.Errorf("message %s rejected with no reason", msg.ID)
		}
	}
	if msg.Delivery == core.DeliveryNewTurn && msg.Attempts > core.MaxDeliveryAttempts {
		return fmt.Errorf("message %s took %d attempts", msg.ID, msg.Attempts)
	}
	record.previous, record.last = record.last, msg.Delivery
	return nil
}

// leadReminder holds a reminder to the lead to its rules, at the moment it is written, before the
// turn it starts changes anything: the run is running and has stood still for the whole time it
// was meant to, nothing is going on in it, and the lead has not had more than the limit.
func (m *monitor) leadReminder(msg core.Message) error {
	run := m.runs[msg.Run]
	m.covered.leadReminders++
	m.leadReminders[msg.Run]++
	switch {
	case run.Status != core.RunRunning:
		return fmt.Errorf("lead reminder %s in a run that is %s", msg.ID, run.Status)
	case m.leadReminders[msg.Run] > core.MaxLeadReminders || run.LeadReminders != uint32(m.leadReminders[msg.Run]):
		return fmt.Errorf("reminder %s is number %d of run %s, which counts %d", msg.ID, m.leadReminders[msg.Run], run.ID, run.LeadReminders)
	case run.ActiveJobs != 0 || !m.restingRun(run.ID) || !m.onlyThis(run.ID, msg.ID):
		return fmt.Errorf("lead reminder %s while the run was not standing still: %+v", msg.ID, run)
	case run.StalledSince.IsZero() || msg.At.Sub(run.StalledSince) < core.LeadIdleAfter:
		return fmt.Errorf("lead reminder %s at %v, but the run has stalled since %v", msg.ID, msg.At, run.StalledSince)
	}
	return nil
}

// onlyThis says the one message on its way to the run's agents is this one.
func (m *monitor) onlyThis(run core.RunID, id core.MessageID) bool {
	for mid, record := range m.messages {
		if mid != id && record.to != "" && !record.last.Final() && m.agents[record.to].Run == run {
			return false
		}
	}
	return true
}

// handoffReminder holds the reminder to hand off to once per session, and to an agent that is not
// the lead and has not handed off.
func (m *monitor) handoffReminder(msg core.Message) error {
	a := m.agents[msg.To]
	session := core.Session{Agent: a.ID, N: a.Session}
	m.covered.handoffReminders++
	m.handoffReminders[session]++
	switch {
	case a.Role == core.RoleLead || m.handed[a.ID]:
		return fmt.Errorf("handoff reminder %s to %s, a lead or an agent that handed off", msg.ID, a.ID)
	case m.handoffReminders[session] > 1:
		return fmt.Errorf("session %v was reminded twice", session)
	}
	return nil
}

func (m *monitor) receipt(r core.Receipt) error {
	if old, ok := m.receipts[r.Command]; ok {
		return fmt.Errorf("command %s got a second receipt: %+v, then %+v", r.Command, old, r)
	}
	m.receipts[r.Command] = r
	if r.Refusal == core.RefuseFanoutLimit {
		m.covered.refusedSpawns++
	}
	if r.Refusal == core.RefuseTimeLimitReached || r.Refusal == core.RefuseCostLimitReached {
		m.covered.limitRefusals++
		want := map[core.RunLimit]core.Refusal{core.LimitTime: core.RefuseTimeLimitReached, core.LimitCost: core.RefuseCostLimitReached}
		if run := m.runs[r.Run]; want[run.LimitReached] != r.Refusal {
			return fmt.Errorf("spawn refused as %s in run %+v", r.Refusal, run)
		}
	}
	return nil
}

// final holds what must be true when everything has settled. Every message reached the end once,
// and one consumed by an agent was taken exactly once: by the turn that completed it, or by the
// running turn it steered, never both. That is what makes a wake happen exactly once.
func (m *monitor) final() error {
	for id, record := range m.messages {
		if record.settled != 1 {
			return fmt.Errorf("message %s settled %d times, last as %s", id, record.settled, record.last)
		}
		wanted := 0
		if record.last == core.DeliveryConsumed && record.to != "" && record.previous != core.DeliverySteered {
			wanted = 1
		}
		if record.finished != wanted {
			return fmt.Errorf("message %s completed %d turns, want %d: it went from %s to %s",
				id, record.finished, wanted, record.previous, record.last)
		}
		if record.running != 0 {
			return fmt.Errorf("message %s ended as %s with a turn running", id, record.last)
		}
	}
	for agent, turn := range m.running {
		return fmt.Errorf("agent %s still runs turn %s", agent, turn)
	}
	if err := m.checkInsFinal(); err != nil {
		return err
	}
	for id, a := range m.agents {
		if a.Status == core.AgentLaunching {
			return fmt.Errorf("agent %s is still launching: a launch ends opened or failed", id)
		}
	}
	return nil
}
