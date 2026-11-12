package supervisor_test

import (
	"context"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
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
			Settings:    core.CommandSettings{Image: core.ImageHarness, Argv: script, Timeout: time.Hour},
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
	if n := len(p.env.Runs()); n != 0 || s.Phase() != lifecycle.RunParked {
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
	if execOf(out).Kind != core.ExecCancelled || !out.CancelAsked || out.CancelReason != "changed my mind" {
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
	if notAttemptedOf(out) == nil || notAttemptedOf(out).Kind != core.FailureNotFound {
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
	if notAttemptedOf(out) == nil || notAttemptedOf(out).Kind != core.FailureUnavailable {
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
	if out := finish(t, s); notAttemptedOf(out) == nil || notAttemptedOf(out).Kind != core.FailureInternal {
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
		env:       fakeenv.New(core.EnvironmentHost, fakeenv.NewMemoryGates(), fakeenv.WithFiles(files), fakeenv.WithMachine(cpu.New(cpu.NewFakeSystem(facts), clk))),
		checkouts: &fakeenv.Checkouts{},
		host:      supervisor.Host{Files: files, Clock: clk},
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
	res := passedOf(out)
	if res == nil {
		t.Fatalf("outcome = %+v, want a pass", out)
	}
	m, ok := res.Outcome.(core.MeasureOutcome)
	if !ok || len(m.Samples) != 2 || m.Samples[0] != 7.5 || m.Samples[1] != 7.5 || m.CheckJob != "j1" {
		t.Errorf("result = %+v", res)
	}
	if res.Job != "j1" || res.Commit != launchOf().Spec.Commit || res.Key != "k1" {
		t.Errorf("result identity = %+v", res)
	}
}

// A result records the machine of the environment that ran the job, which for docker on another
// machine is that machine and not the daemon's, and its load there.
func TestAResultRecordsTheMachineItsEnvironmentRanOn(t *testing.T) {
	r := newPlanRig(t)
	clk := clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), time.Millisecond)
	there := core.MachineFacts{HostID: "host-b", OS: "linux", Arch: "arm64", CPUModel: "Other CPU", LogicalCPUs: 20, MemoryBytes: 128 << 30}
	docker := fakeenv.New(core.EnvironmentDocker, fakeenv.NewMemoryGates(), fakeenv.WithFiles(r.files),
		fakeenv.WithMachine(cpu.New(cpu.NewFakeSystem(there), clk)))
	p := measurePlan("emit", `{"ops": 7.5}`)
	p.Environment.Kind = core.EnvironmentDocker
	l := planLaunch(p)
	l.Spec.Environment = core.EnvironmentDocker
	s := supervisor.New(supervisor.Config{
		Launch: l, Environments: fakeenv.Environments{core.EnvironmentHost: r.env, core.EnvironmentDocker: docker},
		Checkouts: r.checkouts, Spool: &supervisor.MemorySpool{}, Host: r.host,
	})
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	res := passedOf(finish(t, s))
	if res == nil {
		t.Fatal("the job did not pass")
	}
	if res.Machine != there || res.Load.SampledAt.IsZero() {
		t.Errorf("result machine = %+v, load %+v; want the docker machine's %+v and its load", res.Machine, res.Load, there)
	}
}

func TestAPlanThatDoesNotPassSavesNoResultAndSaysWhy(t *testing.T) {
	for name, c := range map[string]struct {
		bench    []string
		rejected bool
		says     string
	}{
		"a benchmark that fails": {[]string{"exit", "3"}, false, "benchmark step exited with code 3"},
		"no result written":      {[]string{"exit", "0"}, true, "wrote no result"},
		"a result that is junk":  {[]string{"emit", "junk"}, true, "sample 1"},
	} {
		t.Run(name, func(t *testing.T) {
			r := newPlanRig(t)
			spool := &supervisor.MemorySpool{}
			s := r.supervisor(planLaunch(measurePlan(c.bench...)), spool)
			if err := s.Start(); err != nil {
				t.Fatal(err)
			}
			out := finish(t, s)
			if passedOf(out) != nil {
				t.Fatalf("outcome = %+v saves a result", out)
			}
			var rejected lifecycle.Rejected
			isRejected := false
			if end, ok := out.End.(lifecycle.FlowEnded); ok {
				rejected, isRejected = end.Verdict.(lifecycle.Rejected)
			}
			if isRejected != c.rejected {
				t.Fatalf("outcome = %+v, rejected = %v, want %v", out, isRejected, c.rejected)
			}
			if !c.rejected && execOf(out).ExitCode != 3 {
				t.Fatalf("outcome = %+v, want the benchmark's exit code 3", out)
			}
			settled := lifecycle.Settle("j1", core.JobRunning, out, time.Unix(1, 0))
			if settled.Transition.To != core.JobFailed || settled.Transition.Result != nil {
				t.Errorf("settlement = %+v", settled)
			}
			// The job's output says why, whether the supervisor wrote the line or the daemon will.
			said := settled.Log + spoolText(spool) + rejected.Reason
			if !strings.Contains(said, c.says) {
				t.Errorf("neither the output nor the settlement says %q: %q", c.says, said)
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
	if tr.To != core.JobSucceeded || tr.Result == nil || settled.Log != "" {
		t.Fatalf("settlement = %+v", settled)
	}
	// The check pass is in the result and nowhere else: the job takes it from there.
	if m, ok := tr.Result.Outcome.(core.MeasureOutcome); !ok || m.CheckJob != "j1" || tr.CheckJob != "" {
		t.Errorf("the result names check job %+v, and the transition %q", tr.Result.Outcome, tr.CheckJob)
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
	if out := finish(t, s); notAttemptedOf(out) == nil || notAttemptedOf(out).Kind != core.FailureUnavailable {
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
	if execOf(out).Kind != core.ExecCancelled || !out.CancelAsked || out.CancelReason != "stop" {
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

// execOf is how the command of an outcome ended, zero when no ending was seen.
func execOf(o lifecycle.Outcome) core.ExecOutcome {
	switch end := o.End.(type) {
	case lifecycle.CommandEnded:
		if e, ok := end.Attempt.(core.Ended); ok {
			return e.Exec
		}
	case lifecycle.FlowEnded:
		if s, ok := end.Verdict.(lifecycle.Stopped); ok {
			return s.Exec
		}
	}
	return core.ExecOutcome{}
}

// notAttemptedOf is why an outcome's command never started, nil when it did.
func notAttemptedOf(o lifecycle.Outcome) *core.Failure {
	switch end := o.End.(type) {
	case lifecycle.CommandEnded:
		if e, ok := end.Attempt.(core.NotStarted); ok {
			return &e.Failure
		}
	case lifecycle.FlowEnded:
		if n, ok := end.Verdict.(lifecycle.NotAttempted); ok {
			return &n.Failure
		}
	}
	return nil
}

// passedOf is the result an outcome saves, nil when it saves none.
func passedOf(o lifecycle.Outcome) *core.Result {
	if end, ok := o.End.(lifecycle.FlowEnded); ok {
		if p, ok := end.Verdict.(lifecycle.Passed); ok {
			return &p.Result
		}
	}
	return nil
}

// spoolText is everything the spool holds.
func spoolText(sp *supervisor.MemorySpool) string {
	buf := make([]byte, 1<<16)
	n, _ := sp.ReadAt(buf, 0)
	return string(buf[:n])
}

// targetRecorder is an Environments that remembers the target of every request.
type targetRecorder struct {
	environment.Environments
	targets []environment.Target
}

func (r *targetRecorder) Environment(kind core.EnvironmentKind, target environment.Target) (environment.Environment, error) {
	r.targets = append(r.targets, target)
	return r.Environments.Environment(kind, target)
}

// The environment is asked for the repository, task and commit of the job it runs, since a docker
// environment builds its image from a file of that task at that commit.
func TestTheEnvironmentIsAskedForTheJobsTarget(t *testing.T) {
	p := newParts(t)
	envs := &targetRecorder{Environments: fakeenv.Environments{core.EnvironmentHost: p.env}}
	l := launchOf("print", "x")
	s := supervisor.New(supervisor.Config{Launch: l, Environments: envs, Checkouts: p.checkouts, Spool: &supervisor.MemorySpool{}})
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	finish(t, s)
	want := environment.Target{Repository: l.Repository, Commit: l.Spec.Commit, Task: l.Task}
	if len(envs.targets) != 1 || envs.targets[0] != want {
		t.Fatalf("the environment was asked for %+v, want exactly %+v", envs.targets, want)
	}
}

// The standing allocations where the job's environments could not confirm its processes killed
// are taken once the command is done, and the outcome carries them to the daemon, beside how the
// job ended.
func TestTheOutcomeCarriesTheProcessesLeftInStandingAllocations(t *testing.T) {
	p := newParts(t)
	left := []lifecycle.ProcessesLeft{{Allocation: "4242", Detail: "killing the processes: timed out"}}
	taken := 0
	s := supervisor.New(supervisor.Config{
		Launch: launchOf("print", "x"), Environments: fakeenv.Environments{core.EnvironmentHost: p.env},
		Checkouts: p.checkouts, Spool: &supervisor.MemorySpool{},
		ProcessesLeft: func() []lifecycle.ProcessesLeft {
			taken++
			if n := len(p.env.Runs()); n != 1 {
				t.Errorf("the processes left were taken with %d runs done, want after the command", n)
			}
			return left
		},
	})
	if err := s.Start(); err != nil {
		t.Fatal(err)
	}
	out := finish(t, s)
	if execOf(out).Kind != core.ExecExited || !reflect.DeepEqual(out.ProcessesLeft, left) || taken != 1 {
		t.Fatalf("outcome = %+v after %d takes, want the command's exit and %+v", out, taken, left)
	}
}
