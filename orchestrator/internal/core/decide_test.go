package core_test

import (
	"fmt"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

var epoch = time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)

// script drives Decide by hand for the named scenarios. Each step checks what holds of every step.
type script struct {
	t       *testing.T
	s       core.State
	now     time.Time
	steps   int
	events  []core.Event
	effects []core.Effect
	log     []core.Event
	offsets map[core.Session]uint64
}

func newScript(t *testing.T) *script {
	return &script{t: t, now: epoch, offsets: map[core.Session]uint64{}}
}

func (w *script) step(body core.InputBody) {
	w.t.Helper()
	w.steps++
	w.now = w.now.Add(time.Second)
	before := w.s
	in := core.Input{At: w.now, Nonce: core.Nonce(fmt.Sprintf("n%d", w.steps)), Body: body}
	w.s, w.events, w.effects = core.Decide(w.s, in)
	w.log = append(w.log, w.events...)
	if !reflect.DeepEqual(core.Replay(before, w.events), core.Replay(w.s, nil)) {
		w.t.Fatalf("Replay of the events of %#v differs from the state Decide returned", body)
	}
	if v := w.s.Violations(); len(v) > 0 {
		w.t.Fatalf("after %#v: %v", body, v)
	}
}

// command runs a command and returns its receipt.
func (w *script) command(caller core.Caller, body core.CommandBody) core.Receipt {
	w.t.Helper()
	id := core.CommandID(fmt.Sprintf("c%d", w.steps))
	w.step(core.Command{ID: id, Digest: "d", Caller: caller, Body: body})
	r, recall := w.s.Recall(id, "d")
	if recall != core.RecallRepeat {
		w.t.Fatalf("command %#v left no receipt: %v", body, recall)
	}
	return r
}

func (w *script) fact(a core.AgentID, body core.FactBody) {
	w.t.Helper()
	agent, ok := w.s.Agent(a)
	if !ok {
		w.t.Fatalf("no agent %s", a)
	}
	session := core.Session{Agent: a, N: agent.Session}
	w.offsets[session]++
	w.step(core.SidecarFact{Session: session, Offset: w.offsets[session], Body: body})
}

func (w *script) open(a core.AgentID, caps core.Capabilities) {
	w.t.Helper()
	w.fact(a, core.ConversationOpened{ConversationID: "conv-" + string(a), Capabilities: caps, Worktree: "/w/" + string(a)})
}

func (w *script) turn(a core.AgentID) core.Turn {
	w.t.Helper()
	turn, ok := w.s.ActiveTurn(a)
	if !ok {
		w.t.Fatalf("agent %s has no running turn", a)
	}
	return turn
}

func (w *script) endTurn(a core.AgentID, status core.TurnStatus) {
	w.t.Helper()
	w.fact(a, core.TurnEnded{Turn: w.turn(a).ID, Status: status})
}

func (w *script) status(a core.AgentID) core.AgentStatus {
	w.t.Helper()
	agent, ok := w.s.Agent(a)
	if !ok {
		w.t.Fatalf("no agent %s", a)
	}
	return agent.Status
}

// kinds names the effects of the last step.
func (w *script) kinds() []string {
	out := make([]string, 0, len(w.effects))
	for _, e := range w.effects {
		out = append(out, fmt.Sprintf("%T", e)[len("core."):])
	}
	return out
}

func (w *script) wantEffects(want ...string) {
	w.t.Helper()
	if got := w.kinds(); !slices.Equal(got, want) {
		w.t.Fatalf("effects = %v, want %v", got, want)
	}
}

func (w *script) delivery(id core.MessageID) core.Delivery {
	w.t.Helper()
	for _, e := range slices.Backward(w.log) {
		if m, ok := e.Body.(core.Message); ok && m.ID == id {
			return m.Delivery
		}
	}
	w.t.Fatalf("no message %s", id)
	return ""
}

var (
	steers   = core.Capabilities{Steers: true, Resumes: true}
	noSteer  = core.Capabilities{Resumes: true}
	bare     = core.Capabilities{}
	operator = core.Operator{}
	lead     = core.AgentSpec{Harness: core.ClaudeCode, Model: "opus", Effort: "high"}
)

