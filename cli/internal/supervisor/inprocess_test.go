package supervisor_test

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/hostfs"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor/suptest"
	"github.com/uw-syfi/vibesys/cli/internal/task"
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

func TestALaunchOfACheckJobWithoutAPlanIsRefused(t *testing.T) {
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
	if out := finish(t, s); out.NotAttempted == nil || out.NotAttempted.Kind != core.FailureInternal || out.Result != nil {
		t.Fatalf("outcome = %+v, want an internal failure and no result", out)
	}
	if len(p.env.Runs()) != 0 {
		t.Fatal("a job without a plan ran something")
	}
}

// planRig is a supervisor over a plan, on fakes: the environment writes the benchmark's result
// files into the same memory file system the job reads them from.
type planRig struct {
	files     *hostfs.Memory
	env       *fakeenv.Environment
	checkouts *fakeenv.Checkouts
	host      supervisor.Host
}

func newPlanRig(t *testing.T) planRig {
	t.Helper()
	files := hostfs.NewMemory()
	clk := clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), time.Millisecond)
	facts := core.MachineFacts{HostID: "host-a", OS: "linux", Arch: "amd64", CPUModel: "Test CPU", LogicalCPUs: 8, MemoryBytes: 16 << 30}
	r := planRig{
		files:     files,
		env:       fakeenv.New(core.EnvironmentHost, fakeenv.NewMemoryGates(), fakeenv.WithFiles(files)),
		checkouts: &fakeenv.Checkouts{},
		host:      supervisor.Host{Backend: cpu.New(cpu.NewFakeSystem(facts), clk), Files: files, Clock: clk},
	}
	t.Cleanup(func() {
		if n := r.env.Active(); n != 0 {
			t.Errorf("%d commands still run after the test", n)
		}
		if n := r.checkouts.Open(); n != 0 {
			t.Errorf("%d checkouts still open after the test", n)
		}
		if left := files.Scratch(); len(left) != 0 {
			t.Errorf("scratch directories left: %v", left)
		}
	})
	return r
}

func (r planRig) supervisor(l lifecycle.Launch, spool supervisor.Spool) *supervisor.Supervisor {
	return supervisor.New(supervisor.Config{
		Launch: l, Environments: fakeenv.Environments{core.EnvironmentHost: r.env},
		Checkouts: r.checkouts, Spool: spool, Host: r.host,
	})
}

func measurePlan(benchmark ...string) flow.Plan {
	script := func(words ...string) task.Command { return task.Command{Argv: words, Timeout: time.Minute} }
	return flow.Plan{
		Kind: core.JobMeasure, Task: "task", TaskVersion: "v1", Key: "k1",
		Environment: core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{"os": "fake"}},
		Check:       script("exit", "0"), Benchmark: script(benchmark...),
		Result: task.Result{JSONArgument: "--out", Field: "ops"}, Metric: core.Metric{Name: "ops", Direction: core.Maximize},
		Samples: 2,
	}
}

func planLaunch(p flow.Plan) lifecycle.Launch {
	l := launchOf()
	l.Spec.Settings = core.MeasureSettings{Samples: p.Samples}
	l.Plan = &p
	return l
}

func TestAPlanRunsAndThePassingJobReportsItsResult(t *testing.T) {
	r := newPlanRig(t)
	spool := &supervisor.MemorySpool{}
	s := r.supervisor(planLaunch(measurePlan("emit", `{"ops": 7.5}`)), spool)
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	out := finish(t, s)
	if out.NotAttempted != nil || out.Rejected || out.Exec.Kind != core.ExecExited || out.Exec.ExitCode != 0 || out.Result == nil {
		t.Fatalf("outcome = %+v", out)
	}
	m, ok := out.Result.Outcome.(core.MeasureOutcome)
	if !ok || len(m.Samples) != 2 || m.Samples[0] != 7.5 || m.Samples[1] != 7.5 || m.CheckJob != "j1" {
		t.Errorf("result = %+v", out.Result)
	}
	if out.Result.Job != "j1" || out.Result.Commit != launchOf().Spec.Commit || out.Result.Key != "k1" {
		t.Errorf("result identity = %+v", out.Result)
	}
}

