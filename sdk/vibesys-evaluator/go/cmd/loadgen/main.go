// Command loadgen runs a phase: it starts the task's request generator, reads the request stream
// from its standard output while it sends the requests, and writes the results and summary; see
// sdk/vibesys-evaluator/docs/spec.md ("loadgen command line") and
// proto/vibesys/evaluator/v1/loadgen.proto.
//
// Exit status: 0 when the phase ran and the summary is written (an unsustained phase included), 2
// for a usage error or a request stream it cannot run, 1 for any other failure (a generator that
// ends its stream early).
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime/debug"
	"slices"
	"sync"
	"syscall"
	"time"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/call"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/engine"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/files"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/plan"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/probe"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/results"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/selftest"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/summary"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go/internal/version"
)

const (
	exitOK      = 0
	exitFailure = 1
	exitUsage   = 2
)

// gcPercent replaces Go's default of 100 unless GOGC is set: a collection every few hundred
// milliseconds showed up as millisecond send lag.
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
  loadgen closed   --target URL --clients N --secs S --warmup W --results F --summary F
                   [--timeout S] [--buffer N] [--record-clients] [--format binpb|json]
                   -- GENERATOR [ARG...]
  loadgen open     --target URL --rate R [--arrivals uniform|poisson] [--seed N] [--max-in-flight N]
                   [--min-sustained RATIO] --secs S --warmup W --results F --summary F
                   [--timeout S] [--buffer N] [--record-clients] [--format binpb|json]
                   -- GENERATOR [ARG...]
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
		return phaseCommand(ctx, args[0], args[1:], stderr)
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

// defaultBuffer is the operations loadgen decodes ahead of the senders (--buffer).
const defaultBuffer = 8192

func phaseCommand(ctx context.Context, model string, args []string, stderr io.Writer) error {
	started := time.Now()
	split := slices.Index(args, "--")
	if split < 0 || split == len(args)-1 {
		return usage("the generator command goes after \"--\"")
	}
	generator := args[split+1:]
	set, format := newFlags(model)
	target := set.String("target", "", "base URL of every step")
	secs := set.Float64("secs", 0, "measurement window, seconds")
	warmup := set.Float64("warmup", 0, "warmup before the window, seconds")
	resultsPath := set.String("results", "", "results file to write")
	summaryPath := set.String("summary", "", "summary file to write")
	timeout := set.Float64("timeout", 10, "per-step timeout, seconds")
	buffer := set.Int("buffer", defaultBuffer, "operations decoded ahead of the senders")
	clients := set.Int("clients", 0, "closed loop: clients")
	rate := set.Float64("rate", 0, "open loop: operations/s")
	arrivals := set.String("arrivals", "poisson", "open loop: uniform or poisson")
	seed := set.Uint64("seed", 0, "open loop: Poisson arrivals seed")
	maxInFlight := set.Int("max-in-flight", 512, "open loop: concurrent operations")
	minSustained := set.Float64("min-sustained", 0.95, "open loop: sustained when offered >= this x rate")
	recordClients := set.Bool("record-clients", false, "record the client of each operation in its result")
	required := []string{"target", "secs", "warmup", "results", "summary"}
	if model == "closed" {
		required = append(required, "clients")
	} else {
		required = append(required, "rate")
	}
	if err := parse(set, args[:split], required...); err != nil {
		return err
	}
	if *buffer < 1 || *buffer > 1<<22 {
		return usage("--buffer must be between 1 and %d", 1<<22)
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
	config := engine.Config{Load: load, RecordClients: *recordClients}
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
	gen, err := startGenerator(ctx, generator, stderr)
	if err != nil {
		return err
	}
	defer gen.stop()
	stream, err := plan.OpenStream(gen.out, *buffer, started)
	if err != nil {
		return gen.failed(err)
	}
	// In reverse: stop the pipeline, close the stream (which ends the generator), then wait.
	defer stream.Wait()
	defer gen.stop()
	defer stream.Stop()
	if err := stream.WaitReady(ctx); err != nil {
		return gen.failed(err)
	}
	summaryMessage, err := runPhase(ctx, config, stream, client, *resultsPath, format.format, func() *pb.GeneratorStats {
		stream.Stop()
		return stream.Stats()
	})
	if err != nil {
		return gen.failed(err)
	}
	return writeFile(*summaryPath, format.format, pb.FileKind_FILE_KIND_SUMMARY, summaryMessage)
}

// generatorGrace is how long a generator has to exit after loadgen closed its stream.
const generatorGrace = 10 * time.Second

// generatorProcess is the running generator command: its stream on out, its log on loadgen's
// standard error.
type generatorProcess struct {
	cmd  *exec.Cmd
	out  *os.File
	done chan error
	argv []string
	once sync.Once
}

func startGenerator(ctx context.Context, argv []string, stderr io.Writer) (*generatorProcess, error) {
	read, write, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	// argv is the generator command the caller named after "--".
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Stdout = write
	cmd.Stderr = stderr
	cmd.WaitDelay = generatorGrace
	if err := cmd.Start(); err != nil {
		_ = read.Close()
		_ = write.Close()
		return nil, usage("cannot start the generator %q: %v", argv[0], err)
	}
	_ = write.Close()
	g := &generatorProcess{cmd: cmd, out: read, done: make(chan error, 1), argv: argv}
	go func() { g.done <- cmd.Wait() }()
	return g, nil
}

// stop closes the stream, which ends the generator (a write to a closed pipe fails), and waits
// for it to exit, killing it after generatorGrace. Its exit status no longer matters.
func (g *generatorProcess) stop() {
	g.once.Do(func() {
		_ = g.out.Close()
		select {
		case <-g.done:
		case <-time.After(generatorGrace):
			_ = g.cmd.Process.Kill()
			<-g.done
		}
	})
}

// failed is err, with the generator's exit status when it exited while loadgen read its stream.
func (g *generatorProcess) failed(err error) error {
	if errors.Is(err, plan.ErrStreamEnded) {
		select {
		case exit := <-g.done:
			g.done <- exit
			if exit != nil {
				return fmt.Errorf("the generator %q failed (%v): %w", g.argv[0], exit, err)
			}
		case <-time.After(generatorGrace):
		}
	}
	return err
}

// readPlan reads probe's ready steps: a finite request stream.
func readPlan(path string, format files.Format) (*plan.Plan, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("--requests: %w", err)
	}
	defer func() { _ = file.Close() }()
	p, err := plan.Read(file, format)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return p, nil
}

// runPhase runs the phase and folds its summary; stats stops the source and returns its own
// GeneratorStats once the phase is over.
func runPhase(ctx context.Context, config engine.Config, source engine.Source, client *call.Client, resultsPath string, format files.Format, stats func() *pb.GeneratorStats) (*pb.PhaseSummary, error) {
	out, err := os.Create(resultsPath)
	if err != nil {
		return nil, fmt.Errorf("--results: %w", err)
	}
	defer func() { _ = out.Close() }()
	sink, err := results.New(out, format, results.Options{Writer: version.Writer()})
	if err != nil {
		return nil, fmt.Errorf("--results: %w", err)
	}
	outcome, runErr := engine.Run(ctx, config, source, client, sink)
	generator := stats()
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
	outcome.Phase.Generator = generator
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
