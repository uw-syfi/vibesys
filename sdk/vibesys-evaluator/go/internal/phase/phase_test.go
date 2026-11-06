package phase

import (
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/evaluator"
	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
)

func TestLoadgenGetsThePhaseOnItsCommandLine(t *testing.T) {
	closed := Input(7, 0, &pb.PhaseInput{Model: pb.LoadModel_LOAD_MODEL_CLOSED, Clients: 16, Secs: 30, WarmupSecs: 10}, false)
	got := LoadgenArgs(closed, "http://kv:8080", "/r", "/s", 0.95, []string{"gen", "generate"})
	want := []string{"closed", "--target", "http://kv:8080", "--clients", "16", "--secs", "30.0", "--warmup", "10.0",
		"--results", "/r", "--summary", "/s", "--timeout", "10.0", "--", "gen", "generate"}
	if !slices.Equal(got, want) {
		t.Errorf("closed:\n got %q\nwant %q", got, want)
	}
	open := Input(7, 1, &pb.PhaseInput{Model: pb.LoadModel_LOAD_MODEL_OPEN, Rate: 3304.5, Arrivals: pb.Arrivals_ARRIVALS_UNIFORM, Secs: 0.5}, true)
	got = LoadgenArgs(open, "http://kv:8080", "/r", "/s", 0.99, []string{"gen"})
	// The Poisson seed is the Python SDK's Rand(7, "phase", 1).child("arrivals").below(2**63).
	want = []string{"open", "--target", "http://kv:8080", "--rate", "3304.5", "--arrivals", "uniform",
		"--seed", "1854176363839245373", "--min-sustained", "0.99", "--secs", "0.5", "--warmup", "0.0",
		"--results", "/r", "--summary", "/s", "--timeout", "10.0", "--record-clients", "--", "gen"}
	if !slices.Equal(got, want) {
		t.Errorf("open:\n got %q\nwant %q", got, want)
	}
	if d := Describe(open); d != "open loop at 3304.5 ops/s (uniform), 0.5 s after 0 s warmup" {
		t.Errorf("describe %q", d)
	}
}

func TestAPhaseInputIsValidated(t *testing.T) {
	for _, in := range []*pb.PhaseInput{
		{Model: pb.LoadModel_LOAD_MODEL_CLOSED, Secs: 1},
		{Model: pb.LoadModel_LOAD_MODEL_OPEN, Rate: 10, Secs: 1},
		{Model: pb.LoadModel_LOAD_MODEL_OPEN, Rate: 10, Arrivals: pb.Arrivals_ARRIVALS_POISSON},
		{Clients: 1, Secs: 1},
	} {
		if Validate(in) == nil {
			t.Errorf("%v was accepted", in)
		}
	}
}

func TestTheGuardAndTheRulesJudgeTheSummary(t *testing.T) {
	base := func() *pb.PhaseSummary {
		return &pb.PhaseSummary{
			Model: pb.LoadModel_LOAD_MODEL_OPEN, TargetRate: 100, OfferedRate: 100, AchievedRate: 100, Completed: 100,
			OperationNames: []string{"get", "put"},
			Operations:     []*pb.OperationSummary{{Operation: 0, Completed: 90}, {Operation: 1, Completed: 10}},
		}
	}
	validity := evaluator.DefaultValidity()
	for name, c := range map[string]struct {
		edit func(*pb.PhaseSummary)
		want string
	}{
		"measured":     {func(*pb.PhaseSummary) {}, ""},
		"generator":    {func(s *pb.PhaseSummary) { s.Generator = &pb.GeneratorStats{Waited: 2, Rate: 50} }, "generator fell behind"},
		"loadgen cpu":  {func(s *pb.PhaseSummary) { s.LoadgenCpuFraction = 0.9 }, "may be the bottleneck"},
		"unsustained":  {func(s *pb.PhaseSummary) { s.OfferedRate = 90 }, "not sustained"},
		"errors":       {func(s *pb.PhaseSummary) { s.Errors = 1 }, "1 loadgen errors over 100 operations"},
		"an operation": {func(s *pb.PhaseSummary) { s.Operations = s.Operations[:1] }, "put (0)"},
		"nothing":      {func(s *pb.PhaseSummary) { s.Completed = 0 }, "no operation completed"},
	} {
		s := base()
		c.edit(s)
		verdict, err := Judge(&pb.PhaseOutput{Summary: s}, validity)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if c.want == "" && verdict.Failure != "" || !strings.Contains(verdict.Failure, c.want) {
			t.Errorf("%s: failure %q, want %q", name, verdict.Failure, c.want)
		}
	}
}
