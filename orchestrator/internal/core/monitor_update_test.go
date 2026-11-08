package core_test

import (
	"fmt"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// This file holds the rules the simulation's monitor keeps for updates and the change of a lead. Like
// the rest of the monitor it works from the committed log alone, keeping its own books.

// updateCoverage counts what the updates of a simulation reached, so that a rule cannot pass by never
// being tested.
type updateCoverage struct {
	cost, span, fanout, loweredBelowLive, leadAsked, leadSwitched, leadWithdrawn int
	refused, noops, reachedByUpdate, queuedWhileClosing, behindMail, afterAFall  int
	crashedPending                                                               int
}

func (c *updateCoverage) add(o updateCoverage) {
	c.cost += o.cost
	c.span += o.span
	c.fanout += o.fanout
	c.loweredBelowLive += o.loweredBelowLive
	c.leadAsked += o.leadAsked
	c.leadSwitched += o.leadSwitched
	c.leadWithdrawn += o.leadWithdrawn
	c.refused += o.refused
	c.noops += o.noops
	c.reachedByUpdate += o.reachedByUpdate
	c.queuedWhileClosing += o.queuedWhileClosing
	c.behindMail += o.behindMail
	c.afterAFall += o.afterAFall
	c.crashedPending += o.crashedPending
}

func (c updateCoverage) require(t *testing.T) {
	t.Helper()
	for name, n := range map[string]int{
		"cost limits changed by an update": c.cost, "time limits changed by an update": c.span, "fan-outs changed by an update": c.fanout,
		"fan-outs lowered below the live subagents": c.loweredBelowLive, "changes of the lead asked for": c.leadAsked,
		"leads replaced": c.leadSwitched, "changes of the lead withdrawn": c.leadWithdrawn, "updates refused": c.refused,
		"updates that changed nothing": c.noops, "limits reached by an update": c.reachedByUpdate,
		"messages queued for a lead whose session was closing": c.queuedWhileClosing,
		"handovers that waited ahead of other mail":            c.behindMail, "leads replaced after they fell": c.afterAFall,
		"crashes with a change of the lead waiting": c.crashedPending,
	} {
		if n == 0 {
			t.Errorf("the simulation never reached: %s", name)
		}
	}
}

// updateBooks is what the monitor keeps to hold updates to their rules.
type updateBooks struct {
	// handovers and switches count, for each run, the handovers posted and the leads replaced: one for
	// one.
	handovers, switches map[core.RunID]int
	// awaiting holds the leads whose first turn in their new session must be for the handover.
	awaiting map[core.AgentID]bool
	// report is the newest report about the whole run, as the lead posted it.
	report map[core.RunID]core.Report
}

func newUpdateBooks() *updateBooks {
	return &updateBooks{
		handovers: map[core.RunID]int{}, switches: map[core.RunID]int{}, awaiting: map[core.AgentID]bool{},
		report: map[core.RunID]core.Report{},
	}
}

// beforeCommit is what the monitor knew of each run before a commit, to set against what the commit
// left.
type beforeCommit struct {
	runs  map[core.RunID]core.Run
	leads map[core.RunID]core.AgentSpec
	// sessions is the session each run's lead was in.
	sessions map[core.RunID]uint32
}

func (m *monitor) beforeCommit() beforeCommit {
	b := beforeCommit{
		runs: make(map[core.RunID]core.Run, len(m.runs)), leads: make(map[core.RunID]core.AgentSpec, len(m.runs)),
		sessions: make(map[core.RunID]uint32, len(m.runs)),
	}
	for id, run := range m.runs {
		b.runs[id] = run
		b.leads[id] = m.agents[run.Lead].Spec
		b.sessions[id] = m.agents[run.Lead].Session
	}
	return b
}

// span is how long a run may take in all, zero for no limit.
func span(r core.Run) time.Duration {
	if r.Deadline.IsZero() {
		return 0
	}
	return r.Deadline.Sub(r.CreatedAt)
}

// updates holds the settings events of one commit to what the commit changed: each setting an update
// changed is one entry of one RunSettingsUpdated, and a setting nothing changed has none; a refused or
// empty update writes none; a setting changes only by an update, but for the lead, which an applied
// change replaces in a fresh session, with the handover for it.
func (m *monitor) updates(body core.InputBody, events []core.Event, before beforeCommit) error {
	var command *core.UpdateRun
	var receipt core.Receipt
	if c, ok := body.(core.Command); ok {
		if u, ok := c.Body.(core.UpdateRun); ok {
			command = &u
			for _, e := range events {
				if r, ok := e.Body.(core.Receipt); ok && r.Command == c.ID {
					receipt = r
				}
			}
		}
	}
	entries := map[core.RunID][]core.SettingChange{}
	written := map[core.RunID]int{}
	// asked is the lead a run was told to wait for at some point in the commit, which an update that also
	// stops the run (a limit it passes) records and then drops with the run's stop.
	asked := map[core.RunID]*core.AgentSpec{}
	fresh, cleared := map[core.RunID]bool{}, map[core.RunID]bool{}
	for _, e := range events {
		switch b := e.Body.(type) {
		case core.Agent:
			if command != nil && b.Role != core.RoleLead && (b.Stop != core.StopNone || b.Status.Over()) && m.runs[b.Run].Status.Live() {
				// An update stops no subagent of a run it leaves live: lowering the fanout drains.
				return fmt.Errorf("an update of run %s stopped agent %s: %+v", b.Run, b.ID, b)
			}
			// A lead's new session that continues no conversation is the one that replaces a lead: a resume
			// always has a token, and a run that was not there before has no earlier session.
			if before.sessions[b.Run] > 0 && b.Role == core.RoleLead && b.Session > before.sessions[b.Run] && b.ResumeToken == "" {
				fresh[b.Run] = true
			}
		case core.Run:
			if b.PendingLead != nil {
				asked[b.ID] = b.PendingLead
			} else if old, ok := before.runs[b.ID]; ok && old.PendingLead != nil && b.Status.Live() {
				cleared[b.ID] = true
			}
		case core.RunSettingsUpdated:
			if len(b.Changes) == 0 {
				return fmt.Errorf("run %s recorded an update that changed nothing", b.Run)
			}
			for _, c := range b.Changes {
				if kinds := b2i(c.CostLimit != nil) + b2i(c.TimeLimit != nil) + b2i(c.Fanout != nil) + b2i(c.Lead != nil); kinds != 1 {
					return fmt.Errorf("run %s recorded a setting change of %d kinds: %+v", b.Run, kinds, c)
				}
			}
			written[b.Run]++
			entries[b.Run] = append(entries[b.Run], b.Changes...)
		case core.Message:
			if r, ok := b.Body.(core.Report); ok && r.Scope != core.ReportScopeWindow {
				m.updated.report[b.Run] = r
			}
		}
	}
	if command != nil && receipt.Refusal != "" {
		m.covered.updates.refused++
		if len(events) != 1 || len(entries) != 0 {
			return fmt.Errorf("an update refused as %s wrote more than its receipt: %d events, %v", receipt.Refusal, len(events), entries)
		}
		return nil
	}
	for id, now := range m.runs {
		old, existed := before.runs[id]
		if !existed {
			if len(entries[id]) != 0 {
				return fmt.Errorf("run %s was made with settings changes %v", id, entries[id])
			}
			continue
		}
		if err := m.updatedRun(body, command, old, now, asked[id], fresh[id], cleared[id], before.leads[id], entries[id], written[id]); err != nil {
			return err
		}
	}
	return m.oneLead()
}

func b2i(b bool) int {
	if b {
		return 1
	}
	return 0
}

func (m *monitor) updatedRun(body core.InputBody, command *core.UpdateRun, old, now core.Run, wanted *core.AgentSpec, switched, cleared bool, was core.AgentSpec, got []core.SettingChange, written int) error {
	id := now.ID
	named := command != nil && command.Run == id
	var cost, limit, fanout, pending, applied []core.SettingChange
	for _, c := range got {
		switch {
		case c.CostLimit != nil:
			cost = append(cost, c)
		case c.TimeLimit != nil:
			limit = append(limit, c)
		case c.Fanout != nil:
			fanout = append(fanout, c)
		case c.Lead.State == core.LeadChangePending:
			pending = append(pending, c)
		default:
			applied = append(applied, c)
		}
	}
	lead := m.agents[now.Lead].Spec
	requested := wanted != nil && (old.PendingLead == nil || *old.PendingLead != *wanted)
	// A run that stops drops the change it waited for, which is no withdrawal: the update recorded one if it
	// cleared the wait while the run was still live.
	withdrawn := named && old.PendingLead != nil && cleared && !switched
	costChanged, spanChanged, fanoutChanged := old.MaxCostUSD != now.MaxCostUSD, span(old) != span(now), old.FanoutLimit != now.FanoutLimit

	switch {
	case written > 1:
		return fmt.Errorf("run %s wrote %d settings events in one commit", id, written)
	case !named && (costChanged || spanChanged || fanoutChanged || requested):
		return fmt.Errorf("run %s changed a setting with no update: %+v became %+v", id, old, now)
	case len(cost) != b2i(costChanged) || len(limit) != b2i(spanChanged) || len(fanout) != b2i(fanoutChanged):
		return fmt.Errorf("run %s changed cost %t, time %t, fan-out %t and recorded %d, %d and %d entries", id, costChanged, spanChanged, fanoutChanged, len(cost), len(limit), len(fanout))
	case len(pending) != b2i(requested) || len(applied) != b2i(switched || withdrawn):
		return fmt.Errorf("run %s asked %t, switched %t, withdrew %t its lead and recorded %d and %d entries", id, requested, switched, withdrawn, len(pending), len(applied))
	}
	if costChanged && *cost[0].CostLimit != (core.CostLimitChange{From: old.MaxCostUSD, To: now.MaxCostUSD}) {
		return fmt.Errorf("run %s recorded %+v for a cost limit that went from %v to %v", id, *cost[0].CostLimit, old.MaxCostUSD, now.MaxCostUSD)
	}
	if spanChanged && *limit[0].TimeLimit != (core.TimeLimitChange{From: span(old), To: span(now)}) {
		return fmt.Errorf("run %s recorded %+v for a time limit that went from %v to %v", id, *limit[0].TimeLimit, span(old), span(now))
	}
	if fanoutChanged && *fanout[0].Fanout != (core.FanoutChange{From: old.FanoutLimit, To: now.FanoutLimit}) {
		return fmt.Errorf("run %s recorded %+v for a fan-out that went from %d to %d", id, *fanout[0].Fanout, old.FanoutLimit, now.FanoutLimit)
	}
	if requested && *pending[0].Lead != (core.LeadChange{From: was, To: *wanted, State: core.LeadChangePending}) {
		return fmt.Errorf("run %s recorded %+v for a lead asked for: it runs on %+v and waits for %+v", id, *pending[0].Lead, was, *wanted)
	}
	if switched {
		if old.PendingLead == nil || *old.PendingLead != lead || now.PendingLead != nil {
			return fmt.Errorf("run %s switched its lead to %+v with the change waiting as %v and then %v", id, lead, old.PendingLead, now.PendingLead)
		}
		if *applied[0].Lead != (core.LeadChange{From: was, To: lead, State: core.LeadChangeApplied}) {
			return fmt.Errorf("run %s recorded %+v for a lead switched from %+v to %+v", id, *applied[0].Lead, was, lead)
		}
		m.updated.switches[id]++
		m.switchCoverage(body, now)
	}
	if withdrawn {
		if *applied[0].Lead != (core.LeadChange{From: *old.PendingLead, To: lead, State: core.LeadChangeApplied}) {
			return fmt.Errorf("run %s recorded %+v for a change of the lead withdrawn", id, *applied[0].Lead)
		}
		m.covered.updates.leadWithdrawn++
	}
	if named {
		m.updateCoverage(old, now, requested, len(got) == 0)
	}
	return nil
}

func (m *monitor) updateCoverage(old, now core.Run, requested, nothing bool) {
	c := &m.covered.updates
	c.cost += b2i(old.MaxCostUSD != now.MaxCostUSD)
	c.span += b2i(span(old) != span(now))
	c.fanout += b2i(old.FanoutLimit != now.FanoutLimit)
	c.leadAsked += b2i(requested)
	c.noops += b2i(nothing)
	c.reachedByUpdate += b2i(old.LimitReached == "" && now.LimitReached != "")
	if old.FanoutLimit != now.FanoutLimit {
		var live uint32
		for _, a := range m.agents {
			if a.Run == now.ID && a.Role != core.RoleLead && !a.Status.Over() {
				live++
			}
		}
		c.loweredBelowLive += b2i(now.FanoutLimit < live)
	}
}

// switchCoverage counts how a lead came to be replaced: after it fell, and with mail waiting.
func (m *monitor) switchCoverage(body core.InputBody, run core.Run) {
	c := &m.covered.updates
	c.leadSwitched++
	switch b := body.(type) {
	case core.SessionLost:
		c.afterAFall++
	case core.SessionFact:
		if _, failed := b.Body.(core.SessionFailed); failed {
			c.afterAFall++
		}
	case core.TimerFired:
		if b.Timer.Kind == core.TimerStopGrace || b.Timer.Kind == core.TimerLaunchDeadline {
			c.afterAFall++
		}
	}
	for _, record := range m.messages {
		if _, handover := record.body.(core.LeadHandover); !handover && record.to == run.Lead && record.last == core.DeliveryQueued {
			c.behindMail++
			return
		}
	}
}

// oneLead holds each run to one lead with a live session, the agent the run names.
func (m *monitor) oneLead() error {
	leads := map[core.RunID]core.AgentID{}
	for _, a := range m.agents {
		if a.Role != core.RoleLead || a.Status.Over() {
			continue
		}
		if other, twice := leads[a.Run]; twice {
			return fmt.Errorf("run %s has two live leads: %s and %s", a.Run, other, a.ID)
		}
		if m.runs[a.Run].Lead != a.ID {
			return fmt.Errorf("run %s names %s as its lead, and %s is the live one", a.Run, m.runs[a.Run].Lead, a.ID)
		}
		leads[a.Run] = a.ID
	}
	return nil
}

// handoverPosted holds the message that opens a replacement lead to what the run was when it was written:
// it is for the run's lead, whose new session is launching on another spec, and says what the run says.
func (m *monitor) handoverPosted(msg core.Message, h core.LeadHandover) error {
	run, lead := m.runs[msg.Run], m.agents[msg.To]
	if msg.To != run.Lead || !run.Status.Live() || lead.Status != core.AgentLaunching || lead.ResumeToken != "" {
		return fmt.Errorf("handover %s to %+v, the lead of %+v, which replaces %+v", msg.ID, lead, run, h.Previous)
	}
	var agents []core.HandoverAgent
	var ended uint32
	for _, a := range m.agents {
		switch {
		case a.Run != run.ID || a.Role == core.RoleLead:
		case a.Status.Over():
			ended++
		default:
			agents = append(agents, core.HandoverAgent{ID: a.ID, Role: a.Role, Status: a.Status, PlanNumber: a.PlanNumber})
		}
	}
	slices.SortFunc(agents, func(x, y core.HandoverAgent) int {
		return compareIDs(string(x.ID), string(y.ID))
	})
	wantSpent := run.Usage.CostUSD
	if !slices.Equal(agents, h.Agents) || ended != h.AgentsEnded ||
		h.Task != run.Task || h.FanoutLimit != run.FanoutLimit || h.ReportsPosted != run.ReportsPosted || h.LimitReached != run.LimitReached ||
		h.ActiveJobs != run.ActiveJobs || h.MaxCost != run.MaxCostUSD ||
		h.Elapsed != msg.At.Sub(run.CreatedAt).Truncate(time.Second) || (h.Spent == nil) != (wantSpent == nil) || (wantSpent != nil && *h.Spent != *wantSpent) {
		return fmt.Errorf("handover %s says %+v, but the run is %+v with agents %v", msg.ID, h, run, agents)
	}
	if want, ok := m.updated.report[run.ID]; ok != (h.LatestReport != nil) || (ok && want != *h.LatestReport) {
		return fmt.Errorf("handover %s carries report %v, the run's latest is %+v (%t)", msg.ID, h.LatestReport, want, ok)
	}
	if total := span(run); (total == 0) != (h.MaxTime == 0) || (total > 0 && h.MaxTime != max(time.Second, total.Truncate(time.Second))) {
		return fmt.Errorf("handover %s says the run may take %v, which is %v", msg.ID, h.MaxTime, total)
	}
	m.updated.handovers[run.ID]++
	m.updated.awaiting[lead.ID] = true
	return nil
}

func compareIDs(a, b string) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	}
	return 0
}