// started is a running run whose lead is resting after its brief.
func started(t *testing.T, fanout uint32, caps core.Capabilities) (*script, core.RunID, core.AgentID) {
	t.Helper()
	w := newScript(t)
	r := w.command(operator, core.StartRun{Repository: "repo", Task: "task", FanoutLimit: fanout, Lead: lead, Brief: "go"})
	w.wantEffects("LaunchSession")
	w.open(r.Agent, caps)
	if got := w.turn(r.Agent).Trigger; got != core.TriggerBrief {
		t.Fatalf("the first turn's trigger = %s", got)
	}
	w.wantEffects("StartTurn")
	w.endTurn(r.Agent, core.TurnCompleted)
	return w, r.Run, r.Agent
}

func (w *script) spawn(run core.RunID, by core.AgentID, caps core.Capabilities) core.AgentID {
	w.t.Helper()
	r := w.command(core.AgentCaller{Agent: by}, core.SpawnAgent{Run: run, Role: core.RoleWorker, Brief: "work"})
	if r.Refusal != "" {
		w.t.Fatalf("spawn refused: %s", r.Refusal)
	}
	w.open(r.Agent, caps)
	return r.Agent
}

func TestARunStartsAndStops(t *testing.T) {
	w, run, leadID := started(t, 2, steers)
	if got, _ := w.s.Run(run); got.Status != core.RunRunning {
		t.Fatalf("run is %s, want running", got.Status)
	}
	worker := w.spawn(run, leadID, steers)
	if w.status(leadID) != core.AgentWaiting {
		t.Fatalf("a lead with a live subagent is %s, want waiting", w.status(leadID))
	}
	r := w.command(operator, core.StopRun{Run: run})
	if r.Refusal != "" {
		t.Fatalf("stop refused: %s", r.Refusal)
	}
	if got, _ := w.s.Run(run); got.Status != core.RunStopping || got.StopReason != core.StopOperator {
		t.Fatalf("run is %+v, want stopping for the operator", got)
	}
	w.endTurn(worker, core.TurnInterrupted)
	w.wantEffects("CloseSession")
	w.fact(worker, core.SessionClosed{})
	if got, _ := w.s.Run(run); got.Status != core.RunStopping {
		t.Fatalf("the run finished with its lead still open: %s", got.Status)
	}
	w.fact(leadID, core.SessionClosed{})
	if got, _ := w.s.Run(run); got.Status != core.RunEnded || got.EndedAt.IsZero() {
		t.Fatalf("run is %+v, want ended", got)
	}
}

func TestAStoppingRunAcceptsARepeatOfTheStop(t *testing.T) {
	w, run, _ := started(t, 0, steers)
	w.command(operator, core.StopRun{Run: run})
	if r := w.command(operator, core.StopRun{Run: run}); r.Refusal != "" {
		t.Fatalf("second stop refused: %s", r.Refusal)
	}
	w.wantEffects()
}

func TestSpawnStopsAtTheFanOutLimit(t *testing.T) {
	w, run, leadID := started(t, 2, steers)
	first := w.spawn(run, leadID, steers)
	w.spawn(run, leadID, steers)
	spawn := core.SpawnAgent{Run: run, Role: core.RoleJudge, Brief: "judge"}
	if r := w.command(operator, spawn); r.Refusal != core.RefuseFanoutLimit {
		t.Fatalf("over the limit: refusal = %q", r.Refusal)
	}
	w.fact(first, core.SessionClosed{})
	if r := w.command(operator, spawn); r.Refusal != "" {
		t.Fatalf("under the limit again: refusal = %q", r.Refusal)
	}
}

func TestASpawnInheritsWhatItLeavesOut(t *testing.T) {
	w, run, leadID := started(t, 3, steers)
	same := w.spawn(run, leadID, steers)
	if got, _ := w.s.Agent(same); got.Spec != lead {
		t.Fatalf("an unspecified spawn runs on %+v, want the lead's %+v", got.Spec, lead)
	}
	r := w.command(core.AgentCaller{Agent: leadID}, core.SpawnAgent{
		Run: run, Role: core.RoleWorker, Spec: core.AgentSpec{Harness: core.Codex}, Brief: "work",
	})
	if got, _ := w.s.Agent(r.Agent); got.Spec != (core.AgentSpec{Harness: core.Codex}) {
		t.Fatalf("a spawn on another harness runs on %+v: the lead's model means nothing there", got.Spec)
	}
}

