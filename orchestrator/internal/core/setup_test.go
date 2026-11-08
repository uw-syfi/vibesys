package core_test

import (
	"math/rand/v2"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// The setup phase in the core, scenario by scenario and then over any order of events: what a run in
// setup never does (spawn, stall, end on a final report, reach a limit, ask for a report), what a name
// declared by its lead does, how its spend is kept apart from the run's, and that a run recorded before
// phases is still an optimize run.

// beginSetup starts a run in setup in a repository and rests its lead after the setup brief.
func (w *script) beginSetup(repository string, caps core.Capabilities) (core.RunID, core.AgentID) {
	w.t.Helper()
	r := w.command(operator, core.BeginSetup{Repository: repository, Lead: lead, Brief: "make a task"})
	if r.Refusal != "" {
		w.t.Fatalf("begin setup refused: %+v", r)
	}
	w.wantEffects("LaunchSession", "ArmTimer")
	w.open(r.Agent, caps)
	if got := w.turn(r.Agent).Trigger; got != core.TriggerBrief {
		w.t.Fatalf("the first turn's trigger = %s", got)
	}
	w.endTurn(r.Agent, core.TurnCompleted)
	return r.Run, r.Agent
}

func (w *script) declare(by core.Caller, run core.RunID, task string) core.Receipt {
	w.t.Helper()
	return w.command(by, core.DeclareTask{Run: run, Task: task})
}

// runEvents counts the Run events the last step wrote.
func (w *script) runEvents() int {
	n := 0
	for _, e := range w.events {
		if _, ok := e.Body.(core.Run); ok {
			n++
		}
	}
	return n
}

// leaveSetup does what completing setup will do to the run, by writing the run as it then is: the
// completion is a later item, and the accounting is read from the run it leaves.
func (w *script) leaveSetup(id core.RunID, task string, maxCost float64) {
	w.t.Helper()
	run := w.runOf(id)
	setup := run.Usage
	run.Phase, run.Task, run.SetupTask = core.PhaseOptimize, task, ""
	run.OptimizeStartedAt, run.SetupUsage, run.MaxCostUSD = w.now, &setup, maxCost
	if a, ok := w.s.Agent(run.Lead); ok && a.Status.Resting() && !run.FinalReported {
		// A lead at rest with nothing to do is a stall in an optimize run, which the completion marks.
		run.StalledSince = w.now
	}
	w.s = core.Replay(w.s, []core.Event{{Run: id, At: w.now, Body: run}})
	if v := w.s.Violations(); len(v) > 0 {
		w.t.Fatalf("after leaving setup: %v", v)
	}
}

func TestARunStartsInItsPhaseAndASetupRunHasNoTask(t *testing.T) {
	w := newScript(t)
	opt, _ := w.startRunIn("repo-a", "task", 2, steers)
	setup, _ := w.beginSetup("repo-b", steers)

	if got := w.runOf(opt); got.Phase != core.PhaseOptimize || got.Task != "task" {
		t.Fatalf("a start: phase %q, task %q", got.Phase, got.Task)
	}
	got := w.runOf(setup)
	if got.Phase != core.PhaseSetup || got.Task != "" || got.SetupTask != "" || got.Status != core.RunRunning ||
		!got.Deadline.IsZero() || got.MaxCostUSD != 0 || got.TaskCard != nil {
		t.Fatalf("a setup run: %+v", got)
	}
	var brief []core.MessageBody
	for _, e := range w.log {
		if m, ok := e.Body.(core.Message); ok && m.Run == setup {
			brief = append(brief, m.Body)
		}
	}
	if want := (core.SetupBrief{Repository: "repo-b", Steering: "make a task"}); len(brief) == 0 || brief[0] != want {
		t.Fatalf("the setup lead's first message is %v, want %v", brief, want)
	}
}

// A setup run holds its repository like any other, and a repository held by one refuses the next.
func TestASetupRunHoldsItsRepositoryAndStopsByTheGenericPath(t *testing.T) {
	w := newScript(t)
	run, leadID := w.beginSetup("repo", steers)

	if r := w.command(operator, core.StartRun{Repository: "repo", Task: "t", Lead: lead}); r.Refusal != core.RefuseRepositoryInUse || r.Run != run {
		t.Fatalf("a start in the held repository: %+v", r)
	}
	if r := w.command(operator, core.BeginSetup{Repository: "repo", Lead: lead}); r.Refusal != core.RefuseRepositoryInUse || r.Run != run {
		t.Fatalf("a second setup in the held repository: %+v", r)
	}
	if listed := w.s.Runs(); len(listed) != 1 || listed[0].ID != run {
		t.Fatalf("listed %v", listed)
	}
	if r := w.command(operator, core.StopRun{Run: run}); r.Refusal != "" {
		t.Fatalf("stop refused: %+v", r)
	}
	w.fact(leadID, core.SessionClosed{})
	if got := w.runOf(run); got.Status != core.RunEnded || got.StopReason != core.StopOperator {
		t.Fatalf("after the stop: %+v", got)
	}
}

func TestDeclareTaskIsTheLeadsInSetupAndTheLatestNameWins(t *testing.T) {
	w := newScript(t)
	run, leadID := w.beginSetup("repo", steers)
	byLead := core.AgentCaller{Agent: leadID}

	if r := w.declare(operator, run, "kv"); r.Refusal != core.RefuseNotPermitted || w.runEvents() != 0 {
		t.Fatalf("the operator declaring: %+v", r)
	}
	if r := w.declare(byLead, run, "kv"); r.Refusal != "" || r.Run != run {
		t.Fatalf("the lead declaring: %+v", r)
	}
	if got := w.runOf(run); got.SetupTask != "kv" || got.Task != "" {
		t.Fatalf("after the first name: setup task %q, task %q", got.SetupTask, got.Task)
	}

	// The same name is no event but its receipt; another name replaces it.
	w.declare(byLead, run, "kv")
	if w.runEvents() != 0 {
		t.Fatalf("a repeated name wrote %d run events", w.runEvents())
	}
	w.declare(byLead, run, "kv2")
	if got := w.runOf(run); got.SetupTask != "kv2" || got.Task != "" || w.runEvents() != 1 {
		t.Fatalf("after the second name: %+v with %d run events", got, w.runEvents())
	}

	other, otherLead := w.beginSetup("repo-2", steers)
	if r := w.declare(byLead, other, "x"); r.Refusal != core.RefuseRunNotFound {
		t.Fatalf("a lead declaring in another run: %+v", r)
	}
	if r := w.declare(operator, "nope", "x"); r.Refusal != core.RefuseNotPermitted {
		t.Fatalf("the operator, in a run that is not there: %+v", r)
	}
	if r := w.declare(core.AgentCaller{Agent: otherLead}, "nope", "x"); r.Refusal != core.RefuseRunNotFound {
		t.Fatalf("a lead, in a run that is not there: %+v", r)
	}
	_ = other
}

func TestDeclareTaskRefusesARunThatIsNotInSetupOrNotLive(t *testing.T) {
	w := newScript(t)
	opt, optLead := w.startRunIn("repo-a", "task", 1, steers)
	if r := w.declare(core.AgentCaller{Agent: optLead}, opt, "x"); r.Refusal != core.RefuseNotInSetup {
		t.Fatalf("an optimize run: %+v", r)
	}
	// A run recorded before phases is an optimize run.
	w.forgetPhases()
	if r := w.declare(core.AgentCaller{Agent: optLead}, opt, "x"); r.Refusal != core.RefuseNotInSetup {
		t.Fatalf("a run with no phase: %+v", r)
	}

	setup, setupLead := w.beginSetup("repo-b", steers)
	w.command(operator, core.StopRun{Run: setup})
	if r := w.declare(core.AgentCaller{Agent: setupLead}, setup, "x"); r.Refusal != core.RefuseNotPermitted && r.Refusal != core.RefuseRunNotRunning {
		t.Fatalf("a stopping run: %+v", r)
	}
}

// forgetPhases rewrites every run of the script with an empty phase, as a run recorded before phases
// is held.
func (w *script) forgetPhases() {
	var events []core.Event
	for _, run := range w.s.Runs() {
		run.Phase = ""
		events = append(events, core.Event{Run: run.ID, At: w.now, Body: run})
	}
	w.s = core.Replay(w.s, events)
}

func TestASpawnInSetupIsRefusedWhateverTheFanoutSays(t *testing.T) {
	w := newScript(t)
	run, leadID := w.beginSetup("repo", steers)
	// Whatever the fan-out says: a run holding one that allows many (as a log might) still refuses.
	got := w.runOf(run)
	got.FanoutLimit = 5
	w.s = core.Replay(w.s, []core.Event{{Run: run, At: w.now, Body: got}})

	for _, caller := range []core.Caller{core.AgentCaller{Agent: leadID}, operator} {
		r := w.command(caller, core.SpawnAgent{Run: run, Role: core.RoleWorker, Brief: "work"})
		if r.Refusal != core.RefuseNotPermitted || r.Agent != "" {
			t.Fatalf("a spawn in setup by %T: %+v", caller, r)
		}
	}
	if agents := w.s.Agents(run); len(agents) != 1 {
		t.Fatalf("agents %v, want only the lead", agents)
	}
	// Nor may the operator raise the fan-out to make room for one.
	if r := w.update(run, core.UpdateRun{Fanout: fanout(3)}); r.Refusal != core.RefuseNotPermitted {
		t.Fatalf("a fan-out in setup: %+v", r)
	}
}

func TestAFinalReportInSetupEndsNothingAndASetupRunNeverStalls(t *testing.T) {
	w := newScript(t)
	run, leadID := w.beginSetup("repo", steers)
	w.send(leadID, "go on")
	w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: core.ReportFinal, Headline: "done"}})
	w.endTurn(leadID, core.TurnCompleted)

	got := w.runOf(run)
	if got.Status != core.RunRunning || got.FinalReported || !got.StalledSince.IsZero() {
		t.Fatalf("after a final report and a quiet lead: %+v", got)
	}
	if _, ok := w.leadTimer(); ok {
		t.Fatal("a quiet setup run armed the lead's idle timer")
	}
	if len(w.s.Agents(run)) != 1 || w.status(leadID) != core.AgentIdle && w.status(leadID) != core.AgentWaiting {
		t.Fatalf("lead is %s", w.status(leadID))
	}
}

