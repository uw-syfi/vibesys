package core_test

import (
	"reflect"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// launchTimer is the timer a launch armed, from the effects of the step that launched.
func (w *script) launchTimer() core.ArmTimer {
	w.t.Helper()
	for _, e := range w.effects {
		if timer, ok := e.(core.ArmTimer); ok && timer.Kind == core.TimerLaunchDeadline {
			return timer
		}
	}
	w.t.Fatalf("no launch timer among %v", w.kinds())
	return core.ArmTimer{}
}

func (w *script) startOnly() core.Receipt {
	w.t.Helper()
	return w.command(operator, core.StartRun{Repository: "repo", Task: "task", FanoutLimit: 1, Lead: lead, Brief: "go"})
}

func TestASessionThatNeverOpensFailsWhenItsLaunchTimerFires(t *testing.T) {
	w := newScript(t)
	r := w.startOnly()
	timer := w.launchTimer()
	if want := w.now.Add(core.LaunchTimeout); !timer.FireAt.Equal(want) {
		t.Fatalf("launch timer at %v, want %v", timer.FireAt, want)
	}
	w.step(core.TimerFired{Timer: timer})
	agent, _ := w.s.Agent(r.Agent)
	if agent.Status != core.AgentFailed || agent.StartFailure != core.StartTimedOut || agent.EndDetail == "" {
		t.Fatalf("lead %+v, want failed as timed out with a detail", agent)
	}
	w.wantEffects("CloseSession")
	if got, _ := w.s.Run(r.Run); got.Status != core.RunFailed || got.StopReason != core.StopLeadGone {
		t.Fatalf("run %+v, want failed because its lead is gone", got)
	}
}

func TestALaunchTimerChangesNothingOnceTheSessionOpened(t *testing.T) {
	w := newScript(t)
	r := w.startOnly()
	timer := w.launchTimer()
	w.open(r.Agent, steers)
	before := w.s
	w.step(core.TimerFired{Timer: timer})
	if len(w.events) != 0 || !reflect.DeepEqual(before, w.s) {
		t.Fatalf("a launch timer after the session opened recorded %v", w.events)
	}
}

func TestASessionThatFailsToStartKeepsItsReasonAndAnOpenOneDoesNot(t *testing.T) {
	for _, reason := range []core.StartFailure{
		core.StartAgentNotInstalled, core.StartNotSignedIn, core.StartPluginUnavailable,
		core.StartToolsUnavailable, core.StartWorkingDirUnavailable, core.StartOther,
	} {
		w := newScript(t)
		r := w.startOnly()
		w.fact(r.Agent, core.SessionFailed{Detail: "no tools", StartFailure: reason})
		if agent, _ := w.s.Agent(r.Agent); agent.Status != core.AgentFailed || agent.StartFailure != reason {
			t.Fatalf("lead %+v, want failed with %s", agent, reason)
		}
	}
	w, _, leadID := started(t, 1, noSteer)
	w.fact(leadID, core.SessionFailed{Detail: "crashed", StartFailure: core.StartToolsUnavailable})
	if agent, _ := w.s.Agent(leadID); agent.StartFailure != "" {
		t.Fatalf("a session that had opened recorded start failure %q", agent.StartFailure)
	}
}

func TestALostSessionThatNeverOpenedGetsTheOtherReasonUnlessTold(t *testing.T) {
	w := newScript(t)
	r := w.startOnly()
	w.step(core.SessionLost{Session: core.Session{Agent: r.Agent, N: 1}, Detail: "gone"})
	if agent, _ := w.s.Agent(r.Agent); agent.Status != core.AgentLost || agent.StartFailure != core.StartOther {
		t.Fatalf("lead %+v, want lost with the other reason", agent)
	}
}