// The role table is one table, and every command goes through it. Each principal and action is
// checked against the table's own promise.
func TestTheRoleTable(t *testing.T) {
	everyone := []core.Principal{
		core.PrincipalOperator, core.PrincipalLead, core.PrincipalPlanner, core.PrincipalWorker,
		core.PrincipalJudge, core.PrincipalCombiner, core.PrincipalProfiler,
	}
	allowed := map[core.Action]func(core.Principal) bool{
		core.ActionStartRun:    func(p core.Principal) bool { return p == core.PrincipalOperator },
		core.ActionStopRun:     func(p core.Principal) bool { return p == core.PrincipalOperator },
		core.ActionSpawnAgent:  func(p core.Principal) bool { return p == core.PrincipalOperator || p == core.PrincipalLead },
		core.ActionSendMessage: func(p core.Principal) bool { return p == core.PrincipalOperator || p == core.PrincipalLead },
		core.ActionListAgents:  func(p core.Principal) bool { return p == core.PrincipalOperator || p == core.PrincipalLead },
		core.ActionStopAgent:   func(p core.Principal) bool { return p == core.PrincipalOperator || p == core.PrincipalLead },
		core.ActionPostHandoff: func(p core.Principal) bool { return p != core.PrincipalOperator && p != core.PrincipalLead },
		core.ActionPostReport:  func(p core.Principal) bool { return p == core.PrincipalLead },
	}
	for action, want := range allowed {
		for _, p := range everyone {
			if got := core.Allowed(p, action); got != want(p) {
				t.Errorf("Allowed(%s, %s) = %v", p, action, got)
			}
		}
	}
}

func TestDecideAppliesTheRoleTable(t *testing.T) {
	w, run, leadID := started(t, 3, steers)
	worker := w.spawn(run, leadID, steers)
	stranger := core.AgentCaller{Agent: "nobody"}
	workerCaller := core.AgentCaller{Agent: worker}
	cases := []struct {
		name   string
		caller core.Caller
		body   core.CommandBody
	}{
		{"a command with no caller", nil, core.StopRun{Run: run}},
		{"an unknown agent", stranger, core.PostHandoff{}},
		{"a worker stopping the run", workerCaller, core.StopRun{Run: run}},
		{"a worker spawning", workerCaller, core.SpawnAgent{Run: run, Role: core.RoleWorker, Brief: "b"}},
		{"a worker posting a report", workerCaller, core.PostReport{Report: core.Report{Headline: "h"}}},
		{"the lead posting a handoff", core.AgentCaller{Agent: leadID}, core.PostHandoff{}},
		{"the operator posting a handoff", operator, core.PostHandoff{}},
		{"the operator posting a report", operator, core.PostReport{Report: core.Report{Headline: "h"}}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if r := w.command(c.caller, c.body); r.Refusal != core.RefuseNotPermitted {
				t.Fatalf("refusal = %q, want not permitted", r.Refusal)
			}
			if len(w.effects) != 0 {
				t.Fatalf("a refused command asked for %v", w.kinds())
			}
		})
	}
}

func TestAnAgentActsOnlyInItsOwnRun(t *testing.T) {
	w, _, leadA := started(t, 2, steers)
	other := w.command(operator, core.StartRun{Repository: "r", Task: "t", FanoutLimit: 2, Lead: lead, Brief: "b"})
	w.open(other.Agent, steers)
	caller := core.AgentCaller{Agent: leadA}
	if r := w.command(caller, core.SpawnAgent{Run: other.Run, Role: core.RoleWorker, Brief: "b"}); r.Refusal != core.RefuseRunNotFound {
		t.Fatalf("spawn in another run: refusal = %q", r.Refusal)
	}
	if r := w.command(caller, core.SendMessage{Agent: other.Agent, Text: "t"}); r.Refusal != core.RefuseAgentNotFound {
		t.Fatalf("message into another run: refusal = %q", r.Refusal)
	}
}

