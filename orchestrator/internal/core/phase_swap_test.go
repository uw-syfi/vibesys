package core_test

import (
	"math/rand/v2"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// The swap from setup to optimize, scenario by scenario and then under crashes at every point: one
// decision commits the whole of it, the lead's turn is never cut, and the lead that takes over reads
// the brief first and once.

// verified is what the shell attests when every check of the task holds.
func verified(task string) core.SetupVerdict {
	return core.SetupVerdict{
		Task: task, TaskVersion: "v1", BaseCommit: strings.Repeat("a", 40),
		Facts: core.StartFacts{Card: &core.TaskCard{Task: task, Heading: "a task"}},
	}
}

func completion(run core.RunID, task string) core.CompleteSetup {
	return core.CompleteSetup{
		Run: run, Task: task, MaxCostUSD: 5, MaxTime: time.Hour, FanoutLimit: 3, Lead: lead, Brief: "be quick",
		Verified: verified(task),
	}
}

// completeSetup has the operator complete setup and returns the receipt.
func (w *script) completeSetup(c core.CompleteSetup) core.Receipt {
	w.t.Helper()
	return w.command(operator, c)
}

func (w *script) messagesTo(a core.AgentID) []core.Message {
	var out []core.Message
	seen := map[core.MessageID]bool{}
	for _, e := range w.log {
		if m, ok := e.Body.(core.Message); ok && m.To == a && !seen[m.ID] {
			seen[m.ID] = true
			out = append(out, m)
		}
	}
	return out
}

func countOf[T core.MessageBody](w *script, a core.AgentID) int {
	n := 0
	for _, m := range w.messagesTo(a) {
		if _, ok := m.Body.(T); ok {
			n++
		}
	}
	return n
}

func TestCompletingSetupCommitsTheWholeSwapInOneDecision(t *testing.T) {
	for _, spec := range []core.AgentSpec{lead, leadOn} {
		w := newScript(t)
		run, leadID := w.beginSetup("repo", steers)
		w.declare(core.AgentCaller{Agent: leadID}, run, "kv")
		before := w.runOf(run)
		w.send(leadID, "go")
		spent := 1.25
		w.fact(leadID, core.TurnEnded{Turn: w.turn(leadID).ID, Status: core.TurnCompleted, Usage: core.Usage{CostUSD: &spent}})

		c := completion(run, "kv")
		c.Lead = spec
		c.Verified.Facts.EvalPlace = "host-a"
		c.EvalPlace = "host-a"
		r := w.completeSetup(c)
		if r.Refusal != "" || r.Run != run || r.Agent != leadID || r.Message == "" || r.Delivery != core.DeliveryQueued {
			t.Fatalf("receipt %+v", r)
		}
		decided := w.now
		got := w.runOf(run)
		want := spec
		if got.Phase != core.PhaseOptimize || got.Task != "kv" || got.SetupTask != "" || got.TaskCard == nil || got.TaskCard.Task != "kv" ||
			got.EvalPlace != "host-a" || got.FanoutLimit != 3 || got.MaxCostUSD != 5 || got.SummaryEvery != core.DefaultSummaryEvery ||
			!got.OptimizeStartedAt.Equal(decided) || !got.LastSummaryAt.Equal(decided) || !got.Deadline.Equal(decided.Add(time.Hour)) ||
			got.SetupUsage == nil || *got.SetupUsage.CostUSD != spent || got.PendingLead == nil || *got.PendingLead != want ||
			!got.StalledSince.IsZero() || got.JobTool != before.JobTool {
			t.Fatalf("spec %v: after the swap %+v", spec, got)
		}
		if !slices.Contains(got.Allowed, core.AgentChoice{Harness: spec.Harness, Model: spec.Model}) {
			t.Fatalf("the lead's choice is not allowed: %v", got.Allowed)
		}
		var timers int
		for _, e := range w.effects {
			if a, ok := e.(core.ArmTimer); ok && a.Kind == core.TimerRunDeadline && a.FireAt.Equal(got.Deadline) {
				timers++
			}
		}
		if timers != 1 {
			t.Fatalf("%d deadline timers armed, effects %v", timers, w.kinds())
		}
		// The brief is queued, with the old session closing to make way, and no handover is written.
		briefs := w.messagesTo(leadID)
		last := briefs[len(briefs)-1]
		if b, ok := last.Body.(core.LeadBrief); !ok || b != (core.LeadBrief{Task: "kv", FanoutLimit: 3, Steering: "be quick"}) || last.ID != r.Message {
			t.Fatalf("the last message to the lead is %+v", last)
		}
		if w.status(leadID) != core.AgentIdle || !slices.Contains(w.kinds(), "CloseSession") {
			t.Fatalf("the lead at rest is not being closed: %v", w.kinds())
		}

		w.fact(leadID, core.SessionClosed{})
		launch, ok := w.launched()
		if !ok || launch.Spec != spec || launch.ResumeToken != "" {
			t.Fatalf("launched %+v, want a fresh session on %v", launch, spec)
		}
		if got := w.runOf(run); got.PendingLead != nil {
			t.Fatalf("the swap still waits: %+v", got.PendingLead)
		}
		w.open(leadID, steers)
		if turn := w.turn(leadID); turn.Trigger != core.TriggerBrief || turn.Message != r.Message {
			t.Fatalf("the new lead's first turn is %+v, want the brief", turn)
		}
		w.endTurn(leadID, core.TurnCompleted)
		if n := countOf[core.LeadHandover](w, leadID); n != 0 {
			t.Fatalf("%d handovers for a swap", n)
		}
		// The new lead is quiet with nothing to do: the optimize run stalls and is reminded as any.
		if run := w.runOf(run); run.StalledSince.IsZero() {
			t.Fatalf("an optimize lead at rest with nothing to do is stalled: %+v", run)
		}
	}
}

// A completion that changes the lead to the spec it has is no change of lead for the timeline, and the
// new session is a fresh one all the same.
func TestCompletingSetupToTheSameLeadWritesNoLeadChange(t *testing.T) {
	w := newScript(t)
	run, leadID := w.beginSetup("repo", steers)
	w.completeSetup(completion(run, "kv"))
	w.fact(leadID, core.SessionClosed{})
	for _, e := range w.log {
		if u, ok := e.Body.(core.RunSettingsUpdated); ok {
			t.Fatalf("a lead change was written: %+v", u)
		}
	}
	if a, _ := w.s.Agent(leadID); a.Session != 2 || a.Status != core.AgentLaunching {
		t.Fatalf("the lead %+v", a)
	}
}

func TestCompletingSetupRefusesWhatIsNotReadyOrNotSetup(t *testing.T) {
	t.Run("unmet checks", func(t *testing.T) {
		w := newScript(t)
		run, leadID := w.beginSetup("repo", steers)
		c := completion(run, "kv")
		c.Verified.Unmet = []core.SetupCheckID{core.CheckBrokenFails, core.CheckNumberStable}
		r := w.completeSetup(c)
		if r.Refusal != core.RefuseSetupIncomplete || !slices.Equal(r.UnmetChecks, c.Verified.Unmet) || r.Run != run {
			t.Fatalf("receipt %+v", r)
		}
		if got := w.runOf(run); got.Phase != core.PhaseSetup || got.PendingLead != nil || w.status(leadID) != core.AgentIdle {
			t.Fatalf("a refused completion changed the run: %+v", got)
		}
	})
	t.Run("not verified, or verified for another task", func(t *testing.T) {
		w := newScript(t)
		run, _ := w.beginSetup("repo", steers)
		for _, v := range []core.SetupVerdict{{}, verified("other")} {
			c := completion(run, "kv")
			c.Verified = v
			if r := w.completeSetup(c); r.Refusal != core.RefuseSetupIncomplete || !slices.Equal(r.UnmetChecks, core.SetupChecks) {
				t.Fatalf("verdict %+v: receipt %+v", v, r)
			}
		}
	})
	t.Run("a place the shell refused", func(t *testing.T) {
		w := newScript(t)
		run, _ := w.beginSetup("repo", steers)
		c := completion(run, "kv")
		c.Verified.Facts = core.StartFacts{Refused: &core.PlaceRefusal{Refusal: core.RefuseEvalPlaceMisfit, Places: []string{"host-b"}}}
		if r := w.completeSetup(c); r.Refusal != core.RefuseEvalPlaceMisfit || !slices.Equal(r.EvalPlaces, []string{"host-b"}) {
			t.Fatalf("receipt %+v", r)
		}
		c.Verified.Facts = core.StartFacts{EvalPlace: "host-a"}
		c.EvalPlace = "host-c"
		if r := w.completeSetup(c); r.Refusal != core.RefuseEvalPlaceNotFound {
			t.Fatalf("a place the shell did not resolve: %+v", r)
		}
		if w.runOf(run).Phase != core.PhaseSetup {
			t.Fatal("a refused completion left setup")
		}
	})
	t.Run("not in setup", func(t *testing.T) {
		w := newScript(t)
		opt, _ := w.startRunIn("repo-a", "task", 2, steers)
		done, doneLead := w.beginSetup("repo-b", steers)
		w.completeSetup(completion(done, "kv"))
		old, _ := w.startRunIn("repo-c", "task", 2, steers)
		stopped, _ := w.beginSetup("repo-d", steers)
		w.command(operator, core.StopRun{Run: stopped})
		w.forgetPhasesOf(old)
		_ = doneLead
		for _, run := range []core.RunID{opt, done, old} {
			if r := w.completeSetup(completion(run, "kv")); r.Refusal != core.RefuseNotInSetup || r.Run != run {
				t.Fatalf("run %s: %+v", run, r)
			}
		}
		if r := w.completeSetup(completion(stopped, "kv")); r.Refusal != core.RefuseRunNotRunning {
			t.Fatalf("a stopped setup: %+v", r)
		}
		if r := w.completeSetup(completion("nope", "kv")); r.Refusal != core.RefuseRunNotFound {
			t.Fatalf("no run: %+v", r)
		}
		if r := w.command(core.AgentCaller{Agent: doneLead}, completion(done, "kv")); r.Refusal != core.RefuseNotPermitted {
			t.Fatalf("an agent completing: %+v", r)
		}
	})
}

func (w *script) forgetPhasesOf(id core.RunID) {
	run := w.runOf(id)
	run.Phase = ""
	w.s = core.Replay(w.s, []core.Event{{Run: id, At: w.now, Body: run}})
}

// A retry is answered with the first receipt whatever the shell now says: the verdict is no part of the
// command's digest, so neither a later pass turns a refusal into a start nor a later fail undoes one.
func TestARetriedCompletionGetsItsFirstReceipt(t *testing.T) {
	for _, firstOK := range []bool{true, false} {
		w := newScript(t)
		run, _ := w.beginSetup("repo", steers)
		c := completion(run, "kv")
		if !firstOK {
			c.Verified.Unmet = []core.SetupCheckID{core.CheckBenchmark}
		}
		w.steps++
		in := core.Command{ID: "retry-me", Digest: "d", Caller: operator, Body: c}
		w.step(in)
		first, _ := w.s.Recall("retry-me", "d")
		after := w.s
		if firstOK {
			c.Verified.Unmet = []core.SetupCheckID{core.CheckBenchmark}
		} else {
			c.Verified.Unmet = nil
		}
		in.Body = c
		w.step(in)
		got, recall := w.s.Recall("retry-me", "d")
		if recall != core.RecallRepeat || got.Refusal != first.Refusal || len(w.events) != 0 || w.runOf(run).Phase != after.Runs()[0].Phase {
			t.Fatalf("firstOK=%v: retry gave %+v (%v), %d events; first was %+v", firstOK, got, recall, len(w.events), first)
		}
	}
}

// A start while the lead is mid-turn waits for it to rest: nothing interrupts the turn, the turn's spend
// is the setup's, and the brief is not read by the session that is going.
func TestACompletionDuringATurnNeverCutsItAndTheTurnIsSetupSpend(t *testing.T) {
	for _, caps := range []core.Capabilities{steers, noSteer} {
		w := newScript(t)
		run, leadID := w.beginSetup("repo", caps)
		w.send(leadID, "work")
		turn := w.turn(leadID)
		r := w.completeSetup(completion(run, "kv"))
		if r.Refusal != "" {
			t.Fatalf("%+v", r)
		}
		for _, e := range w.effects {
			switch e.(type) {
			case core.Interrupt, core.CloseSession, core.Steer:
				t.Fatalf("the swap did %T to a lead in a turn", e)
			}
		}
		if a, _ := w.s.Agent(leadID); a.Status != core.AgentActive || a.Stop != core.StopNone || w.turn(leadID).ID != turn.ID {
			t.Fatalf("the turn was cut: %+v", a)
		}
		if got := w.runOf(run); got.PendingLead == nil || got.Phase != core.PhaseOptimize {
			t.Fatalf("the pending start is not on the run: %+v", got)
		}
		// Mail the operator sends while it waits is not lost, and reads after the brief.
		later := w.send(leadID, "later")

		spent := 2.5
		w.fact(leadID, core.TurnEnded{Turn: turn.ID, Status: core.TurnCompleted, Usage: core.Usage{CostUSD: &spent}})
		if later.Delivery == core.DeliverySteered {
			// A lead that rests with a steer unanswered waits for the answer before it is replaced.
			w.fact(leadID, core.SteerOutcome{Message: later.Message, Accepted: true})
		}
		if !slices.Contains(w.kinds(), "CloseSession") {
			t.Fatalf("the lead at rest after its turn is not closed: %v", w.kinds())
		}
		got := w.runOf(run)
		if got.SetupUsage == nil || *got.SetupUsage.CostUSD != spent {
			t.Fatalf("the turn's spend is not setup spend: %+v", got.SetupUsage)
		}
		if opt := got.Usage.Minus(*got.SetupUsage); opt.CostUSD == nil || *opt.CostUSD != 0 {
			t.Fatalf("optimize spend %v, want none", opt.CostUSD)
		}
		w.fact(leadID, core.SessionClosed{})
		w.open(leadID, caps)
		if m := w.s.InTransit(); len(m) == 0 {
			t.Fatal("nothing queued for the new session")
		}
		if got := w.turn(leadID); got.Trigger != core.TriggerBrief {
			t.Fatalf("the new session's first turn: %+v", got)
		}
	}
}

// A switch of lead made during setup and not yet read, then a completion: the next lead is told by the
// brief alone, once.
func TestACompletionAfterASwitchOfLeadInSetupReplacesTheUnreadHandover(t *testing.T) {
	w := newScript(t)
	run, leadID := w.beginSetup("repo", noSteer)
	w.update(run, core.UpdateRun{Lead: &leadOn})
	w.fact(leadID, core.SessionClosed{})
	var handover core.LeadHandover
	for _, m := range w.messagesTo(leadID) {
		if h, ok := m.Body.(core.LeadHandover); ok {
			handover = h
		}
	}
	if handover.Phase != core.PhaseSetup || handover.Previous != lead {
		t.Fatalf("a switch in setup hands over %+v, want phase setup", handover)
	}
	w.completeSetup(completion(run, "kv"))
	// The session that has not yet read its handover is replaced before it does.
	w.drive(leadID, noSteer)
	if a, _ := w.s.Agent(leadID); a.Session != 3 {
		t.Fatalf("the lead is on session %d, want the fresh third", a.Session)
	}
	if n := w.turnsOn(leadID, true); n != 1 {
		t.Fatalf("%d turns on the brief", n)
	}
	if n := w.turnsOn(leadID, false); n != 0 {
		t.Fatalf("%d turns on a handover", n)
	}
	for _, m := range w.messagesTo(leadID) {
		if _, ok := m.Body.(core.LeadHandover); ok && w.delivery(m.ID) != core.DeliveryConsumed {
			t.Fatalf("a handover is left behind the brief: %+v (%s)", m, w.delivery(m.ID))
		}
	}
}

// drive plays the lead's session to rest: it does what the session is due for until it is due for nothing.
func (w *script) drive(leadID core.AgentID, caps core.Capabilities) {
	w.t.Helper()
	for range 40 {
		before := len(w.log)
		w.drive1(leadID, caps)
		if len(w.log) == before {
			return
		}
	}
	w.t.Fatal("the lead never came to rest")
}

// turnsOn counts the turns started on a lead brief, or on a lead handover.
func (w *script) turnsOn(a core.AgentID, brief bool) int {
	ids := map[core.MessageID]bool{}
	for _, m := range w.messagesTo(a) {
		_, isBrief := m.Body.(core.LeadBrief)
		_, isHandover := m.Body.(core.LeadHandover)
		if brief && isBrief || !brief && isHandover {
			ids[m.ID] = true
		}
	}
	seen := map[core.TurnID]bool{}
	for _, e := range w.log {
		if turn, ok := e.Body.(core.Turn); ok && turn.Agent == a && ids[turn.Message] && !seen[turn.ID] {
			seen[turn.ID] = true
		}
	}
	return len(seen)
}

// The swap holds up under a crash at every point of it: from any prefix of the log, the next inputs
// recover into exactly one fresh lead session on the wanted spec, which reads the brief first and once,
// with the setup's usage and the deadline as the decision wrote them, and no handover. No state on the way
// is an optimize run with no live lead and none to come.
func TestACrashDuringTheSwapRecoversIntoOneFreshLeadThatReadsTheBriefOnce(t *testing.T) {
	for seed := range uint64(200) {
		r := rand.New(rand.NewPCG(seed, seed+77))
		caps := []core.Capabilities{steers, noSteer}[r.IntN(2)]
		spec := []core.AgentSpec{lead, leadOn}[r.IntN(2)]
		midTurn := r.IntN(2) == 0
		switched := r.IntN(3) == 0
		mails := r.IntN(3)
		crashAt := r.IntN(10)

		w := newScript(t)
		run, leadID := w.beginSetup("repo", caps)
		if switched {
			w.update(run, core.UpdateRun{Lead: &leadOn})
			w.drive(leadID, caps)
		}
		if midTurn {
			w.send(leadID, "work")
		}
		step := 0
		crash := func() {
			a, _ := w.s.Agent(leadID)
			got := w.runOf(run)
			if got.Phase == core.PhaseOptimize && got.Status.Live() && a.Status.Over() && got.PendingLead == nil {
				t.Fatalf("seed %d: optimize with no live lead and none pending: %+v", seed, a)
			}
			if step == crashAt {
				w.s = recovered(t, w.log)
			}
			step++
		}
		crash()
		c := completion(run, "kv")
		c.Lead = spec
		if r := w.completeSetup(c); r.Refusal != "" {
			t.Fatalf("seed %d: %+v", seed, r)
		}
		decided := w.runOf(run)
		for range mails {
			crash()
			w.send(leadID, "mail")
		}
		for range 40 {
			crash()
			a, _ := w.s.Agent(leadID)
			if a.Status == core.AgentIdle && a.Stop == core.StopNone && a.Session > 1 && len(w.s.InTransit()) == 0 {
				break
			}
			before := len(w.log)
			w.drive1(leadID, caps)
			if len(w.log) == before {
				break
			}
		}
		got := w.runOf(run)
		a, _ := w.s.Agent(leadID)
		if got.PendingLead != nil || a.Spec != spec || got.Phase != core.PhaseOptimize {
			t.Fatalf("seed %d: run %+v, lead %+v", seed, got, a)
		}
		if !got.Deadline.Equal(decided.Deadline) || !reflect.DeepEqual(got.SetupUsage, decided.SetupUsage) {
			t.Fatalf("seed %d: deadline or setup usage moved: %+v from %+v", seed, got, decided)
		}
		if n := w.optimizeBriefTurns(leadID, decided); n != 1 {
			t.Fatalf("seed %d: the optimize brief was read %d times", seed, n)
		}
		if first := w.firstTurnOfSession(leadID, a.Session); first.Trigger != core.TriggerBrief {
			t.Fatalf("seed %d: the fresh session began with %+v", seed, first)
		}
		for _, m := range w.messagesTo(leadID) {
			if _, ok := m.Body.(core.LeadHandover); ok && m.At.After(decided.OptimizeStartedAt) {
				t.Fatalf("seed %d: a handover was written for a swap", seed)
			}
		}
		if v := w.s.Violations(); len(v) > 0 {
			t.Fatalf("seed %d: %v", seed, v)
		}
	}
}

// drive1 does the one thing the lead's session is due for, and nothing when it is due for none.
func (w *script) drive1(leadID core.AgentID, caps core.Capabilities) {
	w.t.Helper()
	a, _ := w.s.Agent(leadID)
	for _, m := range w.s.InTransit() {
		if m.To == leadID && m.Delivery == core.DeliverySteered {
			w.fact(leadID, core.SteerOutcome{Message: m.ID, Accepted: true})
			return
		}
	}
	switch {
	case a.Status.Over():
	case a.Status == core.AgentLaunching:
		w.open(leadID, caps)
	case a.Status == core.AgentActive && a.Stop == core.StopInterrupting:
		w.endTurn(leadID, core.TurnInterrupted)
	case a.Status == core.AgentActive:
		w.endTurn(leadID, core.TurnCompleted)
	case a.Stop == core.StopClosing:
		w.fact(leadID, core.SessionClosed{})
	}
}

// optimizeBriefTurns counts the turns started on the brief the swap sent, found as the LeadBrief message
// written at or after the decision.
func (w *script) optimizeBriefTurns(a core.AgentID, decided core.Run) int {
	var id core.MessageID
	for _, m := range w.messagesTo(a) {
		if _, ok := m.Body.(core.LeadBrief); ok && !m.At.Before(decided.OptimizeStartedAt) {
			id = m.ID
		}
	}
	seen := map[core.TurnID]bool{}
	for _, e := range w.log {
		if turn, ok := e.Body.(core.Turn); ok && turn.Agent == a && turn.Message == id {
			seen[turn.ID] = true
		}
	}
	return len(seen)
}

// firstTurnOfSession is the first turn the lead started after its session n opened, found in the log.
func (w *script) firstTurnOfSession(a core.AgentID, n uint32) core.Turn {
	opened := false
	for _, e := range w.log {
		switch b := e.Body.(type) {
		case core.Agent:
			if b.ID == a && b.Session == n {
				opened = true
			}
		case core.Turn:
			if opened && b.Agent == a {
				return b
			}
		}
	}
	w.t.Fatalf("session %d of %s started no turn", n, a)
	return core.Turn{}
}