func TestAPlanThatDoesNotPassReportsWhyAndNoResult(t *testing.T) {
	for name, c := range map[string]struct {
		bench    []string
		rejected bool
		exit     int
	}{
		"a benchmark that fails": {[]string{"exit", "3"}, false, 3},
		"no result written":      {[]string{"exit", "0"}, true, 0},
		"a result that is junk":  {[]string{"emit", "junk"}, true, 0},
	} {
		t.Run(name, func(t *testing.T) {
			r := newPlanRig(t)
			s := r.supervisor(planLaunch(measurePlan(c.bench...)), &supervisor.MemorySpool{})
			if err := s.Start(); err != nil {
				t.Fatal(err)
			}
			out := finish(t, s)
			if out.Result != nil || out.Rejected != c.rejected || out.Exec.ExitCode != c.exit || out.Reason == "" || out.NotAttempted != nil {
				t.Fatalf("outcome = %+v", out)
			}
			settled := lifecycle.Settle("j1", core.JobRunning, out, time.Unix(1, 0))
			if settled.Transition.To != core.JobFailed || settled.Transition.Result != nil || !strings.Contains(settled.Log, out.Reason) {
				t.Errorf("settlement = %+v", settled)
			}
		})
	}
}

func TestAPassingPlanSettlesAsASuccessWithItsResult(t *testing.T) {
	r := newPlanRig(t)
	s := r.supervisor(planLaunch(measurePlan("emit", `{"ops": 2}`)), &supervisor.MemorySpool{})
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	out := finish(t, s)
	settled := lifecycle.Settle("j1", core.JobRunning, out, time.Unix(1, 0))
	tr := settled.Transition
	if tr.To != core.JobSucceeded || tr.Result == nil || tr.CheckJob != "j1" || settled.Log != "" {
		t.Fatalf("settlement = %+v", settled)
	}
}

func TestAPlanWithoutAMachineIsUnavailable(t *testing.T) {
	r := newPlanRig(t)
	s := supervisor.New(supervisor.Config{
		Launch: planLaunch(measurePlan("emit", `{"ops": 1}`)), Environments: fakeenv.Environments{core.EnvironmentHost: r.env},
		Checkouts: r.checkouts, Spool: &supervisor.MemorySpool{},
	})
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	if out := finish(t, s); out.NotAttempted == nil || out.NotAttempted.Kind != core.FailureUnavailable || out.Result != nil {
		t.Fatalf("outcome = %+v", out)
	}
	if len(r.env.Runs()) != 0 || r.checkouts.Made() != 0 {
		t.Fatal("a supervisor without a machine ran the job")
	}
}

func TestAPlanThatIsCancelledReportsNoResult(t *testing.T) {
	r := newPlanRig(t)
	gates := fakeenv.NewMemoryGates()
	env := fakeenv.New(core.EnvironmentHost, gates, fakeenv.WithFiles(r.files))
	plan := measurePlan("mark", "running", ";", "hold", "never")
	s := supervisor.New(supervisor.Config{
		Launch: planLaunch(plan), Environments: fakeenv.Environments{core.EnvironmentHost: env},
		Checkouts: r.checkouts, Spool: &supervisor.MemorySpool{}, Host: r.host,
	})
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), suptest.Backstop)
	defer cancel()
	if !gates.Reached(ctx, "running") {
		t.Fatal("the benchmark never started")
	}
	s.Cancel("stop")
	out := finish(t, s)
	if out.Exec.Kind != core.ExecCancelled || !out.CancelAsked || out.CancelReason != "stop" || out.Result != nil {
		t.Fatalf("outcome = %+v", out)
	}
	if st := lifecycle.Settle("j1", core.JobRunning, out, time.Unix(1, 0)); st.Transition.To != core.JobCancelled || st.Transition.Result != nil {
		t.Errorf("settlement = %+v", st)
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