// Delivery is one table: what a message does depends on the agent's state and capability.
func TestDeliveryByAgentStateAndCapability(t *testing.T) {
	type setup func(t *testing.T) (*script, core.AgentID)
	cases := []struct {
		name  string
		setup setup
		want  core.Delivery
		then  []string
	}{
		{"running and capable steers", func(t *testing.T) (*script, core.AgentID) {
			w, run, leadID := started(t, 1, steers)
			return w, w.spawn(run, leadID, steers)
		}, core.DeliverySteered, []string{"Steer"}},
		{"running and not capable queues", func(t *testing.T) (*script, core.AgentID) {
			w, run, leadID := started(t, 1, noSteer)
			return w, w.spawn(run, leadID, noSteer)
		}, core.DeliveryQueued, nil},
		{"launching queues", func(t *testing.T) (*script, core.AgentID) {
			w, run, _ := started(t, 1, steers)
			r := w.command(operator, core.SpawnAgent{Run: run, Role: core.RoleWorker, Brief: "b"})
			return w, r.Agent
		}, core.DeliveryQueued, nil},
		{"idle starts a turn", func(t *testing.T) (*script, core.AgentID) {
			w, run, leadID := started(t, 1, steers)
			a := w.spawn(run, leadID, steers)
			w.endTurn(a, core.TurnCompleted)
			return w, a
		}, core.DeliveryNewTurn, []string{"StartTurn"}},
		{"ended and capable resumes", func(t *testing.T) (*script, core.AgentID) {
			w, run, leadID := started(t, 1, steers)
			a := w.spawn(run, leadID, steers)
			w.fact(a, core.SessionClosed{})
			return w, a
		}, core.DeliveryQueued, []string{"LaunchSession"}},
		{"lost and capable resumes", func(t *testing.T) (*script, core.AgentID) {
			w, run, leadID := started(t, 1, steers)
			a := w.spawn(run, leadID, steers)
			got, _ := w.s.Agent(a)
			w.step(core.SidecarLost{Session: core.Session{Agent: a, N: got.Session}})
			return w, a
		}, core.DeliveryQueued, []string{"LaunchSession"}},
		{"ended and not capable is rejected", func(t *testing.T) (*script, core.AgentID) {
			w, run, leadID := started(t, 1, bare)
			a := w.spawn(run, leadID, bare)
			w.fact(a, core.SessionClosed{})
			return w, a
		}, core.DeliveryRejected, nil},
		{"being stopped is rejected", func(t *testing.T) (*script, core.AgentID) {
			w, run, leadID := started(t, 1, steers)
			a := w.spawn(run, leadID, steers)
			w.command(operator, core.StopAgent{Agent: a})
			return w, a
		}, core.DeliveryRejected, nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			w, a := c.setup(t)
			r := w.command(operator, core.SendMessage{Agent: a, Text: "hello"})
			if r.Refusal != "" {
				t.Fatalf("refused: %s", r.Refusal)
			}
			if r.Delivery != c.want {
				t.Fatalf("delivery = %s, want %s", r.Delivery, c.want)
			}
			w.wantEffects(c.then...)
		})
	}
}

func TestAResumeOverTheFanOutLimitIsRejected(t *testing.T) {
	w, run, leadID := started(t, 1, steers)
	first := w.spawn(run, leadID, steers)
	w.fact(first, core.SessionClosed{})
	w.spawn(run, leadID, steers)
	r := w.command(operator, core.SendMessage{Agent: first, Text: "come back"})
	if r.Delivery != core.DeliveryRejected {
		t.Fatalf("delivery = %s, want rejected", r.Delivery)
	}
}

func TestAResumedSessionKeepsItsConversationAndFencesTheOldOne(t *testing.T) {
	w, run, leadID := started(t, 1, steers)
	a := w.spawn(run, leadID, steers)
	old := core.Session{Agent: a, N: 1}
	w.fact(a, core.SessionClosed{})
	w.command(operator, core.SendMessage{Agent: a, Text: "again"})
	launch := w.effects[0].(core.LaunchSession)
	if launch.ConversationID != "conv-"+string(a) || launch.Session.N != 2 || launch.Worktree == "" {
		t.Fatalf("resume = %+v", launch)
	}
	w.step(core.SidecarFact{Session: old, Offset: 99, Body: core.SessionFailed{Detail: "late"}})
	if len(w.events) != 0 {
		t.Fatalf("a fact from a replaced session recorded %v", w.events)
	}
}