func TestASetupRunAsksForNoReportAndHasNoCatchUp(t *testing.T) {
	w := newScript(t)
	run, leadID := w.beginSetup("repo", steers)
	w.send(leadID, "go")
	w.endTurn(leadID, core.TurnCompleted)
	for _, e := range w.effects {
		if a, ok := e.(core.ArmTimer); ok {
			t.Fatalf("a setup run armed %+v", a)
		}
	}
	if got := w.runOf(run); !got.NextSummaryAt.IsZero() {
		t.Fatalf("a report is due at %v", got.NextSummaryAt)
	}
	viewer := core.Operator{Viewer: core.Viewer{ID: "v", Label: "laptop"}}
	if r := w.command(viewer, core.RequestCatchUp{Run: run}); r.Refusal != core.RefuseNotPermitted {
		t.Fatalf("Catch me up in setup: %+v", r)
	}
	if got := w.runOf(run); got.Status != core.RunRunning {
		t.Fatalf("run is %s", got.Status)
	}
}

// A run recorded before phases has no phase and is an optimize run: it stalls, ends on its final
// report, reaches its cost limit and spawns.
func TestARunWithNoPhaseBehavesAsAnOptimizeRun(t *testing.T) {
	w, run, leadID, _ := limitedRun(t, 0, 2)
	w.forgetPhases()
	if got := w.runOf(run); got.Phase != "" || !got.Phase.Optimizing() {
		t.Fatalf("the old run's phase is %q", got.Phase)
	}
	receipt := w.spawnWorker(run, leadID)
	if receipt.Refusal != "" {
		t.Fatalf("a spawn in an old run: %+v", receipt)
	}
	w.send(leadID, "go on")
	w.endTurnCosting(leadID, 3)
	if got := w.runOf(run); got.LimitReached != core.LimitCost {
		t.Fatalf("an old run over its cost limit: %+v", got)
	}
}

