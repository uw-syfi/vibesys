package core_test

import (
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// What an update does to a run, one setting at a time: the scenarios the simulation draws at random
// (see simulation_update_test.go) stated by hand.

// update sends the operator's UpdateRun and returns its receipt.
func (w *script) update(run core.RunID, u core.UpdateRun) core.Receipt {
	w.t.Helper()
	u.Run = run
	return w.command(operator, u)
}

// changes are the settings the last step recorded as changed, in order, from every RunSettingsUpdated
// it wrote.
func (w *script) changes() []core.SettingChange {
	var out []core.SettingChange
	for _, e := range w.events {
		if u, ok := e.Body.(core.RunSettingsUpdated); ok {
			out = append(out, u.Changes...)
		}
	}
	return out
}

// settingEvents counts the RunSettingsUpdated events the last step wrote.
func (w *script) settingEvents() int {
	n := 0
	for _, e := range w.events {
		if _, ok := e.Body.(core.RunSettingsUpdated); ok {
			n++
		}
	}
	return n
}

// send has the operator send text to an agent.
func (w *script) send(to core.AgentID, text string) core.Receipt {
	w.t.Helper()
	return w.command(operator, core.SendMessage{Agent: to, Text: text})
}

func setCost(usd float64) *core.LimitChange[float64] { return &core.LimitChange[float64]{Set: usd} }
func setTime(d time.Duration) *core.LimitChange[time.Duration] {
	return &core.LimitChange[time.Duration]{Set: d}
}
func fanout(n uint32) *uint32 { return &n }

// leadOn is a lead spec on another tool.
var leadOn = core.AgentSpec{Harness: core.Codex, Model: "gpt", Effort: "low"}

func TestACostLimitIsSetChangedAndCleared(t *testing.T) {
	w, run, _, _ := limitedRun(t, 0, 0)

	if r := w.update(run, core.UpdateRun{CostLimit: setCost(5)}); r.Refusal != "" {
		t.Fatalf("set refused: %+v", r)
	}
	if got := w.runOf(run).MaxCostUSD; got != 5 {
		t.Fatalf("cost limit = %v, want 5", got)
	}
	if got := w.changes(); len(got) != 1 || *got[0].CostLimit != (core.CostLimitChange{From: 0, To: 5}) {
		t.Fatalf("changes = %+v, want one from none to 5", got)
	}

	// The same limit again changes nothing, and says nothing.
	if r := w.update(run, core.UpdateRun{CostLimit: setCost(5)}); r.Refusal != "" || w.settingEvents() != 0 {
		t.Fatalf("a repeated limit: refusal %q and %d events", r.Refusal, w.settingEvents())
	}

	w.update(run, core.UpdateRun{CostLimit: setCost(8)})
	if got := w.changes(); len(got) != 1 || *got[0].CostLimit != (core.CostLimitChange{From: 5, To: 8}) {
		t.Fatalf("changes = %+v, want 5 to 8", got)
	}

	w.update(run, core.UpdateRun{CostLimit: &core.LimitChange[float64]{Remove: true}})
	if got := w.runOf(run).MaxCostUSD; got != 0 {
		t.Fatalf("cost limit after removal = %v", got)
	}
	if got := w.changes(); len(got) != 1 || *got[0].CostLimit != (core.CostLimitChange{From: 8, To: 0}) {
		t.Fatalf("changes = %+v, want 8 to none", got)
	}
	w.update(run, core.UpdateRun{CostLimit: &core.LimitChange[float64]{Remove: true}})
	if w.settingEvents() != 0 {
		t.Fatalf("removing no limit recorded %d events", w.settingEvents())
	}
}

// A cost limit at or under what the run has spent is reached as any other is: the lead is told to
// finish, no agent starts, and the run stops when the lead has posted its final report or after the grace.
func TestACostLimitBelowTheSpendIsReachedAtOnceAndThenRefusesChanges(t *testing.T) {
	w, run, leadID, _ := limitedRun(t, 0, 0)
	w.send(leadID, "go on")
	w.endTurnCosting(leadID, 3)

	w.update(run, core.UpdateRun{CostLimit: setCost(2)})
	got := w.runOf(run)
	if got.LimitReached != core.LimitCost || got.MaxCostUSD != 2 || got.Status != core.RunRunning {
		t.Fatalf("after lowering the limit under the spend: %+v", got)
	}
	if told := w.limitMessages(); len(told) != 1 || told[0].Limit != core.LimitCost || told[0].MaxCost != 2 {
		t.Fatalf("the lead was told %+v", told)
	}
	if _, ok := w.graceTimer(); !ok {
		t.Fatal("no grace timer for the lead to finish in")
	}
	if r := w.spawnWorker(run, leadID); r.Refusal != core.RefuseCostLimitReached {
		t.Fatalf("a spawn after the limit: %+v", r)
	}

	// The limit it stops for is not to be undone, and an update that names it changes nothing at all.
	before := w.runOf(run)
	r := w.update(run, core.UpdateRun{CostLimit: setCost(50), Fanout: fanout(1)})
	if r.Refusal != core.RefuseCostLimitReached || !reflectEqualRuns(before, w.runOf(run)) || w.settingEvents() != 0 {
		t.Fatalf("an update of the reached limit: %+v, run now %+v", r, w.runOf(run))
	}
	if r := w.update(run, core.UpdateRun{CostLimit: &core.LimitChange[float64]{Remove: true}}); r.Refusal != core.RefuseCostLimitReached {
		t.Fatalf("removing the reached limit: %+v", r)
	}
	// Another limit is still the operator's to change.
	if r := w.update(run, core.UpdateRun{TimeLimit: setTime(time.Hour)}); r.Refusal != "" {
		t.Fatalf("a time limit on a run that reached its cost limit: %+v", r)
	}
}

func reflectEqualRuns(a, b core.Run) bool {
	return a.Status == b.Status && a.FanoutLimit == b.FanoutLimit && a.MaxCostUSD == b.MaxCostUSD && a.Deadline.Equal(b.Deadline) &&
		a.LimitReached == b.LimitReached && a.PendingLead == nil && b.PendingLead == nil
}

func TestATimeLimitIsExtendedAndTheOldTimerThenDoesNothing(t *testing.T) {
	w, run, _, first := limitedRun(t, time.Hour, 0)
	created := w.runOf(run).CreatedAt

	w.update(run, core.UpdateRun{TimeLimit: setTime(3 * time.Hour)})
	if got := w.runOf(run).Deadline; !got.Equal(created.Add(3 * time.Hour)) {
		t.Fatalf("deadline = %v, want three hours from the start", got)
	}
	if got := w.changes(); len(got) != 1 || *got[0].TimeLimit != (core.TimeLimitChange{From: time.Hour, To: 3 * time.Hour}) {
		t.Fatalf("changes = %+v", got)
	}
	w.wantEffects("ArmTimer")
	extended := w.effects[0].(core.ArmTimer)
	if extended.Kind != core.TimerRunDeadline || !extended.FireAt.Equal(created.Add(3*time.Hour)) {
		t.Fatalf("armed %+v for the new deadline", extended)
	}
	w.fire(first)
	if len(w.events) != 0 || w.runOf(run).LimitReached != "" {
		t.Fatalf("the old deadline's timer recorded %v", w.events)
	}
	w.fire(extended)
	if got := w.runOf(run); got.LimitReached != core.LimitTime {
		t.Fatalf("at the extended deadline: %+v", got)
	}
}

func TestATimeLimitIsShortenedAndTheLaterTimerThenDoesNothing(t *testing.T) {
	w, run, _, first := limitedRun(t, time.Hour, 0)
	w.update(run, core.UpdateRun{TimeLimit: setTime(30 * time.Minute)})
	w.wantEffects("ArmTimer")
	soon := w.effects[0].(core.ArmTimer)
	w.fire(soon)
	if got := w.runOf(run); got.LimitReached != core.LimitTime || got.Status != core.RunRunning {
		t.Fatalf("at the shortened deadline: %+v", got)
	}
	w.fire(first)
	if len(w.events) != 0 {
		t.Fatalf("the old deadline's timer recorded %v after the limit was reached", w.events)
	}
	if r := w.update(run, core.UpdateRun{TimeLimit: setTime(10 * time.Hour)}); r.Refusal != core.RefuseTimeLimitReached {
		t.Fatalf("a change of the time limit it reached: %+v", r)
	}
}

func TestATimeLimitThatHasPassedIsReachedAtOnce(t *testing.T) {
	w, run, leadID, _ := limitedRun(t, 0, 0)
	w.send(leadID, "go on")
	w.endTurn(leadID, core.TurnCompleted)

	// The run is a few seconds old; a total of one second has passed.
	w.update(run, core.UpdateRun{TimeLimit: setTime(time.Second)})
	got := w.runOf(run)
	if got.LimitReached != core.LimitTime || got.Status != core.RunRunning || got.Deadline.After(w.now) {
		t.Fatalf("after a limit that had passed: %+v", got)
	}
	if told := w.limitMessages(); len(told) != 1 || told[0].Limit != core.LimitTime {
		t.Fatalf("the lead was told %+v", told)
	}
	for _, e := range w.effects {
		if a, ok := e.(core.ArmTimer); ok && a.Kind == core.TimerRunDeadline {
			t.Fatalf("armed a timer for a deadline that had passed: %+v", a)
		}
	}
	grace, ok := w.graceTimer()
	if !ok {
		t.Fatal("no grace timer")
	}
	w.fire(grace)
	if got := w.runOf(run); got.Status != core.RunStopping || got.StopReason != core.StopTimeLimit {
		t.Fatalf("after the grace: %+v", got)
	}
}

func TestATimeLimitIsRemovedAndItsTimerThenDoesNothing(t *testing.T) {
	w, run, _, timer := limitedRun(t, time.Hour, 0)
	w.update(run, core.UpdateRun{TimeLimit: &core.LimitChange[time.Duration]{Remove: true}})
	if got := w.runOf(run); !got.Deadline.IsZero() {
		t.Fatalf("deadline after removal = %v", got.Deadline)
	}
	if got := w.changes(); len(got) != 1 || *got[0].TimeLimit != (core.TimeLimitChange{From: time.Hour}) {
		t.Fatalf("changes = %+v, want an hour to none", got)
	}
	w.fire(timer)
	if len(w.events) != 0 || w.runOf(run).LimitReached != "" {
		t.Fatalf("the removed deadline's timer recorded %v", w.events)
	}
}

// Lowering the fan-out stops no agent. A spawn is refused while the live subagents are at or over the
// limit, and so is a resume of one that went down; it is admitted again once the count is under it.
func TestLoweringTheFanoutStopsNoAgentAndAdmitsNoneUntilTheCountIsUnder(t *testing.T) {
	w, run, leadID := started(t, 3, steers)
	workers := []core.AgentID{w.spawn(run, leadID, steers), w.spawn(run, leadID, steers), w.spawn(run, leadID, steers)}

	w.update(run, core.UpdateRun{Fanout: fanout(1)})
	if got := w.changes(); len(got) != 1 || *got[0].Fanout != (core.FanoutChange{From: 3, To: 1}) {
		t.Fatalf("changes = %+v", got)
	}
	for _, id := range workers {
		if a, _ := w.s.Agent(id); a.Status.Over() || a.Stop != core.StopNone {
			t.Fatalf("lowering the limit stopped %+v", a)
		}
	}
	if r := w.spawnWorker(run, leadID); r.Refusal != core.RefuseFanoutLimit {
		t.Fatalf("a spawn over the limit: %+v", r)
	}

	// Two come down: one is live, which is at the limit of one, so there is still no room.
	w.command(operator, core.StopAgent{Agent: workers[0]})
	w.fact(workers[0], core.SessionClosed{})
	w.command(operator, core.StopAgent{Agent: workers[1]})
	w.fact(workers[1], core.SessionClosed{})
	if r := w.spawnWorker(run, leadID); r.Refusal != core.RefuseFanoutLimit {
		t.Fatalf("a spawn at the limit: %+v", r)
	}
	// Mail to a subagent that went down would resume it, which is another admission.
	if r := w.command(operator, core.SendMessage{Agent: workers[0], Text: "again"}); r.Delivery != core.DeliveryRejected {
		t.Fatalf("a message that would resume an agent at the limit: %+v", r)
	}

	w.command(operator, core.StopAgent{Agent: workers[2]})
	w.fact(workers[2], core.SessionClosed{})
	if r := w.spawnWorker(run, leadID); r.Refusal != "" {
		t.Fatalf("a spawn under the limit: %+v", r)
	}

	// Raised again, it admits more.
	w.update(run, core.UpdateRun{Fanout: fanout(2)})
	if r := w.spawnWorker(run, leadID); r.Refusal != "" {
		t.Fatalf("a spawn under the raised limit: %+v", r)
	}
	if r := w.spawnWorker(run, leadID); r.Refusal != core.RefuseFanoutLimit {
		t.Fatalf("a spawn at the raised limit: %+v", r)
	}
}

func TestAnUpdateIsAllOrNone(t *testing.T) {
	w, run, _ := started(t, 3, steers)
	before := w.runOf(run)

	// The host's policy allows one model; the lead asked for another. Nothing else the update names changes.
	w.models = core.ModelPolicy{Allowed: []core.ModelName{"opus"}, Known: map[core.Harness][]core.ModelName{core.ClaudeCode: {"opus"}, core.Codex: {"gpt"}}}
	r := w.update(run, core.UpdateRun{CostLimit: setCost(4), Fanout: fanout(1), Lead: &leadOn})
	if r.Refusal != core.RefuseModelNotAllowed || r.Model != "gpt" {
		t.Fatalf("receipt %+v, want the policy's refusal naming the model", r)
	}
	if got := w.runOf(run); !reflectEqualRuns(before, got) || w.settingEvents() != 0 {
		t.Fatalf("a refused update changed the run: %+v", got)
	}
}

func TestAnUpdateOfARunThatIsGoneOrNotTheOperatorsIsRefused(t *testing.T) {
	w, run, leadID := started(t, 3, steers)
	if r := w.update("run-nope", core.UpdateRun{Fanout: fanout(1)}); r.Refusal != core.RefuseRunNotFound {
		t.Fatalf("an unknown run: %+v", r)
	}
	// Only the operator changes a run: the lead has no tool that does.
	if r := w.command(core.AgentCaller{Agent: leadID}, core.UpdateRun{Run: run, Fanout: fanout(1)}); r.Refusal != core.RefuseNotPermitted {
		t.Fatalf("the lead updating its own run: %+v", r)
	}
	w.command(operator, core.StopRun{Run: run})
	if r := w.update(run, core.UpdateRun{Fanout: fanout(1)}); r.Refusal != core.RefuseRunNotRunning {
		t.Fatalf("a run that is stopping: %+v", r)
	}
}

// The limits and the fan-out of a run in setup are those of the optimize phase, which its completion sets;
// the lead it talks to may change.
func TestTheLimitsOfARunInSetupAreRefusedButItsLeadMayChange(t *testing.T) {
	w := newScript(t)
	at := epoch
	run := core.Run{ID: "run-s", Repository: "repo", Status: core.RunRunning, Lead: "lead-s", FanoutLimit: 1, CreatedAt: at, Phase: core.PhaseSetup,
		Allowed: []core.AgentChoice{{Harness: lead.Harness, Model: lead.Model}}}
	agent := core.Agent{ID: "lead-s", Run: "run-s", Role: core.RoleLead, Spec: lead, Status: core.AgentIdle, Stop: core.StopNone, Session: 1, StartedAt: at}
	w.s = core.Replay(core.State{}, []core.Event{{Run: "run-s", At: at, Body: run}, {Run: "run-s", At: at, Body: agent}})

	if r := w.update("run-s", core.UpdateRun{CostLimit: setCost(4)}); r.Refusal != core.RefuseNotPermitted {
		t.Fatalf("a cost limit in setup: %+v", r)
	}
	if r := w.update("run-s", core.UpdateRun{TimeLimit: setTime(time.Hour), Fanout: fanout(2)}); r.Refusal != core.RefuseNotPermitted {
		t.Fatalf("a time limit in setup: %+v", r)
	}
	if r := w.update("run-s", core.UpdateRun{Fanout: fanout(2), Lead: &leadOn}); r.Refusal != core.RefuseNotPermitted || w.runOf("run-s").FanoutLimit != 1 {
		t.Fatalf("a fan-out in setup: %+v", r)
	}
	if r := w.update("run-s", core.UpdateRun{Lead: &leadOn}); r.Refusal != "" || w.runOf("run-s").PendingLead == nil {
		t.Fatalf("the lead in setup: %+v", r)
	}
}

// startedTurn is the turn the last step started, if it started one.
func (w *script) startedTurn() (core.StartTurn, bool) {
	for _, e := range w.effects {
		if s, ok := e.(core.StartTurn); ok {
			return s, true
		}
	}
	return core.StartTurn{}, false
}

func (w *script) launched() (core.LaunchSession, bool) {
	for _, e := range w.effects {
		if l, ok := e.(core.LaunchSession); ok {
			return l, true
		}
	}
	return core.LaunchSession{}, false
}

// An idle lead is replaced at once: its session closes, and when it is closed the new one opens on the
// new spec with nothing to resume, the change is recorded as applied, and the new lead's first turn is
// the handover, ahead of the mail that was waiting.
func TestAnIdleLeadIsReplacedByAFreshSessionThatReadsTheHandoverFirst(t *testing.T) {
	w, run, leadID := started(t, 2, noSteer)
	worker := w.spawn(run, leadID, noSteer)
	w.post(leadID, core.ReportProgress)

	w.update(run, core.UpdateRun{Lead: &leadOn})
	got := w.changes()
	if len(got) != 1 || got[0].Lead.State != core.LeadChangePending || got[0].Lead.From != lead || got[0].Lead.To != leadOn {
		t.Fatalf("changes = %+v, want the lead asked for", got)
	}
	if pending := w.runOf(run).PendingLead; pending == nil || *pending != leadOn {
		t.Fatalf("pending lead = %v", pending)
	}
	w.wantEffects("ArmTimer", "CloseSession")
	if l, _ := w.s.Agent(leadID); l.Stop != core.StopClosing || l.Spec != lead {
		t.Fatalf("the lead is %+v: it should be closing on its old spec", l)
	}
	if !slices.Contains(w.runOf(run).Allowed, core.AgentChoice{Harness: leadOn.Harness, Model: leadOn.Model}) {
		t.Fatalf("the run's allowance %v lacks the new lead's", w.runOf(run).Allowed)
	}

	// Mail that comes while the session closes waits for the next one.
	w.step(core.JobUpdate{Job: "j1", Run: run, State: core.JobSucceeded})
	sent := w.command(operator, core.SendMessage{Agent: leadID, Text: "hello"})
	if sent.Delivery != core.DeliveryQueued {
		t.Fatalf("mail to the closing lead was %s", sent.Delivery)
	}
	w.fact(worker, core.TurnEnded{Turn: w.turn(worker).ID, Status: core.TurnCompleted})

	w.fact(leadID, core.SessionClosed{})
	launch, ok := w.launched()
	if !ok || launch.Spec != leadOn || launch.ResumeToken != "" || launch.Session.N != 2 || launch.Role != core.RoleLead {
		t.Fatalf("launch = %+v (%v): want session 2 on the new spec with no token", launch, ok)
	}
	applied := w.changes()
	if len(applied) != 1 || applied[0].Lead.State != core.LeadChangeApplied || applied[0].Lead.From != lead || applied[0].Lead.To != leadOn {
		t.Fatalf("changes = %+v, want the lead's change applied", applied)
	}
	if a, _ := w.s.Agent(leadID); a.Spec != leadOn || a.ResumeToken != "" || a.Status != core.AgentLaunching || a.SessionFailures != 0 {
		t.Fatalf("the lead after the switch: %+v", a)
	}
	if w.runOf(run).PendingLead != nil {
		t.Fatal("the change is still pending after it was applied")
	}

	w.open(leadID, noSteer)
	turn, ok := w.startedTurn()
	handover, isHandover := turn.Message.Body.(core.LeadHandover)
	if !ok || !isHandover {
		t.Fatalf("the new lead's first turn is for %+v", turn.Message.Body)
	}
	if handover.Previous != lead || handover.Task != "task" || len(handover.Agents) != 1 || handover.Agents[0].ID != worker || handover.LatestReport == nil {
		t.Fatalf("handover = %+v", handover)
	}
	if got := w.turn(leadID).Trigger; got != core.TriggerBrief {
		t.Fatalf("the handover turn's trigger = %s", got)
	}

	// The mail that waited is delivered after it, in the order it came.
	var after []core.MessageBody
	for range 5 {
		w.endTurn(leadID, core.TurnCompleted)
		if turn, ok := w.startedTurn(); ok {
			after = append(after, turn.Message.Body)
			continue
		}
		break
	}
	if len(after) != 2 {
		t.Fatalf("turns after the handover: %+v", after)
	}
	if _, ok := after[0].(core.JobFinished); !ok {
		t.Fatalf("the first turn after the handover is for %+v, want the job that finished", after[0])
	}
	if text, ok := after[1].(core.Text); !ok || text.Text != "hello" {
		t.Fatalf("the second turn after the handover is for %+v", after[1])
	}
}

// A lead in a turn finishes it first: the change waits, and takes effect when the turn ends, before any
// mail that arrived during it starts another.
func TestALeadInATurnIsReplacedWhenTheTurnEnds(t *testing.T) {
	w, run, leadID := started(t, 2, noSteer)
	w.command(operator, core.SendMessage{Agent: leadID, Text: "work"})
	w.turn(leadID)

	w.update(run, core.UpdateRun{Lead: &leadOn})
	if len(w.effects) != 0 {
		t.Fatalf("a lead in a turn was acted on: %v", w.kinds())
	}
	if a, _ := w.s.Agent(leadID); a.Status != core.AgentActive || a.Stop != core.StopNone {
		t.Fatalf("the lead is %+v", a)
	}
	w.step(core.JobUpdate{Job: "j1", Run: run, State: core.JobFailed})

	w.endTurn(leadID, core.TurnCompleted)
	w.wantEffects("ArmTimer", "CloseSession")
	if _, started := w.startedTurn(); started {
		t.Fatal("a turn started on the old session after the change was asked for")
	}
	w.fact(leadID, core.SessionClosed{})
	if l, ok := w.launched(); !ok || l.Spec != leadOn {
		t.Fatalf("launch = %+v", l)
	}
}

// Asking for the spec the lead already has withdraws a change that waited, and asking again for the
// one that waits changes nothing.
func TestAChangeOfTheLeadIsWithdrawnByAskingForTheLeadItHas(t *testing.T) {
	w, run, leadID := started(t, 2, noSteer)
	w.command(operator, core.SendMessage{Agent: leadID, Text: "work"})

	w.update(run, core.UpdateRun{Lead: &leadOn})
	w.update(run, core.UpdateRun{Lead: &leadOn})
	if w.settingEvents() != 0 {
		t.Fatalf("asking for the lead that waits recorded %d events", w.settingEvents())
	}
	w.update(run, core.UpdateRun{Lead: &lead})
	if got := w.changes(); len(got) != 1 || got[0].Lead.State != core.LeadChangeApplied || got[0].Lead.From != leadOn || got[0].Lead.To != lead {
		t.Fatalf("changes = %+v, want the wait withdrawn", got)
	}
	if w.runOf(run).PendingLead != nil {
		t.Fatal("the withdrawn change still waits")
	}
	w.update(run, core.UpdateRun{Lead: &lead})
	if w.settingEvents() != 0 {
		t.Fatalf("asking for the lead it has recorded %d events", w.settingEvents())
	}
	w.endTurn(leadID, core.TurnCompleted)
	if slices.Contains(w.kinds(), "CloseSession") {
		t.Fatalf("a withdrawn change was acted on: %v", w.kinds())
	}
}

// A change that waits for a lead that is stopping is dropped with the run.
func TestAChangeOfTheLeadIsDroppedWhenTheRunStops(t *testing.T) {
	w, run, leadID := started(t, 2, noSteer)
	w.command(operator, core.SendMessage{Agent: leadID, Text: "work"})
	w.update(run, core.UpdateRun{Lead: &leadOn})
	w.command(operator, core.StopRun{Run: run})
	if got := w.runOf(run); got.PendingLead != nil || got.Status != core.RunStopping {
		t.Fatalf("the stopping run: %+v", got)
	}
	w.fact(leadID, core.TurnEnded{Turn: w.turn(leadID).ID, Status: core.TurnInterrupted})
	w.fact(leadID, core.SessionClosed{})
	if got := w.runOf(run); got.Status != core.RunEnded {
		t.Fatalf("the run is %+v", got)
	}
	if l, _ := w.s.Agent(leadID); l.Spec != lead || l.Session != 1 {
		t.Fatalf("the lead of a stopped run was replaced: %+v", l)
	}
}

// A lead that falls while another waits to take over is replaced by the one that waits, not resumed.
func TestALeadThatFallsWhileAnotherWaitsIsReplacedNotResumed(t *testing.T) {
	w, run, leadID := started(t, 2, noSteer)
	w.command(operator, core.SendMessage{Agent: leadID, Text: "work"})
	w.update(run, core.UpdateRun{Lead: &leadOn})
	w.step(core.SessionLost{Session: core.Session{Agent: leadID, N: 1}, Detail: "gone"})
	l, ok := w.launched()
	if !ok || l.Spec != leadOn || l.ResumeToken != "" {
		t.Fatalf("launch after the lead fell = %+v", l)
	}
	if got := w.runOf(run); got.PendingLead != nil || got.Status.Final() {
		t.Fatalf("run %+v", got)
	}
}

// A second change waits for the first handover to be read.
func TestASecondChangeOfTheLeadWaitsForTheFirstHandoverToBeRead(t *testing.T) {
	w, run, leadID := started(t, 2, noSteer)
	w.update(run, core.UpdateRun{Lead: &leadOn})
	w.fact(leadID, core.SessionClosed{})
	next := core.AgentSpec{Harness: core.ClaudeCode, Model: "sonnet"}
	w.update(run, core.UpdateRun{Lead: &next})
	w.open(leadID, noSteer)
	if turn, ok := w.startedTurn(); !ok {
		t.Fatal("the handover did not start a turn")
	} else if _, ok := turn.Message.Body.(core.LeadHandover); !ok {
		t.Fatalf("first turn is for %+v", turn.Message.Body)
	}
	if got := w.runOf(run).PendingLead; got == nil || *got != next {
		t.Fatalf("pending = %v, want the second change still waiting", got)
	}
	w.endTurn(leadID, core.TurnCompleted)
	w.wantEffects("ArmTimer", "CloseSession")
	w.fact(leadID, core.SessionClosed{})
	if l, ok := w.launched(); !ok || l.Spec != next || l.Session.N != 3 {
		t.Fatalf("launch = %+v", l)
	}
}

// A lead that falls before it read the handover of a change, while another change waits, is handed the run
// once: the handover it never read is written again as the run stands, not queued beside the new one.
func TestALeadThatFallsBeforeReadingItsHandoverIsHandedTheRunOnce(t *testing.T) {
	w, run, leadID := started(t, 2, noSteer)
	w.update(run, core.UpdateRun{Lead: &leadOn})
	w.fact(leadID, core.SessionClosed{})
	next := core.AgentSpec{Harness: core.ClaudeCode, Model: "sonnet"}
	w.update(run, core.UpdateRun{Lead: &next})

	w.step(core.SessionLost{Session: core.Session{Agent: leadID, N: 2}, Detail: "gone"})
	if l, ok := w.launched(); !ok || l.Spec != next || l.Session.N != 3 || l.ResumeToken != "" {
		t.Fatalf("launch = %+v (%v)", l, ok)
	}
	var handovers []core.LeadHandover
	for _, m := range w.s.InTransit() {
		if h, ok := m.Body.(core.LeadHandover); ok {
			handovers = append(handovers, h)
		}
	}
	if len(handovers) != 1 || handovers[0].Previous != leadOn {
		t.Fatalf("the handovers waiting for the lead: %+v, want one, from the lead that fell", handovers)
	}
	w.open(leadID, noSteer)
	if turn, ok := w.startedTurn(); !ok {
		t.Fatal("no turn started")
	} else if _, ok := turn.Message.Body.(core.LeadHandover); !ok {
		t.Fatalf("first turn is for %+v", turn.Message.Body)
	}
}

// A crash at any point between the request and the switch recovers from the log: the run holds the
// wanted lead, and what is left of the change is done once.
func TestACrashBetweenRequestedAndAppliedRecoversFromThePendingLead(t *testing.T) {
	for crashAt := range 5 {
		w, run, leadID := started(t, 2, noSteer)
		w.command(operator, core.SendMessage{Agent: leadID, Text: "work"})
		step := 0
		crash := func() {
			if step == crashAt {
				w.s = recovered(t, w.log)
			}
			step++
		}
		crash()
		w.update(run, core.UpdateRun{Lead: &leadOn})
		crash()
		w.endTurn(leadID, core.TurnCompleted)
		crash()
		w.fact(leadID, core.SessionClosed{})
		crash()
		w.open(leadID, noSteer)
		crash()
		w.endTurn(leadID, core.TurnCompleted)

		got := w.runOf(run)
		l, _ := w.s.Agent(leadID)
		if got.PendingLead != nil || l.Spec != leadOn || l.Session != 2 {
			t.Fatalf("crash at %d: run %+v, lead %+v", crashAt, got, l)
		}
		requested, applied, handovers := 0, 0, 0
		for _, e := range w.log {
			switch b := e.Body.(type) {
			case core.RunSettingsUpdated:
				for _, c := range b.Changes {
					if c.Lead != nil && c.Lead.State == core.LeadChangePending {
						requested++
					}
					if c.Lead != nil && c.Lead.State == core.LeadChangeApplied {
						applied++
					}
				}
			case core.Message:
				if _, ok := b.Body.(core.LeadHandover); ok && b.Delivery == core.DeliveryQueued {
					handovers++
				}
			}
		}
		if requested != 1 || applied != 1 || handovers != 1 {
			t.Fatalf("crash at %d: %d requested, %d applied, %d handovers", crashAt, requested, applied, handovers)
		}
	}
}

// recovered is the state a restarted daemon rebuilds from the committed events as the store holds them.
func recovered(t *testing.T, log []core.Event) core.State {
	t.Helper()
	var events []core.Event
	for i, e := range log {
		if r, ok := e.Body.(core.Receipt); ok {
			// The script's digests are placeholders; the store holds a real one's hex.
			r.Digest = core.Digest(strings.Repeat("a", 64))
			e.Body = r
		}
		data, err := wire.EncodeEvent(uint64(i+1), e)
		if err != nil {
			t.Fatal(err)
		}
		_, back, err := wire.DecodeEvent(data)
		if err != nil {
			t.Fatal(err)
		}
		events = append(events, back)
	}
	return core.Replay(core.State{}, events)
}
