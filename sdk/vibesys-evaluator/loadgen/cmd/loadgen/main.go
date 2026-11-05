// Command loadgen sends a phase's planned requests and writes its results and summary; see
// sdk/vibesys-evaluator/docs/spec.md ("loadgen command line") and
// proto/vibesys/evaluator/v1/loadgen.proto.
//
// Exit status: 0 when the phase ran and the summary is written (an exhausted or unsustained phase
// included), 2 for a usage error or a plan it cannot run, 1 for any other failure.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"runtime/debug"
	"syscall"
	"time"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/call"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/engine"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/files"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/plan"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/probe"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/results"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/selftest"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/summary"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/version"
)

const (
	exitOK      = 0
	exitFailure = 1
	exitUsage   = 2
)

// gcPercent replaces Go's default of 100 unless GOGC is set: the plan dominates the live heap,
// and a collection every few hundred milliseconds showed up as millisecond send lag.
const gcPercent = 400

func main() {
	if os.Getenv("GOGC") == "" {
		debug.SetGCPercent(gcPercent)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	code := run(ctx, os.Args[1:], os.Stdout, os.Stderr)
	stop()
	os.Exit(code)
}

// usageError is a command line or plan the loadgen cannot run (exit status 2).
type usageError struct{ err error }

func (e usageError) Error() string { return e.err.Error() }
func (e usageError) Unwrap() error { return e.err }

func usage(format string, args ...any) error { return usageError{fmt.Errorf(format, args...)} }

const help = `usage:
  loadgen closed   --target URL --clients N --secs S --warmup W --requests F --results F --summary F
                   [--timeout S] [--format binpb|json]
  loadgen open     --target URL --rate R [--arrivals uniform|poisson] [--seed N] [--max-in-flight N]
                   [--min-sustained RATIO] --secs S --warmup W --requests F --results F --summary F
                   [--timeout S] [--format binpb|json]
  loadgen probe    --target URL --requests F --rounds N --deadline S [--timeout S] [--format binpb|json]
  loadgen selftest --summary F [--step-secs S] [--max-rate R] [--format binpb|json]
  loadgen --capabilities [--format binpb|json]
`

// run is the whole command, in process so tests drive it directly.
func run(ctx context.Context, args []string, stdout, stderr io.Writer) int {
	err := dispatch(ctx, args, stdout, stderr)
	if err == nil {
		return exitOK
	}
	fmt.Fprintln(stderr, "loadgen:", err)
	var usageErr usageError
	switch {
	case errors.Is(err, plan.ErrInvalid):
		return exitUsage
	case errors.As(err, &usageErr):
		fmt.Fprint(stderr, help)
		return exitUsage
	default:
		return exitFailure
	}
}

func dispatch(ctx context.Context, args []string, stdout, stderr io.Writer) error {
	if len(args) == 0 {
		return usage("no command")
	}
	switch args[0] {
	case "closed", "open":
		return phaseCommand(ctx, args[0], args[1:])
	case "probe":
		return probeCommand(ctx, args[1:])
	case "selftest":
		return selftestCommand(ctx, args[1:], stderr)
	case "--capabilities", "-capabilities":
		return capabilitiesCommand(args[1:], stdout)
	default:
		return usage("unknown command %q", args[0])
	}
}

type formatFlag struct{ format files.Format }

func (f *formatFlag) String() string {
	if f.format == files.JSON {
		return "json"
	}
	return "binpb"
}

func (f *formatFlag) Set(value string) error {
	format, err := files.ParseFormat(value)
	f.format = format
	return err
}

func newFlags(name string) (*flag.FlagSet, *formatFlag) {
	set := flag.NewFlagSet(name, flag.ContinueOnError)
	set.SetOutput(io.Discard)
	format := &formatFlag{}
	set.Var(format, "format", "file format: binpb or json")
	return set, format
}

func parse(set *flag.FlagSet, args []string, required ...string) error {
	if err := set.Parse(args); err != nil {
		return usageError{err}
	}
	if set.NArg() > 0 {
		return usage("unexpected argument %q", set.Arg(0))
	}
	seen := map[string]bool{}
	set.Visit(func(f *flag.Flag) { seen[f.Name] = true })
	for _, name := range required {
		if !seen[name] {
			return usage("--%s is required", name)
		}
	}
	return nil
}

func seconds(value float64, name string) (time.Duration, error) {
	if value < 0 || value > 1e6 {
		return 0, usage("--%s must be between 0 and 1e6 seconds", name)
	}
	return time.Duration(value * float64(time.Second)), nil
}

func phaseCommand(ctx context.Context, model string, args []string) error {
	set, format := newFlags(model)
	target := set.String("target", "", "base URL of every step")
	secs := set.Float64("secs", 0, "measurement window, seconds")
	warmup := set.Float64("warmup", 0, "warmup before the window, seconds")
	requests := set.String("requests", "", "requests file")
	resultsPath := set.String("results", "", "results file to write")
	summaryPath := set.String("summary", "", "summary file to write")
	timeout := set.Float64("timeout", 10, "per-step timeout, seconds")
	clients := set.Int("clients", 0, "closed loop: clients")
	rate := set.Float64("rate", 0, "open loop: operations/s")
	arrivals := set.String("arrivals", "poisson", "open loop: uniform or poisson")
	seed := set.Uint64("seed", 0, "open loop: Poisson arrivals seed")
	maxInFlight := set.Int("max-in-flight", 512, "open loop: concurrent operations")
	minSustained := set.Float64("min-sustained", 0.95, "open loop: sustained when offered >= this x rate")
	required := []string{"target", "secs", "warmup", "requests", "results", "summary"}
	if model == "closed" {
		required = append(required, "clients")
	} else {
		required = append(required, "rate")
	}
	if err := parse(set, args, required...); err != nil {
		return err
	}
	var load engine.Load
	conns := *clients
	if model == "closed" {
		load = engine.Closed{Clients: *clients}
	} else {
		kind, ok := map[string]pb.Arrivals{"uniform": pb.Arrivals_ARRIVALS_UNIFORM, "poisson": pb.Arrivals_ARRIVALS_POISSON}[*arrivals]
		if !ok {
			return usage("--arrivals must be uniform or poisson, got %q", *arrivals)
		}
		load = engine.Open{Rate: *rate, Arrivals: kind, Seed: *seed, MaxInFlight: *maxInFlight, MinSustained: *minSustained}
		conns = *maxInFlight
	}
	config := engine.Config{Load: load}
	var err error
	if config.Window, err = seconds(*secs, "secs"); err != nil {
		return err
	}
	if config.Warmup, err = seconds(*warmup, "warmup"); err != nil {
		return err
	}
	if config.Timeout, err = seconds(*timeout, "timeout"); err != nil {
		return err
	}
	if err := config.Validate(); err != nil {
		return usageError{err}
	}
	client, err := call.NewClient(*target, call.Options{Timeout: config.Timeout, Conns: conns})
	if err != nil {
		return usageError{err}
	}
	defer client.Close()
	p, err := readPlan(*requests, format.format)
	if err != nil {
		return err
	}
	summaryMessage, err := runPhase(ctx, config, p, client, *resultsPath, format.format)
	if err != nil {
		return err
	}
	return writeFile(*summaryPath, format.format, pb.FileKind_FILE_KIND_SUMMARY, summaryMessage)
}

func readPlan(path string, format files.Format) (*plan.Plan, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("--requests: %w", err)
	}
	p, err := plan.Parse(data, format)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return p, nil
}