// mailToLead holds mail for the lead of a live run to be kept while the lead is replaced: a lead of a
// live run stops only to make way for the next, so no message is turned away for its stopping.
func (m *monitor) mailToLead(msg core.Message, fresh bool) error {
	a, run := m.agents[msg.To], m.runs[msg.Run]
	if a.Role != core.RoleLead || !run.Status.Live() {
		return nil
	}
	if fresh && msg.Delivery == core.DeliveryQueued && a.Stop != core.StopNone {
		m.covered.updates.queuedWhileClosing++
	}
	if msg.Delivery == core.DeliveryRejected && msg.Rejection == core.RejectAgentStopping {
		return fmt.Errorf("message %s to the lead of a live run was turned away for its stopping: %+v", msg.ID, a)
	}
	return nil
}

// firstTurnAfterSwitch holds the first turn of a replacement lead to be for its handover.
func (m *monitor) firstTurnAfterSwitch(t core.Turn, record *messageRecord) error {
	if !m.updated.awaiting[t.Agent] {
		return nil
	}
	delete(m.updated.awaiting, t.Agent)
	if _, ok := record.body.(core.LeadHandover); !ok {
		return fmt.Errorf("the first turn of the replacement lead %s is for %T, not its handover", t.Agent, record.body)
	}
	return nil
}

// updatesFinal holds that every lead replaced was handed the run, once.
func (m *monitor) updatesFinal() error {
	for run, n := range m.updated.switches {
		if m.updated.handovers[run] != n {
			return fmt.Errorf("run %s replaced its lead %d times and posted %d handovers", run, n, m.updated.handovers[run])
		}
	}
	return nil
}