// A steer too late for its turn comes back and takes the next turn, and the turn before it does
// not count as having delivered it.
func TestALateSteerStartsTheNextTurn(t *testing.T) {
	w, run, leadID := started(t, 1, steers)
	a := w.spawn(run, leadID, steers)
	r := w.command(operator, core.SendMessage{Agent: a, Text: "steer me"})
	w.endTurn(a, core.TurnCompleted)
	w.wantEffects()
	w.fact(a, core.SteerOutcome{Message: r.Message, Accepted: false})
	w.wantEffects("StartTurn")
	if got := w.turn(a).Message; got != r.Message {
		t.Fatalf("the next turn is for %s, want %s", got, r.Message)
	}
}

// A steer sent as a handed-off agent's turn ends may still come back refused, and then only a new
// turn can deliver it, so the session stays open until the steer is answered.
func TestAHandedOffAgentWaitsForASteerStillInFlight(t *testing.T) {
	w, run, leadID := started(t, 1, steers)
	a := w.spawn(run, leadID, steers)
	w.command(core.AgentCaller{Agent: a}, core.PostHandoff{Handoff: core.Handoff{Claim: "done"}})
	sent := w.command(operator, core.SendMessage{Agent: a, Text: "one more thing"})
	w.endTurn(a, core.TurnCompleted)
	w.wantEffects()
	w.fact(a, core.SteerOutcome{Message: sent.Message})
	w.wantEffects("StartTurn")
	if got := w.delivery(sent.Message); got != core.DeliveryNewTurn {
		t.Fatalf("delivery = %s", got)
	}
}

func TestALandedSteerIsConsumed(t *testing.T) {
	w, run, leadID := started(t, 1, steers)
	a := w.spawn(run, leadID, steers)
	r := w.command(operator, core.SendMessage{Agent: a, Text: "steer me"})
	w.fact(a, core.SteerOutcome{Message: r.Message, Accepted: true})
	if got := w.delivery(r.Message); got != core.DeliveryConsumed {
		t.Fatalf("delivery = %s", got)
	}
}

func TestAHandoffWakesTheLeadOnce(t *testing.T) {
	for _, caps := range []core.Capabilities{steers, noSteer} {
		t.Run(fmt.Sprintf("steers=%v", caps.Steers), func(t *testing.T) {
			w, run, leadID := started(t, 1, caps)
			a := w.spawn(run, leadID, caps)
			w.command(core.AgentCaller{Agent: a}, core.PostHandoff{Handoff: core.Handoff{Claim: "done"}})
			turn := w.turn(leadID)
			if turn.Trigger != core.TriggerHandoff {
				t.Fatalf("the lead woke for %s", turn.Trigger)
			}
			w.endTurn(leadID, core.TurnCompleted)
			w.endTurn(a, core.TurnCompleted)
			w.wantEffects("ArmTimer", "CloseSession")
			if _, ok := w.s.ActiveTurn(leadID); ok {
				t.Fatal("the lead was woken a second time")
			}
			w.fact(a, core.SessionClosed{})
			if _, ok := w.s.ActiveTurn(leadID); ok {
				t.Fatal("the end of a handed-off agent woke the lead")
			}
			if w.status(leadID) != core.AgentIdle {
				t.Fatalf("lead is %s, want idle", w.status(leadID))
			}
		})
	}
}

func TestASubagentThatStopsWithoutAHandoffWakesTheLead(t *testing.T) {
	ends := map[string]func(w *script, a core.AgentID){
		"idle":   func(w *script, a core.AgentID) { w.endTurn(a, core.TurnCompleted) },
		"failed": func(w *script, a core.AgentID) { w.fact(a, core.SessionFailed{Detail: "boom"}) },
		"closed": func(w *script, a core.AgentID) { w.fact(a, core.SessionClosed{}) },
		"lost":   func(w *script, a core.AgentID) { w.step(core.SidecarLost{Session: core.Session{Agent: a, N: 1}}) },
	}
	for name, end := range ends {
		t.Run(name, func(t *testing.T) {
			w, run, leadID := started(t, 1, steers)
			end(w, w.spawn(run, leadID, steers))
			if got := w.turn(leadID).Trigger; got != core.TriggerSubagentSettled {
				t.Fatalf("the lead woke for %s", got)
			}
		})
	}
}

