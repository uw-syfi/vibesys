package supervisor_test

import (
	"context"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor/suptest"
)

type rigParts struct {
	gates     *fakeenv.MemoryGates
	env       *fakeenv.Environment
	checkouts *fakeenv.Checkouts
	survivors *supervisor.Survivors
}

func newParts(t *testing.T) rigParts {
	t.Helper()
	gates := fakeenv.NewMemoryGates()
	p := rigParts{
		gates:     gates,
		env:       fakeenv.New(core.EnvironmentHost, gates),
		checkouts: &fakeenv.Checkouts{},
		survivors: supervisor.NewSurvivors(),
	}
	t.Cleanup(func() {
		p.survivors.Close()
		if n := p.env.Active(); n != 0 {
			t.Errorf("%d commands still run after the test", n)
		}
		if n := p.checkouts.Open(); n != 0 {
			t.Errorf("%d checkouts still open after the test", n)
		}
	})
	return p
}

func (p rigParts) supervisors() supervisor.Supervisors {
	return supervisor.NewInProcess(fakeenv.Environments{core.EnvironmentHost: p.env}, p.checkouts, p.survivors)
}

func TestInProcessPassesTheContract(t *testing.T) {
	suptest.Run(t, func(t *testing.T) suptest.Rig {
		p := newParts(t)
		return suptest.Rig{
			Supervisors: p.supervisors(),
			Restart:     p.supervisors,
			Open:        p.gates.Release,
			Kill:        func(ref lifecycle.Ref) { p.survivors.Kill(ref) },
		}
	})
}

func launchOf(script ...string) lifecycle.Launch {
	return lifecycle.Launch{
		Repository: "/repo", Task: "task", Job: "j1",
		Spec: core.JobSpec{
			Commit:      core.Commit("0123456789abcdef0123456789abcdef01234567"),
			Environment: core.EnvironmentHost,
			Settings:    core.CommandSettings{Argv: script, Timeout: time.Hour},
		},
	}
}

func finish(t *testing.T, s *supervisor.Supervisor) lifecycle.Outcome {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), suptest.Backstop)
	defer cancel()
	out, err := s.Attach(ctx, 0, func(uint64, []byte) error { return nil })
	if err != nil {
		t.Fatalf("Attach: %v", err)
	}
	return out
}

func TestParkedSupervisorRunsNothing(t *testing.T) {
	p := newParts(t)
	s := supervisor.New(supervisor.Config{
		Launch: launchOf("print", "x"), Environments: fakeenv.Environments{core.EnvironmentHost: p.env},
		Checkouts: p.checkouts, Spool: &supervisor.MemorySpool{},
	})
	if n := len(p.env.Runs()); n != 0 || s.Phase() != supervisor.PhaseParked {
		t.Fatalf("a parked supervisor ran %d commands, phase %s", n, s.Phase())
	}
	if err := s.Release(); err != nil {
		t.Fatal(err)
	}
	if n := len(p.env.Runs()) + p.checkouts.Made(); n != 0 {
		t.Fatalf("a released parked supervisor ran %d things", n)
	}
}

func TestCancelBeforeStartNeverRuns(t *testing.T) {
	p := newParts(t)
	s := supervisor.New(supervisor.Config{
		Launch: launchOf("print", "x"), Environments: fakeenv.Environments{core.EnvironmentHost: p.env},
		Checkouts: p.checkouts, Spool: &supervisor.MemorySpool{},
	})
	s.Cancel("changed my mind")
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	out := finish(t, s)
	if out.Exec.Kind != core.ExecCancelled || !out.CancelAsked || out.CancelReason != "changed my mind" {
		t.Fatalf("outcome = %+v, want cancelled", out)
	}
	if len(p.env.Runs()) != 0 || p.checkouts.Made() != 0 {
		t.Fatalf("a cancelled job ran: %d runs, %d checkouts", len(p.env.Runs()), p.checkouts.Made())
	}
}

func TestCheckoutFailureIsNotAttempted(t *testing.T) {
	p := newParts(t)
	p.checkouts.Fail = &core.Failure{Kind: core.FailureNotFound, Message: "no such commit"}
	s := supervisor.New(supervisor.Config{
		Launch: launchOf("print", "x"), Environments: fakeenv.Environments{core.EnvironmentHost: p.env},
		Checkouts: p.checkouts, Spool: &supervisor.MemorySpool{},
	})
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	out := finish(t, s)
	if out.NotAttempted == nil || out.NotAttempted.Kind != core.FailureNotFound {
		t.Fatalf("outcome = %+v, want not attempted: not found", out)
	}
}

func TestUnknownEnvironmentIsNotAttempted(t *testing.T) {
	p := newParts(t)
	l := launchOf("print", "x")
	l.Spec.Environment = core.EnvironmentDocker
	s := supervisor.New(supervisor.Config{
		Launch: l, Environments: fakeenv.Environments{core.EnvironmentHost: p.env},
		Checkouts: p.checkouts, Spool: &supervisor.MemorySpool{},
	})
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	out := finish(t, s)
	if out.NotAttempted == nil || out.NotAttempted.Kind != core.FailureUnavailable {
		t.Fatalf("outcome = %+v, want unavailable, never another environment", out)
	}
	if len(p.env.Runs()) != 0 {
		t.Fatal("the job ran in another environment")
	}
}

func TestOnlyCommandJobsRunInThisBuild(t *testing.T) {
	p := newParts(t)
	l := launchOf()
	l.Spec.Settings = core.CheckSettings{}
	s := supervisor.New(supervisor.Config{
		Launch: l, Environments: fakeenv.Environments{core.EnvironmentHost: p.env},
		Checkouts: p.checkouts, Spool: &supervisor.MemorySpool{},
	})
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	if out := finish(t, s); out.NotAttempted == nil || out.NotAttempted.Kind != core.FailureUnavailable {
		t.Fatalf("outcome = %+v, want unavailable", out)
	}
}

func TestCheckoutIsRemovedOnEveryEnd(t *testing.T) {
	for _, script := range [][]string{{"exit", "0"}, {"exit", "3"}, {"timeout"}, {"fail", "x"}} {
		p := newParts(t)
		s := supervisor.New(supervisor.Config{
			Launch: launchOf(script...), Environments: fakeenv.Environments{core.EnvironmentHost: p.env},
			Checkouts: p.checkouts, Spool: &supervisor.MemorySpool{},
		})
		if err := s.Start(); err != nil {
			t.Fatal(err)
		}
		finish(t, s)
		if p.checkouts.Open() != 0 || p.env.Active() != 0 {
			t.Errorf("script %v left %d checkouts, %d commands", script, p.checkouts.Open(), p.env.Active())
		}
	}
}

func TestCloseEndsARunningCommand(t *testing.T) {
	p := newParts(t)
	s := supervisor.New(supervisor.Config{
		Launch: launchOf("print", "up", ";", "hold", "never"), Environments: fakeenv.Environments{core.EnvironmentHost: p.env},
		Checkouts: p.checkouts, Spool: &supervisor.MemorySpool{},
	})
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	s.Close()
	if p.env.Active() != 0 || p.checkouts.Open() != 0 {
		t.Fatalf("Close left %d commands, %d checkouts", p.env.Active(), p.checkouts.Open())
	}
	// A command ended by Close is not a cancel the user asked for.
	if out := finish(t, s); out.CancelAsked {
		t.Fatalf("outcome = %+v, a close is not a cancel", out)
	}
}
