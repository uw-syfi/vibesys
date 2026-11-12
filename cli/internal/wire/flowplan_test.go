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

// anyFlowPlan is a valid plan of either kind.
func anyFlowPlan(r *rand.Rand) flow.Plan {
	p := flow.Plan{
		Kind: core.JobCheck, Task: "kv", TaskVersion: fmt.Sprintf("v%d", r.IntN(9)), Key: core.JobKey(fmt.Sprintf("k%d", r.IntN(9))),
		Environment: anyFingerprint(r), Check: anyCommand(r),
	}
	if r.IntN(2) == 0 {
		b := anyCommand(r)
		p.Build = &b
	}
	if r.IntN(2) == 0 {
		p.Service = &task.Service{
			Serve: append([]string{"serve", "--port", "${PORT}"}, words(r, r.IntN(3))...), Ready: pick(r, "/health", "/v1/models"),
			StartupTimeout: time.Duration(1+r.IntN(3600)) * time.Second, Reset: anyCommand(r), ShareWithCheck: r.IntN(2) == 0,
		}
	}
	if r.IntN(2) == 0 {
		return p
	}
	p.Kind = core.JobMeasure
	p.Samples = uint32(1 + r.IntN(9))
	p.Benchmark = anyCommand(r)
	p.Metric = core.Metric{Name: pick(r, "ops", "latency"), Direction: pick(r, core.Maximize, core.Minimize), Unit: pick(r, "", "tok/s", "ms", "µs 🧪")}
	if r.IntN(2) == 0 {
		p.Result = task.Result{Protocol2: true}
		if r.IntN(2) == 0 {
			ttft := core.Metric{Name: "ttft", Direction: pick(r, core.Maximize, core.Minimize), Unit: pick(r, "", "ms", "s")}
			p.Others = []core.Metric{ttft}
			p.Objectives = core.Objectives{
				Metrics:       []core.Metric{p.Metric, ttft},
				RelativeNoise: pick(r, 0, 0.02),
			}
			if r.IntN(2) == 0 {
				p.Objectives.Noise = []core.ObjectiveNoise{{Name: "ttft", RelativeNoise: pick(r, 0, 0.15)}}
			}
		}
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
		l.Spec = core.JobSpec{Commit: commit, Environment: core.EnvironmentHost, Settings: core.CommandSettings{Image: core.ImageHarness, Argv: []string{"true"}, Timeout: time.Minute}}
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
		p := anyFlowPlan(r)
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
	check := anyFlowPlan(r)
	for check.Kind != core.JobCheck {
		check = anyFlowPlan(r)
	}
	measure := anyFlowPlan(r)
	for measure.Kind != core.JobMeasure {
		measure = anyFlowPlan(r)
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

// FuzzDecodeLaunchPlan reads what a file in a run directory or a connection could hold: it never
// panics, and a launch it accepts carries a plan the flow can follow.
func FuzzDecodeLaunchPlan(f *testing.F) {
	r := newRand(26)
	for range 8 {
		p := anyFlowPlan(r)
		data, _ := proto.Marshal(wire.Launch(launchWith(r, &p)))
		f.Add(data)
	}
	f.Fuzz(func(t *testing.T, data []byte) {
		var msg cliv1.Launch
		if proto.Unmarshal(data, &msg) != nil {
			return
		}
		l, err := wire.ToLaunch(&msg)
		if err != nil || l.Plan == nil {
			return
		}
		if err := l.Plan.Validate(); err != nil {
			t.Fatalf("an accepted launch has a plan the flow refuses: %v", err)
		}
	})
}