func runPhase(ctx context.Context, config engine.Config, p *plan.Plan, client *call.Client, resultsPath string, format files.Format) (*pb.PhaseSummary, error) {
	out, err := os.Create(resultsPath)
	if err != nil {
		return nil, fmt.Errorf("--results: %w", err)
	}
	defer func() { _ = out.Close() }()
	sink, err := results.New(out, format, results.Options{Writer: version.Writer()})
	if err != nil {
		return nil, fmt.Errorf("--results: %w", err)
	}
	outcome, runErr := engine.Run(ctx, config, p, client, sink)
	spilled, closeErr := sink.Close()
	if runErr != nil {
		return nil, runErr
	}
	if closeErr != nil {
		return nil, fmt.Errorf("write results: %w", closeErr)
	}
	if err := out.Close(); err != nil {
		return nil, fmt.Errorf("write results: %w", err)
	}
	outcome.Phase.ResultsSpilled = spilled
	return outcome.Aggregator.Summary(outcome.Phase)
}

// writeFile writes a one-message file atomically: a reader never sees a partial summary.
func writeFile(path string, format files.Format, kind pb.FileKind, message proto.Message) error {
	temp, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".*")
	if err != nil {
		return err
	}
	defer func() { _ = os.Remove(temp.Name()) }()
	if err := writeMessage(temp, format, kind, message); err != nil {
		_ = temp.Close()
		return err
	}
	if err := temp.Close(); err != nil {
		return err
	}
	return os.Rename(temp.Name(), path)
}