// Every input of a run with no phase gives what the same input gives an optimize run, but for the
// phase it writes: the empty phase is the optimize phase everywhere a reader asks.
func TestARunWithNoPhaseDecidesAsAnOptimizeRunDoes(t *testing.T) {
	for seed := range uint64(60) {
		forked := func(strip bool) []core.Event {
			r := rand.New(rand.NewPCG(seed, seed+11))
			w, run, leadID, timer := limitedRun(t, time.Duration(10+r.IntN(30))*time.Minute, float64(1+r.IntN(3)))
			if strip {
				w.forgetPhases()
			}
			w.log = nil
			timers := []core.ArmTimer{timer}
			for range 60 {
				w.now = w.now.Add(time.Duration(r.IntN(200)) * time.Second)
				if i := slices.IndexFunc(timers, func(a core.ArmTimer) bool { return !a.FireAt.After(w.now) }); i >= 0 {
					timer := timers[i]
					timers = slices.Delete(timers, i, i+1)
					w.fire(timer)
					timers = append(timers, armedAll(w)...)
					continue
				}
				live := w.runOf(run).Status.Live()
				switch r.IntN(5) {
				case 0:
					if _, running := w.s.ActiveTurn(leadID); running && live {
						w.endTurnCosting(leadID, float64(r.IntN(4))/2)
					}
				case 1:
					if a, ok := w.s.Agent(leadID); ok && a.Status.Resting() && live {
						w.send(leadID, "go")
					}
				case 2:
					if live {
						if receipt := w.spawnWorker(run, leadID); receipt.Refusal == "" {
							w.open(receipt.Agent, steers)
						}
					}
				case 3:
					if live {
						kind := []core.ReportKind{core.ReportProgress, core.ReportFinal}[r.IntN(2)]
						w.command(core.AgentCaller{Agent: leadID}, core.PostReport{Report: core.Report{Kind: kind, Headline: "h"}})
					}
				default:
					if _, running := w.s.ActiveTurn(leadID); running && live {
						w.endTurnCosting(leadID, 0)
					}
				}
				timers = append(timers, armedAll(w)...)
			}
			var out []core.Event
			for _, e := range w.log {
				if run, ok := e.Body.(core.Run); ok {
					run.Phase = ""
					e.Body = run
				}
				out = append(out, e)
			}
			return out
		}
		got, want := forked(true), forked(false)
		for i := range min(len(got), len(want)) {
			if !reflect.DeepEqual(got[i], want[i]) {
				t.Fatalf("seed %d: event %d of a run with no phase is %+v, of an optimize run %+v", seed, i, got[i], want[i])
			}
		}
		if len(got) != len(want) {
			t.Fatalf("seed %d: a run with no phase wrote %d events, an optimize run %d", seed, len(got), len(want))
		}
	}
}