func TestAFinishedJobWakesTheLeadOnceHoweverOftenItIsReported(t *testing.T) {
	w, run, leadID := started(t, 1, noSteer)
	update := core.JobUpdate{Job: "j1", Run: run, State: core.JobSucceeded, Detail: "ok"}
	w.step(core.JobUpdate{Job: "j1", Run: run, State: core.JobRunning})
	if len(w.events) != 0 {
		t.Fatalf("a running job recorded %v", w.events)
	}
	w.step(update)
	if got := w.turn(leadID).Trigger; got != core.TriggerJobFinished {
		t.Fatalf("the lead woke for %s", got)
	}
	w.step(update)
	if len(w.events) != 0 {
		t.Fatalf("a repeated job update recorded %v", w.events)
	}
	replayed := core.Replay(core.State{}, w.log)
	if _, events, _ := core.Decide(replayed, core.Input{At: w.now, Nonce: "n", Body: update}); len(events) != 0 {
		t.Fatalf("a job repeated after a restart recorded %v", events)
	}
}

func TestALostSidecarFailsItsTurnAndHoldsItsMessageForTheResume(t *testing.T) {
	w, _, leadID := started(t, 1, noSteer)
	r := w.command(operator, core.SendMessage{Agent: leadID, Text: "wake"})
	turn := w.turn(leadID)
	w.fact(leadID, core.ItemObserved{ID: "i1", Turn: turn.ID, Status: core.ItemRunning, Body: core.Assistant{Text: "hm"}})
	w.step(core.SidecarLost{Session: core.Session{Agent: leadID, N: 1}})
	w.wantEffects("LaunchSession")
	var item core.Item
	var ended core.Turn
	for _, e := range w.events {
		switch b := e.Body.(type) {
		case core.Item:
			item = b
		case core.Turn:
			ended = b
		}
	}
	if item.Status != core.ItemFailed || ended.Status != core.TurnFailed {
		t.Fatalf("item %+v and turn %+v after the loss", item, ended)
	}
	w.open(leadID, noSteer)
	if got := w.turn(leadID).Message; got != r.Message {
		t.Fatalf("the resumed lead took %s, want the message that was in flight", got)
	}
}

func TestALeadThatKeepsDyingFailsTheRun(t *testing.T) {
	w, run, leadID := started(t, 1, steers)
	worker := w.spawn(run, leadID, steers)
	for range core.MaxLeadResumes {
		a, _ := w.s.Agent(leadID)
		w.step(core.SidecarLost{Session: core.Session{Agent: leadID, N: a.Session}})
		w.wantEffects("LaunchSession")
		w.open(leadID, steers)
	}
	a, _ := w.s.Agent(leadID)
	w.step(core.SidecarLost{Session: core.Session{Agent: leadID, N: a.Session}})
	got, _ := w.s.Run(run)
	if got.Status != core.RunStopping || got.StopReason != core.StopLeadGone {
		t.Fatalf("run is %+v, want stopping because the lead is gone", got)
	}
	w.endTurn(worker, core.TurnInterrupted)
	w.fact(worker, core.SessionClosed{})
	if got, _ = w.s.Run(run); got.Status != core.RunFailed {
		t.Fatalf("run is %s, want failed", got.Status)
	}
}

func TestALeadThatCannotResumeFailsARunStillStarting(t *testing.T) {
	w := newScript(t)
	r := w.command(operator, core.StartRun{Repository: "r", Task: "t", Lead: lead, Brief: "b"})
	w.step(core.SidecarLost{Session: core.Session{Agent: r.Agent, N: 1}})
	if got, _ := w.s.Run(r.Run); got.Status != core.RunFailed {
		t.Fatalf("run is %s, want failed", got.Status)
	}
}

func TestAMessageStopsBeingRetriedAfterItsAttempts(t *testing.T) {
	w, _, leadID := started(t, 1, noSteer)
	r := w.command(operator, core.SendMessage{Agent: leadID, Text: "poison"})
	for range core.MaxDeliveryAttempts {
		if got := w.turn(leadID).Message; got != r.Message {
			t.Fatalf("the turn is for %s", got)
		}
		w.endTurn(leadID, core.TurnFailed)
	}
	if got := w.delivery(r.Message); got != core.DeliveryRejected {
		t.Fatalf("delivery = %s", got)
	}
	if _, ok := w.s.ActiveTurn(leadID); ok {
		t.Fatal("a turn started after the attempts ran out")
	}
}