func writeMessage(out io.Writer, format files.Format, kind pb.FileKind, message proto.Message) error {
	writer, err := files.NewWriter(out, format, files.Header(kind, version.Writer()))
	if err != nil {
		return err
	}
	if err := writer.Write(message); err != nil {
		return err
	}
	return writer.Flush()
}

func probeCommand(ctx context.Context, args []string) error {
	set, format := newFlags("probe")
	target := set.String("target", "", "base URL of every step")
	requests := set.String("requests", "", "requests file")
	rounds := set.Int("rounds", 0, "successes per operation")
	deadline := set.Float64("deadline", 0, "seconds until the probe fails")
	timeout := set.Float64("timeout", 10, "per-step timeout, seconds")
	if err := parse(set, args, "target", "requests", "rounds", "deadline"); err != nil {
		return err
	}
	stepTimeout, err := seconds(*timeout, "timeout")
	if err != nil {
		return err
	}
	limit, err := seconds(*deadline, "deadline")
	if err != nil {
		return err
	}
	if *rounds < 1 || stepTimeout <= 0 || limit <= 0 {
		return usage("--rounds, --timeout and --deadline must be positive")
	}
	client, err := call.NewClient(*target, call.Options{Timeout: stepTimeout, Fresh: true})
	if err != nil {
		return usageError{err}
	}
	defer client.Close()
	p, err := readPlan(*requests, format.format)
	if err != nil {
		return err
	}
	probeCtx, cancel := context.WithTimeout(ctx, limit)
	defer cancel()
	return probe.Run(probeCtx, client, p, *rounds)
}

func selftestCommand(ctx context.Context, args []string, stderr io.Writer) error {
	set, format := newFlags("selftest")
	summaryPath := set.String("summary", "", "selftest file to write")
	step := set.Float64("step-secs", 2, "window of each trial rate, seconds")
	maxRate := set.Float64("max-rate", 200_000, "highest rate tried, operations/s")
	if err := parse(set, args, "summary"); err != nil {
		return err
	}
	stepDuration, err := seconds(*step, "step-secs")
	if err != nil {
		return err
	}
	result, err := selftest.Run(ctx, selftest.Options{Step: stepDuration, MaxRate: *maxRate})
	if err != nil {
		return err
	}
	if err := writeFile(*summaryPath, format.format, pb.FileKind_FILE_KIND_SELFTEST, result); err != nil {
		return err
	}
	p50, err := summary.Percentile(result.GetAddedLatency(), 50)
	if err != nil {
		return err
	}
	p99, err := summary.Percentile(result.GetAddedLatency(), 99)
	if err != nil {
		return err
	}
	fmt.Fprintf(stderr, "loadgen selftest: max rate %.0f ops/s on %.1f CPUs (%.0f per CPU); at %.0f ops/s it adds p50 %d us, p99 %d us, CPU %.1f%%\n",
		result.GetMaxRate(), result.GetCpus(), result.GetMaxRate()/result.GetCpus(), result.GetAddedLatencyRate(), p50, p99, 100*result.GetLoadgenCpuFraction())
	return nil
}

func capabilitiesCommand(args []string, stdout io.Writer) error {
	set, format := newFlags("capabilities")
	if err := parse(set, args); err != nil {
		return err
	}
	return writeMessage(stdout, format.format, pb.FileKind_FILE_KIND_CAPABILITIES, version.Capabilities())
}