// armedAll are the timers the last step asked for, whatever run they belong to.
func armedAll(w *script) []core.ArmTimer {
	var out []core.ArmTimer
	for _, e := range w.effects {
		if a, ok := e.(core.ArmTimer); ok {
			out = append(out, a)
		}
	}
	return out
}

// Over any order of messages, turns, reports, spawn attempts, operator updates, job counts, catch-up
// presses and clock, a run in setup spawns nothing, never stalls, is asked for no report and told of
// no limit, arms no timer for any of them, and is not ended by anything but the operator.
func TestASetupRunIsOnlyTalkedToOverAnyOrderOfEvents(t *testing.T) {
	for seed := range uint64(250) {
		r := rand.New(rand.NewPCG(seed, seed+3))
		w := newScript(t)
		run, leadID := w.beginSetup("repo", steers)
		caller := core.AgentCaller{Agent: leadID}
		viewer := core.Operator{Viewer: core.Viewer{ID: "v", Label: "laptop"}}
		var timers []core.ArmTimer
		for range 90 {
			w.now = w.now.Add(time.Duration(r.IntN(3000)) * time.Second)
			if i := slices.IndexFunc(timers, func(a core.ArmTimer) bool { return !a.FireAt.After(w.now) }); i >= 0 && r.IntN(2) == 0 {
				timer := timers[i]
				timers = slices.Delete(timers, i, i+1)
				w.fire(timer)
			} else {
				switch r.IntN(9) {
				case 0:
					if _, running := w.s.ActiveTurn(leadID); running {
						w.endTurnCosting(leadID, float64(r.IntN(4)))
					}
				case 1:
					if a, ok := w.s.Agent(leadID); ok && a.Status.Resting() {
						w.send(leadID, "next")
					}
				case 2:
					by := []core.Caller{caller, operator}[r.IntN(2)]
					if rc := w.command(by, core.SpawnAgent{Run: run, Role: core.RoleWorker, Brief: "work"}); rc.Refusal != core.RefuseNotPermitted {
						t.Fatalf("seed %d: a spawn in setup: %+v", seed, rc)
					}
				case 3:
					kind := []core.ReportKind{core.ReportProgress, core.ReportFinal}[r.IntN(2)]
					w.command(caller, core.PostReport{Report: core.Report{Kind: kind, Headline: "h"}})
				case 4:
					w.step(core.JobsRunning{Run: run, Count: uint32(r.IntN(3))})
				case 5:
					w.declare(caller, run, []string{"a", "b", "a.b-c"}[r.IntN(3)])
				case 6:
					if rc := w.update(run, core.UpdateRun{Fanout: fanout(uint32(1 + r.IntN(4)))}); rc.Refusal != core.RefuseNotPermitted {
						t.Fatalf("seed %d: a fan-out in setup: %+v", seed, rc)
					}
				case 7:
					if rc := w.command(viewer, core.RequestCatchUp{Run: run}); rc.Refusal != core.RefuseNotPermitted {
						t.Fatalf("seed %d: a catch-up in setup: %+v", seed, rc)
					}
				default:
					if _, running := w.s.ActiveTurn(leadID); running {
						w.endTurnCosting(leadID, 0)
					}
				}
			}
			for _, a := range armedAll(w) {
				switch a.Kind {
				case core.TimerRunDeadline, core.TimerLeadIdle, core.TimerSummaryDue, core.TimerLimitGrace:
					t.Fatalf("seed %d: a setup run armed %+v", seed, a)
				}
				timers = append(timers, a)
			}
			got := w.runOf(run)
			if got.Status != core.RunRunning || got.Phase != core.PhaseSetup || got.Task != "" || got.LimitReached != "" ||
				got.FinalReported || !got.StalledSince.IsZero() || len(w.s.Agents(run)) != 1 {
				t.Fatalf("seed %d: the setup run is %+v with %d agents", seed, got, len(w.s.Agents(run)))
			}
		}
		for _, e := range w.log {
			if m, ok := e.Body.(core.Message); ok {
				switch m.Body.(type) {
				case core.ReportDue, core.LeadIdleReminder, core.RunLimitReached, core.HandoffReminder:
					t.Fatalf("seed %d: a setup run sent %T", seed, m.Body)
				}
			}
		}
		if rc := w.command(operator, core.StopRun{Run: run}); rc.Refusal != "" {
			t.Fatalf("seed %d: stop refused: %+v", seed, rc)
		}
	}
}

