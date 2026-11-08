package core

// This file holds what a run's phase means to the core. A run starts in one of two phases: setup, in
// which its lead makes the task with the operator (BeginSetup), and optimize, in which it improves the
// task (StartRun, and the run that setup completes into). A run recorded before phases has none, and
// reads as optimizing: every reader asks Run.Phase.Optimizing and never compares with PhaseOptimize.
//
// A run in setup is a run that is only talked to:
//
//   - It does not stall, end on a final report, hit a deadline or a cost limit, or ask its lead for
//     progress reports, because none of them is a question about a run that has no task to optimize
//     (see quiet, asksForReports, costLimit).
//   - It has no subagent: a spawn is refused whatever the fan-out limit says, which the operator may
//     change in setup, so the limit is no guard (spawnAgent, limitRefusal, Violations).
//   - It holds its repository, is listed, and ends as any run does, by StopRun.
//
// Its spend and its time are accounted apart from the optimize phase's: see setup_account.go.

// inSetup says the run is in its setup phase.
func (r Run) inSetup() bool { return !r.Phase.Optimizing() }

// beginSetup starts a run in its setup phase: a lead in the repository with the setup brief, no task,
// no limits and no schedule. The task is named by DeclareTask and fixed by completing setup.
func (t *tx) beginSetup(b BeginSetup, r *Receipt) RunID {
	if busy, ok := t.runInRepository(b.Repository); ok {
		r.Run, r.Refusal = busy, RefuseRepositoryInUse
		return ""
	}
	lead, ok := t.resolveChoice(r, b.Lead.Harness, b.Lead.Model)
	if !ok {
		return ""
	}
	b.Lead.Model = lead
	allow := make([]AgentChoice, len(b.Allow))
	for i, c := range b.Allow {
		if allow[i].Model, ok = t.resolveChoice(r, c.Harness, c.Model); !ok {
			return ""
		}
		allow[i].Harness = c.Harness
	}
	run := Run{
		ID: RunID(t.mint("run")), Repository: b.Repository, Status: RunStarting, Phase: PhaseSetup,
		Lead: AgentID(t.mint("agent")), CreatedAt: t.in.At, LastSummaryAt: t.in.At, Allowed: allowance(b.Lead, allow),
	}
	r.Run, r.Agent = run.ID, run.Lead
	t.emit(run.ID, run)
	t.launch(run, Agent{ID: run.Lead, Run: run.ID, Role: RoleLead, Spec: b.Lead})
	r.Message = t.post(Message{Run: run.ID, To: run.Lead, Body: SetupBrief{Repository: b.Repository, Steering: b.Brief}}).ID
	return run.ID
}

// declareTask records the name the setup lead is making its task under. The latest name wins, and
// the name it already has is no change.
func (t *tx) declareTask(c Command, b DeclareTask, r *Receipt) RunID {
	run, ok := t.s.runs[b.Run]
	switch {
	case !ok || !t.callerIn(c, b.Run):
		r.Refusal = RefuseRunNotFound
		return ""
	case !run.Status.Live():
		r.Run, r.Refusal = run.ID, RefuseRunNotRunning
		return ""
	case !run.inSetup():
		r.Run, r.Refusal = run.ID, RefuseNotInSetup
		return ""
	}
	r.Run = run.ID
	if run.SetupTask != b.Task {
		run.SetupTask = b.Task
		t.emit(run.ID, run)
	}
	return run.ID
}
