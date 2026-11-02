package wire_test

import (
	"fmt"
	"math/rand/v2"
	"reflect"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

func anyCommand(r *rand.Rand) task.Command {
	return task.Command{Argv: append([]string{"run"}, words(r, r.IntN(3))...), Timeout: time.Duration(1+r.IntN(1000)) * time.Second}
}

func anyFingerprint(r *rand.Rand) core.Fingerprint {
	f := core.Fingerprint{Kind: core.EnvironmentHost, Backend: pick(r, core.BackendCPU, core.BackendCUDA)}
	if r.IntN(2) == 0 {
		f.Facts = map[string]string{"os": "linux", "arch": pick(r, "amd64", "arm64")}
	}
	return f
}

// anyPlan is a valid plan of either kind.
func anyPlan(r *rand.Rand) flow.Plan {
	p := flow.Plan{
		Kind: core.JobCheck, Task: "kv", TaskVersion: fmt.Sprintf("v%d", r.IntN(9)), Key: core.JobKey(fmt.Sprintf("k%d", r.IntN(9))),
		Environment: anyFingerprint(r), Check: anyCommand(r),
	}
	if r.IntN(2) == 0 {
		b := anyCommand(r)
		p.Build = &b
	}
	if r.IntN(2) == 0 {
		return p
	}
	p.Kind = core.JobMeasure
	p.Samples = uint32(1 + r.IntN(9))
	p.Benchmark = anyCommand(r)
	p.Metric = core.Metric{Name: pick(r, "ops", "latency"), Direction: pick(r, core.Maximize, core.Minimize)}
	if r.IntN(2) == 0 {
		p.Result = task.Result{Protocol2: true}
	} else {
		p.Result = task.Result{JSONArgument: "--out", Field: pick(r, "ops", "ü")}
	}
	if r.IntN(2) == 0 {
		p.CheckPass = core.JobID(fmt.Sprintf("j%d", 1+r.IntN(9)))
	}
	return p
}

func launchWith(r *rand.Rand, p *flow.Plan) lifecycle.Launch {
	commit := core.Commit(fmt.Sprintf("%040x", r.Uint64()))
	l := lifecycle.Launch{Repository: "/repo", Task: "kv", Job: "j1", Plan: p}
	switch {
	case p == nil:
		l.Spec = core.JobSpec{Commit: commit, Environment: core.EnvironmentHost, Settings: core.CommandSettings{Argv: []string{"true"}, Timeout: time.Minute}}
	case p.Kind == core.JobCheck:
		l.Spec = core.JobSpec{Commit: commit, Environment: p.Environment.Kind, Settings: core.CheckSettings{}}
	default:
		l.Spec = core.JobSpec{Commit: commit, Environment: p.Environment.Kind, Settings: core.MeasureSettings{Samples: p.Samples}}
	}
	return l
}

func TestAPlanSurvivesTheLaunchAndBack(t *testing.T) {
	r := newRand(21)
	for range 1000 {
		p := anyPlan(r)
		l := launchWith(r, &p)
		// Through bytes, as the file in the run directory does.
		data, err := proto.Marshal(wire.Launch(l))
		if err != nil {
			t.Fatal(err)
		}
		var msg cliv1.Launch
		if err := proto.Unmarshal(data, &msg); err != nil {
			t.Fatal(err)
		}
		got, err := wire.ToLaunch(&msg)
		if err != nil {
			t.Fatalf("plan %+v: %v", p, err)
		}
		if !reflect.DeepEqual(got, l) {
			t.Fatalf("launch came back changed:\n got %+v\nwant %+v", got.Plan, l.Plan)
		}
	}
}

func TestALaunchWithoutAPlanStaysAsItWas(t *testing.T) {
	r := newRand(22)
	l := launchWith(r, nil)
	got, err := wire.ToLaunch(wire.Launch(l))
	if err != nil || got.Plan != nil || !reflect.DeepEqual(got, l) {
		t.Fatalf("got %+v, %v", got, err)
	}
}

// What a supervisor is promised about its plan holds for every plan the daemon could write: the
// plan fits the job it is for, and it is one the flow can follow.
func TestALaunchPlanIsHeldToTheJobAndToTheFlow(t *testing.T) {
	r := newRand(23)
	check := anyPlan(r)
	for check.Kind != core.JobCheck {
		check = anyPlan(r)
	}
	measure := anyPlan(r)
	for measure.Kind != core.JobMeasure {
		measure = anyPlan(r)
	}
	for name, c := range map[string]struct {
		launch lifecycle.Launch
		want   string
	}{
		"a command job with a plan": {func() lifecycle.Launch { l := launchWith(r, nil); l.Plan = &check; return l }(), "launch.plan"},
		"a check job with no plan":  {func() lifecycle.Launch { l := launchWith(r, &check); l.Plan = nil; return l }(), "launch.plan"},
		"a measure job with a check plan": {func() lifecycle.Launch {
			l := launchWith(r, &measure)
			l.Plan = &check
			return l
		}(), "launch.plan"},
		"a plan for another environment": {func() lifecycle.Launch {
			p := check
			p.Environment.Kind = core.EnvironmentDocker
			p.Environment.ImageDigest = "sha256:" + strings.Repeat("0", 64)
			l := launchWith(r, &check)
			l.Plan = &p
			return l
		}(), "launch.plan"},
		"a measure plan with no samples": {func() lifecycle.Launch {
			p := measure
			p.Samples = 0
			return launchWith(r, &p)
		}(), "sample"},
		"a plan whose command has no program": {func() lifecycle.Launch {
			p := check
			p.Check.Argv = nil
			return launchWith(r, &p)
		}(), "check"},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := wire.ToLaunch(wire.Launch(c.launch)); err == nil || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("ToLaunch: %v, want it to name %q", err, c.want)
			}
		})
	}
}