// The spend of the setup phase is the part of the run's usage that belongs to turns that started in
// setup, a turn that started there being setup spend however late it ends, and the cost limit counts the
// rest. What no turn reports stays unreported.
func TestTheCostLimitIgnoresSetupSpendOverAnyOrderOfTurns(t *testing.T) {
	for seed := range uint64(250) {
		r := rand.New(rand.NewPCG(seed, seed+5))
		w := newScript(t)
		run, leadID := w.beginSetup("repo", steers)

		var setupCost, optCost float64
		var setupCostKnown, optCostKnown bool
		var setupTokens, optTokens uint64
		turn := func() (core.Usage, bool) {
			usage := core.Usage{}
			tokens := uint64(1 + r.IntN(50))
			usage.InputTokens = &tokens
			known := r.IntN(3) != 0
			if known {
				usd := float64(r.IntN(8)) / 2
				usage.CostUSD = &usd
			}
			return usage, known
		}
		// Turns that start and end in setup.
		for range r.IntN(5) {
			w.send(leadID, "go")
			usage, known := turn()
			w.fact(leadID, core.TurnEnded{Turn: w.turn(leadID).ID, Status: core.TurnCompleted, Usage: usage})
			setupTokens += *usage.InputTokens
			if known {
				setupCost, setupCostKnown = setupCost+*usage.CostUSD, true
			}
		}
		// One that is still going when setup is left.
		inFlight := r.IntN(2) == 0
		if inFlight {
			w.send(leadID, "go")
		}
		maxCost := float64(1 + r.IntN(6))
		w.now = w.now.Add(time.Second)
		w.leaveSetup(run, "task", maxCost)
		if inFlight {
			usage, known := turn()
			w.fact(leadID, core.TurnEnded{Turn: w.turn(leadID).ID, Status: core.TurnCompleted, Usage: usage})
			setupTokens += *usage.InputTokens
			if known {
				setupCost, setupCostKnown = setupCost+*usage.CostUSD, true
			}
		}
		check := func(step string) {
			t.Helper()
			got := w.runOf(run)
			if got.SetupUsage == nil {
				t.Fatalf("seed %d, %s: no setup usage", seed, step)
			}
			if sum := tokens(got.SetupUsage.InputTokens); sum != setupTokens {
				t.Fatalf("seed %d, %s: setup tokens %d, want %d", seed, step, sum, setupTokens)
			}
			if total := tokens(got.Usage.InputTokens); total != setupTokens+optTokens {
				t.Fatalf("seed %d, %s: total tokens %d, want %d", seed, step, total, setupTokens+optTokens)
			}
			if (got.SetupUsage.CostUSD != nil) != setupCostKnown {
				t.Fatalf("seed %d, %s: setup cost known = %v, want %v", seed, step, got.SetupUsage.CostUSD != nil, setupCostKnown)
			}
			if setupCostKnown && *got.SetupUsage.CostUSD != setupCost {
				t.Fatalf("seed %d, %s: setup cost %v, want %v", seed, step, *got.SetupUsage.CostUSD, setupCost)
			}
			if (got.Usage.CostUSD != nil) != (setupCostKnown || optCostKnown) {
				t.Fatalf("seed %d, %s: total cost known = %v", seed, step, got.Usage.CostUSD != nil)
			}
			if got.Usage.CostUSD != nil && *got.Usage.CostUSD != setupCost+optCost {
				t.Fatalf("seed %d, %s: total cost %v, want %v", seed, step, *got.Usage.CostUSD, setupCost+optCost)
			}
			if reached := got.LimitReached == core.LimitCost; reached != (optCostKnown && optCost >= maxCost) {
				t.Fatalf("seed %d, %s: setup $%v, optimize $%v of $%v, limit reached = %v", seed, step, setupCost, optCost, maxCost, reached)
			}
		}
		check("left setup")
		// Turns that start after it.
		for range 1 + r.IntN(6) {
			if a, ok := w.s.Agent(leadID); !ok || !a.Status.Resting() {
				break
			}
			w.now = w.now.Add(time.Second)
			w.send(leadID, "go")
			usage, known := turn()
			w.fact(leadID, core.TurnEnded{Turn: w.turn(leadID).ID, Status: core.TurnCompleted, Usage: usage})
			optTokens += *usage.InputTokens
			if known {
				optCost, optCostKnown = optCost+*usage.CostUSD, true
			}
			check("optimize turn")
		}
	}
}