func TestTheDeadlineStopsARunOnce(t *testing.T) {
	w := newScript(t)
	deadline := epoch.Add(time.Hour)
	r := w.command(operator, core.StartRun{Repository: "r", Task: "t", Deadline: deadline, Lead: lead, Brief: "b"})
	w.wantEffects("ArmTimer", "LaunchSession")
	timer := w.effects[0].(core.ArmTimer)
	if !timer.FireAt.Equal(deadline) {
		t.Fatalf("timer = %+v", timer)
	}
	w.open(r.Agent, steers)
	w.step(core.TimerFired{Timer: timer})
	if got, _ := w.s.Run(r.Run); got.Status != core.RunStopping || got.StopReason != core.StopDeadline {
		t.Fatalf("run is %+v", got)
	}
	w.step(core.TimerFired{Timer: timer})
	if len(w.events) != 0 {
		t.Fatalf("a repeated deadline recorded %v", w.events)
	}
}

func TestADeadlineInThePastIsRefused(t *testing.T) {
	w := newScript(t)
	r := w.command(operator, core.StartRun{Repository: "r", Task: "t", Deadline: epoch, Lead: lead, Brief: "b"})
	if r.Refusal != core.RefuseDeadlinePassed {
		t.Fatalf("refusal = %q", r.Refusal)
	}
}

func TestASessionThatNeverConfirmsAStopIsWrittenOff(t *testing.T) {
	w, run, leadID := started(t, 1, steers)
	a := w.spawn(run, leadID, steers)
	w.command(operator, core.StopAgent{Agent: a})
	w.wantEffects("ArmTimer", "Interrupt")
	grace := w.effects[0].(core.ArmTimer)
	w.step(core.TimerFired{Timer: grace})
	w.wantEffects("CloseSession", "StartTurn") // The lead hears of it.
	if close := w.effects[0].(core.CloseSession); !close.Force {
		t.Fatalf("close = %+v, want forced", close)
	}
	if w.status(a) != core.AgentLost {
		t.Fatalf("agent is %s, want lost", w.status(a))
	}
}

func TestAStaleStopGraceDoesNothing(t *testing.T) {
	w, run, leadID := started(t, 1, steers)
	a := w.spawn(run, leadID, steers)
	w.command(operator, core.StopAgent{Agent: a})
	grace := w.effects[0].(core.ArmTimer)
	w.endTurn(a, core.TurnInterrupted)
	w.fact(a, core.SessionClosed{})
	w.command(operator, core.SendMessage{Agent: a, Text: "back"})
	w.open(a, steers)
	w.step(core.TimerFired{Timer: grace})
	if len(w.events) != 0 {
		t.Fatalf("a grace timer of an earlier session recorded %v", w.events)
	}
}

func TestTheLeadIsStoppedByStoppingItsRun(t *testing.T) {
	w, _, leadID := started(t, 1, steers)
	if r := w.command(operator, core.StopAgent{Agent: leadID}); r.Refusal != core.RefuseAgentIsLead {
		t.Fatalf("refusal = %q", r.Refusal)
	}
}

func TestACommandAnswersEveryRetryAlike(t *testing.T) {
	w, run, _ := started(t, 2, steers)
	cmd := core.Command{ID: "spawn-1", Digest: "abc", Caller: operator, Body: core.SpawnAgent{Run: run, Role: core.RoleWorker, Brief: "b"}}
	w.step(cmd)
	first, _ := w.s.Recall(cmd.ID, cmd.Digest)
	agents := len(w.s.Agents(run))
	for range 3 {
		w.step(cmd)
		if len(w.events)+len(w.effects) != 0 {
			t.Fatalf("a retry produced %v and %v", w.events, w.effects)
		}
	}
	if got := len(w.s.Agents(run)); got != agents {
		t.Fatalf("retries spawned: %d agents, want %d", got, agents)
	}
	again, recall := core.Replay(core.State{}, w.log).Recall(cmd.ID, cmd.Digest)
	if recall != core.RecallRepeat || !reflect.DeepEqual(again, first) {
		t.Fatalf("after a restart the receipt is %+v (%s), want %+v", again, recall, first)
	}
	if _, recall := w.s.Recall(cmd.ID, "other"); recall != core.RecallConflict {
		t.Fatalf("one id on another command: %s", recall)
	}
}