func anyResult(r *rand.Rand, measure bool) *core.Result {
	res := &core.Result{
		Job: "j3", Key: "k1", Task: "kv", TaskVersion: "v2", Commit: core.Commit(fmt.Sprintf("%040x", r.Uint64())), SchemaVersion: core.SchemaVersion,
		Environment: anyFingerprint(r),
		Machine:     core.MachineFacts{HostID: "host-a", OS: "linux", Arch: "amd64", CPUModel: "cpu", LogicalCPUs: uint32(1 + r.IntN(64)), MemoryBytes: r.Uint64()},
		Load: core.MachineLoad{
			SampledAt: epoch.Add(time.Duration(r.IntN(1e6)) * time.Millisecond), LoadAverage1m: r.Float64() * 8,
			CPUBusyFraction: r.Float64(), MemoryAvailableBytes: r.Uint64(),
		},
		RecordedAt: epoch.Add(time.Duration(r.IntN(1e6)) * time.Second),
	}
	if !measure {
		res.Outcome = core.CheckOutcome{Duration: time.Duration(r.IntN(1e9))}
		return res
	}
	m := core.MeasureOutcome{
		Metric: core.Metric{Name: "ops", Direction: pick(r, core.Maximize, core.Minimize)}, CheckJob: pick[core.JobID](r, "j1", "j3"),
	}
	for range 1 + r.IntN(6) {
		m.Samples = append(m.Samples, r.NormFloat64()*1e6)
	}
	for range r.IntN(3) {
		o := core.Observation{Name: fmt.Sprintf("o%d", len(m.Observations))}
		for range 1 + r.IntN(4) {
			o.Values = append(o.Values, r.Float64())
		}
		m.Observations = append(m.Observations, o)
	}
	res.Outcome = m
	return res
}

func TestAFoundResultSurvivesTheOutcomeAndBack(t *testing.T) {
	r := newRand(24)
	for i := range 1000 {
		o := lifecycle.Outcome{Exec: core.ExecOutcome{Kind: core.ExecExited}, Result: anyResult(r, i%2 == 0)}
		data, err := proto.Marshal(wire.Outcome(o))
		if err != nil {
			t.Fatal(err)
		}
		var msg cliv1.SupervisorOutcome
		if err := proto.Unmarshal(data, &msg); err != nil {
			t.Fatal(err)
		}
		if got := wire.ToOutcome(&msg); !reflect.DeepEqual(got, o) {
			t.Fatalf("outcome came back changed:\n got %+v\nwant %+v", got.Result, o.Result)
		}
	}
}

func TestARejectionAndItsReasonSurviveTheOutcomeAndBack(t *testing.T) {
	for _, o := range []lifecycle.Outcome{
		{Exec: core.ExecOutcome{Kind: core.ExecExited}, Rejected: true, Reason: "sample 1: no result"},
		{Exec: core.ExecOutcome{Kind: core.ExecExited, ExitCode: 3}, Reason: "the benchmark exited with code 3"},
	} {
		if got := wire.ToOutcome(wire.Outcome(o)); !reflect.DeepEqual(got, o) {
			t.Errorf("got %+v, want %+v", got, o)
		}
	}
}

// A result that cannot be read is never a success: the job it came with is lost, not saved.
func TestAnOutcomeWhoseResultCannotBeReadIsNotASuccess(t *testing.T) {
	r := newRand(25)
	o := lifecycle.Outcome{Exec: core.ExecOutcome{Kind: core.ExecExited}, Result: anyResult(r, true)}
	msg := wire.Outcome(o)
	msg.Found.Outcome = nil
	got := wire.ToOutcome(msg)
	if got.Result != nil || got.Exec.Kind != core.ExecUnknown {
		t.Fatalf("got %+v", got)
	}
	if st := lifecycle.Settle("j1", core.JobRunning, got, epoch); st.Transition.To != core.JobLost {
		t.Fatalf("settled as %s", st.Transition.To)
	}
}

// FuzzLaunchAndOutcome reads what a file or a connection could hold: it never panics, and a launch
// it accepts is a plan the flow can follow.
func FuzzLaunchAndOutcome(f *testing.F) {
	r := newRand(26)
	for range 8 {
		p := anyPlan(r)
		data, _ := proto.Marshal(wire.Launch(launchWith(r, &p)))
		f.Add(data, true)
		o, _ := proto.Marshal(wire.Outcome(lifecycle.Outcome{Exec: core.ExecOutcome{Kind: core.ExecExited}, Result: anyResult(r, r.IntN(2) == 0)}))
		f.Add(o, false)
	}
	f.Fuzz(func(t *testing.T, data []byte, asLaunch bool) {
		if asLaunch {
			var msg cliv1.Launch
			if proto.Unmarshal(data, &msg) != nil {
				return
			}
			l, err := wire.ToLaunch(&msg)
			if err != nil {
				return
			}
			if l.Plan != nil {
				if err := l.Plan.Validate(); err != nil {
					t.Fatalf("an accepted launch has a plan the flow refuses: %v", err)
				}
			}
			return
		}
		var msg cliv1.SupervisorOutcome
		if proto.Unmarshal(data, &msg) != nil {
			return
		}
		// Whatever the bytes said, an outcome that has a result says what it measured.
		if o := wire.ToOutcome(&msg); o.Result != nil && o.Result.Outcome == nil {
			t.Fatalf("a result with no outcome was accepted: %+v", o.Result)
		}
	})
}
