package core

import (
	"slices"
	"time"
)

// This file holds UpdateRun: the operator changing the limits, the fan-out or the lead of a run that is
// live. An update is all or none, and every setting it changes is one entry of one RunSettingsUpdated;
// what it leaves as it was, and an update that changes nothing, record nothing but the receipt.
//
// The limits are read from the run when they are used, so a change takes effect at the next reading and
// needs no migration. A limit already passed is reached as any other is (reachLimit): the lead is told
// to finish and the run stops when it has, or after LimitGrace. A run that has reached a limit refuses a
// change to that limit, since what it is stopping for is not to be undone.
//
// Lowering the fan-out stops no agent. A spawn or a resume is admitted only while the live subagents
// are fewer than the limit, so a run that is over its new limit takes no more until it is under it.
//
// The lead is not changed here: the update records which lead is wanted (PendingLead), and handover.go
// makes it so.

// updateRun applies an update to a live run.
func (t *tx) updateRun(b UpdateRun, r *Receipt) RunID {
	run, ok := t.s.runs[b.Run]
	switch {
	case !ok:
		r.Refusal = RefuseRunNotFound
		return ""
	case !run.Status.Live():
		r.Run, r.Refusal = run.ID, RefuseRunNotRunning
		return ""
	}
	r.Run = run.ID
	if refusal := run.limitRefusal(b); refusal != "" {
		r.Refusal = refusal
		return ""
	}
	var lead AgentSpec
	if b.Lead != nil {
		model, ok := t.resolveChoice(r, b.Lead.Harness, b.Lead.Model)
		if !ok {
			return ""
		}
		lead = *b.Lead
		lead.Model = model
	}

	was := run
	var changes []SettingChange
	if c := b.CostLimit; c != nil {
		if to := c.value(); to != run.MaxCostUSD {
			changes = append(changes, SettingChange{CostLimit: &CostLimitChange{From: run.MaxCostUSD, To: to}})
			run.MaxCostUSD = to
		}
	}
	if c := b.TimeLimit; c != nil {
		if to := c.value(); to != run.totalTime() {
			changes = append(changes, SettingChange{TimeLimit: &TimeLimitChange{From: run.totalTime(), To: to}})
			run.Deadline = time.Time{}
			if to > 0 {
				run.Deadline = run.limitClock().Add(to)
			}
		}
	}
	if f := b.Fanout; f != nil && *f != run.FanoutLimit {
		changes = append(changes, SettingChange{Fanout: &FanoutChange{From: run.FanoutLimit, To: *f}})
		run.FanoutLimit = *f
	}
	if b.Lead != nil {
		if change, ok := t.wantLead(&run, lead); ok {
			changes = append(changes, change)
		}
	}
	if len(changes) == 0 {
		return run.ID
	}

	t.emit(run.ID, run)
	t.emit(run.ID, RunSettingsUpdated{Run: run.ID, Changes: changes})
	if !run.Deadline.Equal(was.Deadline) && run.Deadline.After(t.in.At) {
		// An earlier timer for the old deadline is harmless: it is answered only at the deadline the run holds.
		t.effect(ArmTimer{Kind: TimerRunDeadline, Run: run.ID, FireAt: run.Deadline})
	}
	if run.MaxCostUSD != was.MaxCostUSD {
		t.costLimit(run.ID)
	}
	if !run.Deadline.Equal(was.Deadline) && !run.Deadline.IsZero() && !t.in.At.Before(run.Deadline) {
		t.reachLimit(run.ID, LimitTime)
	}
	return run.ID
}

// limitRefusal is why an update may not change the limits it names, or empty: a limit the run has
// reached stays as it is, and the limits of a run in setup are those of the optimize phase, which its
// completion sets.
func (r Run) limitRefusal(b UpdateRun) Refusal {
	switch {
	case b.CostLimit == nil && b.TimeLimit == nil:
		return ""
	case !r.Phase.Optimizing():
		return RefuseNotPermitted
	case b.CostLimit != nil && r.LimitReached == LimitCost:
		return RefuseCostLimitReached
	case b.TimeLimit != nil && r.LimitReached == LimitTime:
		return RefuseTimeLimitReached
	}
	return ""
}

// value is the limit an update sets, zero for none.
func (c LimitChange[T]) value() T {
	var none T
	if c.Remove {
		return none
	}
	return c.Set
}

// limitClock is the instant a run's time is counted from: its start, or for a run that had a setup
// the moment it began to optimize.
func (r Run) limitClock() time.Time {
	if !r.OptimizeStartedAt.IsZero() {
		return r.OptimizeStartedAt
	}
	return r.CreatedAt
}

// totalTime is how long the run may take in all, zero for no limit.
func (r Run) totalTime() time.Duration {
	if r.Deadline.IsZero() {
		return 0
	}
	return r.Deadline.Sub(r.limitClock())
}

// wantLead records that the lead is to run on want, on a copy of the run, and says what changed. Asking
// for the spec the lead has already withdraws a change that waited, and asking for the one that waits
// changes nothing. A change whose session is already closing cannot be withdrawn, since that session is
// going: asking for another spec, the lead's own included, sets what the fresh session opens on. The
// lead's harness and model join the run's allowance if they were not in it, as a start's lead is, so
// that the lead is never outside it.
func (t *tx) wantLead(run *Run, want AgentSpec) (SettingChange, bool) {
	lead := t.s.agents[run.Lead]
	current, closing := lead.Spec, lead.Stop != StopNone
	pending := run.PendingLead
	switch {
	case want == current && pending == nil:
		return SettingChange{}, false
	case want == current && !closing:
		run.PendingLead = nil
		return SettingChange{Lead: &LeadChange{From: *pending, To: current, State: LeadChangeApplied}}, true
	case pending != nil && *pending == want:
		return SettingChange{}, false
	}
	run.PendingLead = &want
	if choice := (AgentChoice{Harness: want.Harness, Model: want.Model}); !slices.Contains(run.Allowed, choice) {
		run.Allowed = append(slices.Clone(run.Allowed), choice)
	}
	return SettingChange{Lead: &LeadChange{From: current, To: want, State: LeadChangePending}}, true
}