func TestARefusalIsRememberedAcrossARestart(t *testing.T) {
	w := newScript(t)
	r := w.command(operator, core.StopRun{Run: "nowhere"})
	if r.Refusal != core.RefuseRunNotFound {
		t.Fatalf("refusal = %q", r.Refusal)
	}
	if got, recall := core.Replay(core.State{}, w.log).Recall(r.Command, r.Digest); recall != core.RecallRepeat || got != r {
		t.Fatalf("after a restart: %+v (%s)", got, recall)
	}
}

func TestItemsKeepTheirPlaceAsTheyChange(t *testing.T) {
	w, _, leadID := started(t, 0, steers)
	w.command(operator, core.SendMessage{Agent: leadID, Text: "go"})
	turn := w.turn(leadID)
	item := func(id core.ItemID, status core.ItemStatus) core.Item {
		w.fact(leadID, core.ItemObserved{ID: id, Turn: turn.ID, Status: status, Body: core.ToolCall{Tool: "bash"}})
		if len(w.events) != 1 {
			t.Fatalf("item %s recorded %v", id, w.events)
		}
		return w.events[0].Body.(core.Item)
	}
	first := item("i1", core.ItemRunning)
	second := item("i2", core.ItemRunning)
	if second.Ordinal != first.Ordinal+1 {
		t.Fatalf("ordinals %d then %d", first.Ordinal, second.Ordinal)
	}
	if got := item("i1", core.ItemFinished); got.Ordinal != first.Ordinal {
		t.Fatalf("an update moved item i1 to %d from %d", got.Ordinal, first.Ordinal)
	}
	w.fact(leadID, core.ItemObserved{ID: "i1", Turn: turn.ID, Status: core.ItemRunning, Body: core.ToolCall{Tool: "bash"}})
	if len(w.events) != 0 {
		t.Fatal("a finished item changed")
	}
	w.fact(leadID, core.ItemObserved{ID: "i3", Turn: "another", Status: core.ItemRunning, Body: core.ToolCall{Tool: "bash"}})
	if len(w.events) != 0 {
		t.Fatal("an item of another turn was recorded")
	}
}

func TestTheCompletionBarrierClosesItemsBeforeTheTurn(t *testing.T) {
	w, _, leadID := started(t, 0, steers)
	w.command(operator, core.SendMessage{Agent: leadID, Text: "go"})
	turn := w.turn(leadID)
	w.fact(leadID, core.ItemObserved{ID: "i1", Turn: turn.ID, Status: core.ItemRunning, Body: core.Assistant{Text: "partial"}})
	w.endTurn(leadID, core.TurnCompleted)
	closedItem, closedTurn := -1, -1
	for i, e := range w.events {
		switch b := e.Body.(type) {
		case core.Item:
			if b.Status != core.ItemFinished || b.Body != (core.Assistant{Text: "partial"}) {
				t.Fatalf("the barrier wrote %+v", b)
			}
			closedItem = i
		case core.Turn:
			closedTurn = i
		}
	}
	if closedItem < 0 || closedTurn < closedItem {
		t.Fatalf("item closed at %d, turn at %d", closedItem, closedTurn)
	}
}

func TestUsageMergesWithoutInventingZeros(t *testing.T) {
	w, _, leadID := started(t, 0, steers)
	w.command(operator, core.SendMessage{Agent: leadID, Text: "go"})
	turn := w.turn(leadID)
	in, out := uint64(5), uint64(7)
	w.fact(leadID, core.UsageReported{Turn: turn.ID, Usage: core.Usage{InputTokens: &in}})
	w.fact(leadID, core.UsageReported{Turn: turn.ID, Usage: core.Usage{OutputTokens: &out}})
	got := w.turn(leadID).Usage
	if got.InputTokens == nil || *got.InputTokens != 5 || got.OutputTokens == nil || *got.OutputTokens != 7 || got.CacheReadTokens != nil {
		t.Fatalf("usage = %+v", got)
	}
}

func TestAFactAtACommittedOffsetIsDropped(t *testing.T) {
	w, _, leadID := started(t, 0, steers)
	session := core.Session{Agent: leadID, N: 1}
	committed := w.s.CommittedOffset(session)
	w.step(core.SidecarFact{Session: session, Offset: committed, Body: core.SessionFailed{Detail: "again"}})
	if len(w.events) != 0 {
		t.Fatalf("a repeated fact recorded %v", w.events)
	}
	if got := core.Replay(core.State{}, w.log).CommittedOffset(session); got != committed {
		t.Fatalf("replay gives offset %d, want %d", got, committed)
	}
}