// What a limit message and a lead's handover say of the time and the spend is counted from the optimize
// start of a run that had a setup: the lead is told what the optimize phase spent and how long it has run.
func TestALimitMessageAndAHandoverCountFromTheOptimizeStart(t *testing.T) {
	w := newScript(t)
	run, leadID := w.beginSetup("repo", steers)
	w.now = w.now.Add(2 * time.Hour)
	w.send(leadID, "go")
	spent := 3.0
	w.fact(leadID, core.TurnEnded{Turn: w.turn(leadID).ID, Status: core.TurnCompleted, Usage: core.Usage{CostUSD: &spent}})
	w.leaveSetup(run, "task", 1)
	started := w.now

	// A handover now: the lead is replaced, and the new one hears what optimizing has taken.
	w.now = w.now.Add(5 * time.Minute)
	w.update(run, core.UpdateRun{Lead: &leadOn})
	w.fact(leadID, core.SessionClosed{})
	var handover []core.LeadHandover
	for _, e := range w.log {
		if m, ok := e.Body.(core.Message); ok && m.Run == run {
			if h, ok := m.Body.(core.LeadHandover); ok {
				handover = append(handover, h)
			}
		}
	}
	if len(handover) == 0 {
		t.Fatal("no handover was written")
	}
	if h := handover[len(handover)-1]; h.Elapsed > 5*time.Minute+2*time.Second || h.Spent == nil || *h.Spent != 0 {
		t.Fatalf("handover %+v: want about five minutes and nothing spent since setup", h)
	}

	w.open(leadID, steers)
	w.endTurn(leadID, core.TurnCompleted)
	w.now = w.now.Add(10 * time.Minute)
	w.send(leadID, "go")
	w.endTurnCosting(leadID, 1.5)
	told := w.limitMessages()
	if len(told) != 1 {
		t.Fatalf("limit messages %+v", told)
	}
	if want := w.runOf(run).LimitReachedAt.Sub(started).Truncate(time.Second); told[0].Elapsed != want {
		t.Fatalf("elapsed %v, want %v from the optimize start", told[0].Elapsed, want)
	}
	if told[0].Spent == nil || *told[0].Spent != 1.5 {
		t.Fatalf("spent %v, want the optimize phase's 1.5", told[0].Spent)
	}
}

