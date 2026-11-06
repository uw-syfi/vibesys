// Package phase is one microservices load phase on both sides of the phase pod: the PhaseInput
// the orchestrator writes and how it describes the load, what the pod does (loadgen sends the
// generator's request stream; the pod's files), and the judge of the pod's output (the
// saturation guard and the task's validity rules). Package microservices (the Go benchmark
// author's API) and `vibesys-evaluator phase` (the Python SDK's) are built on it. See
// sdk/vibesys-evaluator/docs/spec.md, "A phase".
package phase

import (
	"fmt"
	"math"
	"strconv"
	"time"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/workload"
)

// The files of a phase's work directory.
const (
	// InputFile is the PhaseInput (binpb), written by the orchestrator before the pod starts.
	InputFile = "input.binpb"
	// OutputFile is the PhaseOutput (binpb) the pod leaves: its presence means loadgen ran.
	OutputFile = "output.binpb"
	// SummaryFile is loadgen's summary file.
	SummaryFile = "summary.binpb"
	// ResultsFile is loadgen's results file, whole: what the pod leaves under capture.
	ResultsFile = "results.binpb"
	// PackedResultsFile is loadgen's results file, gzip-compressed: what a benchmark's pod leaves.
	PackedResultsFile = "results.binpb.gz"
	// KeptFile is the generator's KeptOperations, which the pod leaves under capture.
	KeptFile = "kept.binpb"
	// DatasetFile is the dataset file the generator reads (--dataset).
	DatasetFile = "dataset.json"
	// LogFile is the pod's combined output, which the orchestrator writes into the phase's
	// directory.
	LogFile = "phase.log"
)

const (
	// StepTimeout is loadgen's per-step timeout (its default): it bounds how long a phase runs
	// past its window.
	StepTimeout = 10 * time.Second
	// loadgenMargin is the time loadgen may take besides warmup, window and spill: the
	// generator's start-up and the files.
	loadgenMargin = 300 * time.Second
	// Overhead is the time a phase pod may take besides its warmup and window: its start, the
	// generator's start-up, the spill after the window and copying the files.
	Overhead = 1500 * time.Second
)

// Input is the PhaseInput of phase index of the sample seed: closed loop with clients > 0, else
// open loop at rate with arrivals.
func Input(seed uint64, index int, load *pb.PhaseInput, capture bool) *pb.PhaseInput {
	return &pb.PhaseInput{
		SampleSeed: seed, PhaseIndex: uint32(index), Model: load.GetModel(), Clients: load.GetClients(),
		Rate: load.GetRate(), Arrivals: load.GetArrivals(), Secs: load.GetSecs(), WarmupSecs: load.GetWarmupSecs(),
		Capture: capture,
	}
}

// Validate rejects a PhaseInput no phase can run, naming the field.
func Validate(in *pb.PhaseInput) error {
	switch in.GetModel() {
	case pb.LoadModel_LOAD_MODEL_CLOSED:
		if in.GetClients() < 1 {
			return fmt.Errorf("a closed loop needs clients >= 1, got %d", in.GetClients())
		}
	case pb.LoadModel_LOAD_MODEL_OPEN:
		if !(in.GetRate() > 0) || math.IsInf(in.GetRate(), 0) {
			return fmt.Errorf("an open loop needs a finite rate > 0, got %v", in.GetRate())
		}
		if a := in.GetArrivals(); a != pb.Arrivals_ARRIVALS_POISSON && a != pb.Arrivals_ARRIVALS_UNIFORM {
			return fmt.Errorf("an open loop's arrivals are poisson or uniform, got %v", a)
		}
	default:
		return fmt.Errorf("the load model is closed or open, got %v", in.GetModel())
	}
	if !(in.GetSecs() > 0) || math.IsInf(in.GetSecs(), 0) {
		return fmt.Errorf("the window must be a finite number of seconds > 0, got %v", in.GetSecs())
	}
	if !(in.GetWarmupSecs() >= 0) || math.IsInf(in.GetWarmupSecs(), 0) {
		return fmt.Errorf("the warmup must be a finite number of seconds >= 0, got %v", in.GetWarmupSecs())
	}
	return nil
}

// Describe is the load of a phase for messages: "closed loop, 128 clients, 30 s after 10 s
// warmup", "open loop at 3304.0 ops/s (uniform), 30 s after 10 s warmup".
func Describe(in *pb.PhaseInput) string {
	window := g(in.GetSecs()) + " s after " + g(in.GetWarmupSecs()) + " s warmup"
	if in.GetModel() == pb.LoadModel_LOAD_MODEL_CLOSED {
		return fmt.Sprintf("closed loop, %d clients, %s", in.GetClients(), window)
	}
	mode := "uniform"
	if in.GetArrivals() == pb.Arrivals_ARRIVALS_POISSON {
		mode = "poisson"
	}
	return fmt.Sprintf("open loop at %.1f ops/s (%s), %s", in.GetRate(), mode, window)
}

// Timeout is the most a phase pod may take, from its start to its files copied back.
func Timeout(in *pb.PhaseInput) time.Duration {
	return seconds(in.GetWarmupSecs()+in.GetSecs()) + Overhead
}

// ArrivalsSeed is the open loop's Poisson seed: drawn from the phase stream.
func ArrivalsSeed(in *pb.PhaseInput) uint64 {
	return workload.NewRand(in.GetSampleSeed(), "phase", in.GetPhaseIndex()).Child("arrivals").Uint64() % (1 << 63)
}

// LoadgenArgs is loadgen's command line for the phase, the generator command after "--".
func LoadgenArgs(in *pb.PhaseInput, target, results, summary string, minSustained float64, generator []string) []string {
	common := []string{
		"--secs", pyRepr(in.GetSecs()), "--warmup", pyRepr(in.GetWarmupSecs()),
		"--results", results, "--summary", summary, "--timeout", pyRepr(StepTimeout.Seconds()),
	}
	if in.GetCapture() {
		common = append(common, "--record-clients")
	}
	stream := append([]string{"--"}, generator...)
	if in.GetModel() == pb.LoadModel_LOAD_MODEL_CLOSED {
		args := append([]string{"closed", "--target", target, "--clients", strconv.FormatUint(uint64(in.GetClients()), 10)}, common...)
		return append(args, stream...)
	}
	mode := "uniform"
	if in.GetArrivals() == pb.Arrivals_ARRIVALS_POISSON {
		mode = "poisson"
	}
	args := []string{
		"open", "--target", target, "--rate", pyRepr(in.GetRate()), "--arrivals", mode,
		"--seed", strconv.FormatUint(ArrivalsSeed(in), 10), "--min-sustained", pyRepr(minSustained),
	}
	return append(append(args, common...), stream...)
}

func seconds(s float64) time.Duration { return time.Duration(s * float64(time.Second)) }

// g formats a number as Python's format(x, "g") does for the values a phase has (seconds).
func g(x float64) string { return strconv.FormatFloat(x, 'g', 6, 64) }

// pyRepr formats a float as Python's repr does: the shortest text that reads back, with ".0" on
// an integral value.
func pyRepr(x float64) string {
	text := strconv.FormatFloat(x, 'g', -1, 64)
	for _, c := range text {
		if c == '.' || c == 'e' || c == 'n' || c == 'I' {
			return text
		}
	}
	return text + ".0"
}