func TestUsageMinusIsThePlusInverseAndKeepsWhatIsUnknown(t *testing.T) {
	for seed := range uint64(300) {
		r := rand.New(rand.NewPCG(seed, seed+9))
		draw := func() core.Usage {
			var u core.Usage
			for _, p := range []**uint64{&u.InputTokens, &u.CacheReadTokens, &u.CacheCreationTokens, &u.OutputTokens, &u.ReasoningTokens} {
				if r.IntN(2) == 0 {
					n := uint64(r.IntN(1000))
					*p = &n
				}
			}
			if r.IntN(2) == 0 {
				c := float64(r.IntN(400)) / 4
				u.CostUSD = &c
			}
			return u
		}
		a, b := draw(), draw()
		sum := a.Plus(b)
		if back := sum.Minus(b); !reflect.DeepEqual(back, orUnknown(a, sum)) {
			t.Fatalf("seed %d: (%+v + %+v) - %+v = %+v", seed, a, b, b, back)
		}
		if got := a.Minus(core.Usage{}); !reflect.DeepEqual(got, a) {
			t.Fatalf("seed %d: %+v less nothing is %+v", seed, a, got)
		}
	}
}

// orUnknown is what the sum less b should be: a, except that a field a does not report but b does comes
// back as the field's zero, since the sum reports it.
func orUnknown(a, sum core.Usage) core.Usage {
	zero := func(have, in *uint64) *uint64 {
		if have == nil && in != nil {
			z := uint64(0)
			return &z
		}
		return have
	}
	out := a
	out.InputTokens = zero(a.InputTokens, sum.InputTokens)
	out.CacheReadTokens = zero(a.CacheReadTokens, sum.CacheReadTokens)
	out.CacheCreationTokens = zero(a.CacheCreationTokens, sum.CacheCreationTokens)
	out.OutputTokens = zero(a.OutputTokens, sum.OutputTokens)
	out.ReasoningTokens = zero(a.ReasoningTokens, sum.ReasoningTokens)
	if a.CostUSD == nil && sum.CostUSD != nil {
		z := 0.0
		out.CostUSD = &z
	}
	return out
}

// Every new run, whichever way it starts, has a phase.
func TestNoNewRunHasAnEmptyPhase(t *testing.T) {
	for seed := range uint64(100) {
		r := rand.New(rand.NewPCG(seed, seed+13))
		w := newScript(t)
		for i := range 1 + r.IntN(4) {
			repo := []string{"a", "b", "c"}[r.IntN(3)]
			if r.IntN(2) == 0 {
				w.command(operator, core.StartRun{Repository: repo, Task: "t", Lead: lead})
			} else {
				w.command(operator, core.BeginSetup{Repository: repo, Lead: lead})
			}
			_ = i
		}
		for _, run := range w.s.Runs() {
			if run.Phase == "" {
				t.Fatalf("seed %d: run %s has no phase", seed, run.ID)
			}
		}
		for _, e := range w.log {
			if run, ok := e.Body.(core.Run); ok && run.Phase == "" {
				t.Fatalf("seed %d: an event wrote run %s with no phase", seed, run.ID)
			}
		}
	}
}

// tokens is a count that a usage may not report, which reads as none.
func tokens(n *uint64) uint64 {
	if n == nil {
		return 0
	}
	return *n
}
