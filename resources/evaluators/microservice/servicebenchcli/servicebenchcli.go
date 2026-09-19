// Package servicebenchcli provides reusable servicebench command orchestration.
package servicebenchcli

import (
	"bufio"
	"context"
	cryptorand "crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/uw-syfi/vibesys/sdk/vs-evaluator/vseval"

	"vibesys/microservice-evaluator/accuracy"
	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/composition"
	"vibesys/microservice-evaluator/config"
	"vibesys/microservice-evaluator/engine"
	"vibesys/microservice-evaluator/fsutil"
	"vibesys/microservice-evaluator/lifecycle"
	"vibesys/microservice-evaluator/registry"
	"vibesys/microservice-evaluator/telemetry"
)

type targetOverrides map[string]string

type modeFlagConfig struct {
	mode               string
	trace              bool
	explicit           map[string]bool
	outputRaw          string
	streamOutput       string
	validateOnly       bool
	casesMin           int
	casesMax           int
	startupTimeout     float64
	runCommandJSON     string
	stopCommandJSON    string
	cleanupCommandJSON string
	stateDir           string
	stateEnv           string
	telemetryCommand   string
	telemetryOutput    string
	telemetryTimeout   float64
	traceGraphOutput   string
	traceTextOutput    string
	traceMaxRoots      int
	traceMaxNodes      int
	traceTimelineWidth int
}

func (o *targetOverrides) String() string {
	return fmt.Sprint(map[string]string(*o))
}

func (o *targetOverrides) Set(value string) error {
	name, address, ok := strings.Cut(value, "=")
	if !ok || name == "" || address == "" {
		return fmt.Errorf("target override must be NAME=ADDRESS")
	}
	(*o)[name] = address
	return nil
}

// Run parses and executes one servicebench invocation using only the supplied
// component registrations.
func Run(
	args []string,
	engineVersion string,
	registrations ...composition.Registration,
) (resultErr error) {
	if engineVersion == "" {
		return errors.New("servicebench engine version must not be empty")
	}
	traceMode, commandArgs, err := parseServicebenchCommand(args)
	if err != nil {
		return err
	}
	flags := flag.NewFlagSet("servicebench", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	var mode string
	var workloadPath string
	var profile string
	var outputJSON string
	var outputRaw string
	var baseURL string
	var skipPrepare bool
	var validateOnly bool
	var overrides targetOverrides = make(map[string]string)
	var rate float64
	var duration float64
	var warmup float64
	var concurrency int
	var repetitions int
	var seed string
	var fixtureSeed string
	var casesMin int
	var casesMax int
	var startupTimeout float64
	var runCommandJSON string
	var stopCommandJSON string
	var cleanupCommandJSON string
	var candidateDir string
	var stateDir string
	var stateEnv string
	var telemetryCommandJSON string
	var telemetryOutput string
	var telemetryTimeout float64
	var traceGraphOutput string
	var traceTextOutput string
	var traceMaxRoots int
	var traceMaxNodes int
	var traceTimelineWidth int

	flags.StringVar(&mode, "mode", "benchmark", "execution mode: benchmark or accuracy")
	flags.StringVar(&workloadPath, "workload", "", "path to the workload TOML file")
	flags.StringVar(&profile, "profile", "", "optional named workload profile")
	flags.StringVar(&outputJSON, "output-json", "", "optional structured result path")
	flags.StringVar(&outputRaw, "output-raw", "", "optional per-operation NDJSON path")
	flags.StringVar(&baseURL, "base-url", "", "override every HTTP target address")
	flags.BoolVar(&skipPrepare, "skip-prepare", false, "skip application fixture preparation")
	flags.BoolVar(&validateOnly, "validate-only", false, "validate configuration and registered extensions without connecting")
	flags.Var(&overrides, "target", "override a target address as NAME=ADDRESS (repeatable)")
	flags.Float64Var(&rate, "rate", 0, "override open-loop target logical operations per second")
	flags.Float64Var(&duration, "duration", 0, "override measured duration in seconds")
	flags.Float64Var(&warmup, "warmup", -1, "override warmup duration in seconds")
	flags.IntVar(&concurrency, "concurrency", 0, "override maximum in-flight logical operations")
	flags.IntVar(&repetitions, "repetitions", 0, "override independent trial count")
	flags.StringVar(&seed, "seed", "", "override deterministic random seed, or use 'random'")
	flags.StringVar(&fixtureSeed, "fixture-seed", "", "override the fixture namespace seed, or use 'random'")
	flags.IntVar(&casesMin, "cases-min", 2, "minimum randomized cases in accuracy mode")
	flags.IntVar(&casesMax, "cases-max", 5, "maximum randomized cases in accuracy mode")
	flags.Float64Var(&startupTimeout, "startup-timeout", 15, "candidate readiness timeout in seconds")
	flags.StringVar(&runCommandJSON, "run-command-json", "", "managed candidate command as a JSON string array")
	flags.StringVar(&stopCommandJSON, "stop-command-json", "", "managed candidate stop or restart command as a JSON string array")
	flags.StringVar(&cleanupCommandJSON, "cleanup-command-json", "", "managed candidate final cleanup command as a JSON string array")
	flags.StringVar(&candidateDir, "candidate-dir", ".", "managed candidate working directory")
	flags.StringVar(&stateDir, "state-dir", "", "managed candidate persistent state directory in accuracy mode")
	flags.StringVar(&stateEnv, "state-env", "VIBESYS_STATE_DIR", "environment variable receiving --state-dir in accuracy mode")
	flags.StringVar(&telemetryCommandJSON, "telemetry-command-json", "", "trusted telemetry collector command as a JSON string array")
	flags.StringVar(&telemetryOutput, "telemetry-output", "", "normalized telemetry report path")
	flags.Float64Var(&telemetryTimeout, "telemetry-timeout", 30, "telemetry collector timeout in seconds")
	flags.StringVar(&traceGraphOutput, "trace-graph-json", "", "versioned trace graph output path")
	flags.StringVar(&traceTextOutput, "trace-graph-text", "", "optional rendered trace graph output path")
	flags.IntVar(&traceMaxRoots, "trace-max-roots", 10, "maximum root groups in rendered trace output")
	flags.IntVar(&traceMaxNodes, "trace-max-nodes", 30, "maximum nodes per root in rendered trace output")
	flags.IntVar(&traceTimelineWidth, "trace-timeline-width", 48, "representative waterfall width")
	// The SDK owns the name and usage of the result-stream flag. Registering it
	// here rather than through vseval.Open keeps flag parsing in one place.
	streamOutput := vseval.RegisterFlags(flags)
	if err := flags.Parse(commandArgs); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	// The stream opens before the first thing that can fail, so a rejected flag
	// combination or a workload that does not load reaches the framework as an
	// error record instead of as a missing file. Close runs last; finish
	// reports whatever the command returns, including a managed-candidate
	// cleanup failure, as the stream's error record.
	stream, err := openBenchmarkStream(*streamOutput)
	if err != nil {
		return err
	}
	defer stream.Close()
	defer func() { resultErr = stream.finish(resultErr) }()
	explicitFlags := make(map[string]bool)
	flags.Visit(func(used *flag.Flag) { explicitFlags[used.Name] = true })
	if err := validateModeFlags(modeFlagConfig{
		mode: mode, trace: traceMode, explicit: explicitFlags, outputRaw: outputRaw,
		streamOutput: *streamOutput, validateOnly: validateOnly,
		casesMin: casesMin, casesMax: casesMax, startupTimeout: startupTimeout,
		runCommandJSON: runCommandJSON, stopCommandJSON: stopCommandJSON,
		cleanupCommandJSON: cleanupCommandJSON,
		stateDir:           stateDir, stateEnv: stateEnv,
		telemetryCommand: telemetryCommandJSON, telemetryOutput: telemetryOutput,
		telemetryTimeout: telemetryTimeout,
		traceGraphOutput: traceGraphOutput, traceTextOutput: traceTextOutput,
		traceMaxRoots: traceMaxRoots, traceMaxNodes: traceMaxNodes,
		traceTimelineWidth: traceTimelineWidth,
	}); err != nil {
		return err
	}
	if workloadPath == "" {
		return errors.New("--workload is required")
	}

	workload, err := config.Load(workloadPath, profile)
	if err != nil {
		return err
	}
	if rate != 0 {
		workload.Load.Rate = rate
	}
	if duration != 0 {
		workload.Load.DurationSeconds = duration
	}
	if warmup >= 0 {
		workload.Load.WarmupSeconds = warmup
	}
	if concurrency != 0 {
		workload.Load.Concurrency = concurrency
	}
	if repetitions != 0 {
		workload.Load.Repetitions = repetitions
	}
	if seed != "" {
		parsed, parseErr := parseSeed(seed, "--seed")
		if parseErr != nil {
			return parseErr
		}
		workload.Load.Seed = parsed
	}
	if fixtureSeed != "" {
		parsed, parseErr := parseSeed(fixtureSeed, "--fixture-seed")
		if parseErr != nil {
			return parseErr
		}
		workload.Load.FixtureSeed = parsed
	}
	for index := range workload.Targets {
		if baseURL != "" && workload.Targets[index].Protocol == "http" {
			workload.Targets[index].Address = baseURL
		}
		if address, ok := overrides[workload.Targets[index].Name]; ok {
			workload.Targets[index].Address = address
			delete(overrides, workload.Targets[index].Name)
		}
	}
	if len(overrides) > 0 {
		return fmt.Errorf("target overrides reference unknown targets: %v", overrides)
	}
	if err := config.Validate(workload); err != nil {
		return fmt.Errorf("invalid command-line override: %w", err)
	}
	// The metrics are the resolved workload's own, so the schema is declared
	// here, once it is valid and before anything is measured.
	if err := stream.declare(workload.Objective); err != nil {
		return err
	}
	canonical, err := config.CanonicalJSON(workload)
	if err != nil {
		return fmt.Errorf("serialize resolved workload: %w", err)
	}
	hash := sha256.Sum256(canonical)

	registry, err := composition.NewRegistry(registrations...)
	if err != nil {
		return err
	}
	if mode == "benchmark" {
		if _, err := registry.Application(workload); err != nil {
			return err
		}
	} else {
		if _, err := registry.AccuracyApplication(workload); err != nil {
			return err
		}
	}
	for _, target := range workload.Targets {
		if _, err := registry.Driver(target.Protocol); err != nil {
			return fmt.Errorf("target %q: %w", target.Name, err)
		}
	}

	seedDisplay := strconv.FormatInt(workload.Load.Seed, 10)
	fixtureSeedDisplay := strconv.FormatInt(workload.Load.FixtureSeed, 10)
	if mode == "accuracy" {
		seedDigest := sha256.Sum256([]byte(seedDisplay))
		seedDisplay = fmt.Sprintf("sha256:%x", seedDigest[:8])
		fixtureSeedDigest := sha256.Sum256([]byte(fixtureSeedDisplay))
		fixtureSeedDisplay = fmt.Sprintf("sha256:%x", fixtureSeedDigest[:8])
	}
	fmt.Fprintf(
		os.Stderr,
		"workload=%s application=%s model=%s rate=%.2f duration=%.2fs warmup=%.2fs concurrency=%d repetitions=%d seed=%s fixture_seed=%s\n",
		workload.Name,
		workload.Application,
		workload.Load.Model,
		workload.Load.Rate,
		workload.Load.DurationSeconds,
		workload.Load.WarmupSeconds,
		workload.Load.Concurrency,
		workload.Load.Repetitions,
		seedDisplay,
		fixtureSeedDisplay,
	)
	for _, target := range workload.Targets {
		fmt.Fprintf(os.Stderr, "target=%s protocol=%s address=%s session_policy=%s\n", target.Name, target.Protocol, target.Address, target.SessionPolicy)
	}
	if validateOnly {
		fmt.Printf("workload is valid for %s mode\n", mode)
		return nil
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if mode == "accuracy" {
		return runAccuracy(
			ctx,
			registry,
			workload,
			engineVersion,
			outputJSON,
			casesMin,
			casesMax,
			startupTimeout,
			runCommandJSON,
			stopCommandJSON,
			cleanupCommandJSON,
			candidateDir,
			stateDir,
			stateEnv,
		)
	}
	var managed *lifecycle.ManagedCandidate
	if runCommandJSON != "" {
		command, parseErr := parseCommandJSON(runCommandJSON, "--run-command-json")
		if parseErr != nil {
			return parseErr
		}
		var stopCommand []string
		if stopCommandJSON != "" {
			stopCommand, parseErr = parseCommandJSON(stopCommandJSON, "--stop-command-json")
			if parseErr != nil {
				return parseErr
			}
		}
		var cleanupCommand []string
		if cleanupCommandJSON != "" {
			cleanupCommand, parseErr = parseCommandJSON(cleanupCommandJSON, "--cleanup-command-json")
			if parseErr != nil {
				return parseErr
			}
		}
		managed, err = lifecycle.NewManagedCandidate(command, stopCommand, cleanupCommand, candidateDir, nil)
		if err != nil {
			return err
		}
		if err := managed.Prepare(ctx); err != nil {
			return fmt.Errorf("prepare managed candidate: %w", err)
		}
		defer func() {
			if closeErr := managed.Close(context.WithoutCancel(ctx)); closeErr != nil && resultErr == nil {
				resultErr = fmt.Errorf("close managed candidate: %w", closeErr)
			}
		}()
	}
	runner := engine.New(registry, engine.Options{
		EngineVersion:  engineVersion,
		WorkloadHash:   hex.EncodeToString(hash[:]),
		SkipPrepare:    skipPrepare,
		StartupTimeout: time.Duration(startupTimeout * float64(time.Second)),
	})
	runResult, err := runner.Run(ctx, workload)
	if err != nil {
		return err
	}
	var renderedTrace string
	if shouldCollectTelemetry(telemetryCommandJSON, runResult.Summary.Valid) {
		command, parseErr := parseCommandJSON(
			telemetryCommandJSON,
			"--telemetry-command-json",
		)
		if parseErr != nil {
			return parseErr
		}
		windows := make([]telemetry.MeasurementWindow, 0, len(runResult.Summary.Trials))
		for _, trial := range runResult.Summary.Trials {
			windows = append(windows, trial.MeasurementWindow)
		}
		collector := telemetry.CommandCollector{
			Command:        command,
			OutputPath:     telemetryOutput,
			TraceGraphPath: traceGraphOutput,
			Timeout:        time.Duration(telemetryTimeout * float64(time.Second)),
		}
		request := telemetry.CollectionRequest{
			SchemaVersion: telemetry.RequestSchemaVersion,
			WorkloadName:  workload.Name,
			WorkloadHash:  hex.EncodeToString(hash[:]),
			Windows:       windows,
		}
		if traceMode {
			artifacts, collectErr := collector.CollectTrace(ctx, request)
			if collectErr != nil {
				return fmt.Errorf("collect benchmark trace graph: %w", collectErr)
			}
			runResult.Summary.Telemetry = &artifacts.Report
			renderedTrace, err = telemetry.RenderTraceGraph(artifacts.TraceGraph, telemetry.TraceRenderOptions{MaxRoots: traceMaxRoots, MaxNodesPerRoot: traceMaxNodes, TimelineWidth: traceTimelineWidth})
			if err != nil {
				return fmt.Errorf("render benchmark trace graph: %w", err)
			}
			if traceTextOutput != "" {
				if err := fsutil.WriteFileAtomic(traceTextOutput, []byte(renderedTrace), 0o644); err != nil {
					return fmt.Errorf("write rendered trace graph: %w", err)
				}
			}
		} else {
			report, collectErr := collector.Collect(ctx, request)
			if collectErr != nil {
				return fmt.Errorf("collect benchmark telemetry: %w", collectErr)
			}
			runResult.Summary.Telemetry = &report
		}
	}
	if outputRaw != "" {
		if err := writeNDJSON(outputRaw, runResult.Observations); err != nil {
			return err
		}
	}
	encoded, err := json.MarshalIndent(runResult.Summary, "", "  ")
	if err != nil {
		return fmt.Errorf("encode result: %w", err)
	}
	if outputJSON != "" {
		if err := fsutil.WriteFileAtomic(outputJSON, append(encoded, '\n'), 0o600); err != nil {
			return err
		}
	}
	if traceMode {
		fmt.Print(renderedTrace)
	} else {
		fmt.Println(string(encoded))
	}
	return stream.emit(runResult.Summary)
}

// shouldCollectTelemetry reports whether servicebench invokes the telemetry
// collector after a benchmark run. Telemetry is diagnostic evidence for a valid
// measured workload, so an invalid benchmark skips collection: the run already
// fails on its trial invalid_reasons, and invoking the collector would waste an
// invocation and let a collector fault replace those reasons with a telemetry
// error.
func shouldCollectTelemetry(command string, valid bool) bool {
	return command != "" && valid
}

func parseServicebenchCommand(args []string) (bool, []string, error) {
	if len(args) == 0 || strings.HasPrefix(args[0], "-") {
		return false, args, nil
	}
	if args[0] != "trace" {
		return false, nil, fmt.Errorf("unknown subcommand %q", args[0])
	}
	return true, args[1:], nil
}

func validateModeFlags(config modeFlagConfig) error {
	if config.mode != "benchmark" && config.mode != "accuracy" {
		return fmt.Errorf("--mode must be benchmark or accuracy, got %q", config.mode)
	}
	if config.startupTimeout <= 0 {
		return fmt.Errorf("--startup-timeout must be positive")
	}
	if config.trace {
		if config.mode != "benchmark" {
			return errors.New("servicebench trace only supports benchmark mode")
		}
		if config.telemetryCommand == "" || config.telemetryOutput == "" {
			return errors.New("servicebench trace requires --telemetry-command-json and --telemetry-output")
		}
		if config.traceGraphOutput == "" {
			return errors.New("servicebench trace requires --trace-graph-json")
		}
		if config.traceMaxRoots <= 0 || config.traceMaxNodes <= 0 || config.traceTimelineWidth < 10 {
			return errors.New("trace render limits must be positive and --trace-timeline-width must be at least 10")
		}
	} else if config.traceGraphOutput != "" || config.traceTextOutput != "" ||
		config.explicit["trace-max-roots"] || config.explicit["trace-max-nodes"] || config.explicit["trace-timeline-width"] {
		return errors.New("trace graph flags require the servicebench trace subcommand")
	}
	if config.streamOutput != "" && config.validateOnly {
		return fmt.Errorf(
			"--%s reports a measurement, so it cannot be combined with --validate-only",
			vseval.OutputFlag,
		)
	}
	accuracyOnly := []string{"cases-min", "cases-max", "state-dir", "state-env"}
	benchmarkOnly := []string{
		"output-raw", "skip-prepare", "fixture-seed", "rate", "duration", "warmup", "concurrency", "repetitions",
		"telemetry-command-json", "telemetry-output", "telemetry-timeout",
		"trace-graph-json", "trace-graph-text", "trace-max-roots", "trace-max-nodes", "trace-timeline-width",
		vseval.OutputFlag,
	}
	if config.mode == "benchmark" {
		for _, name := range accuracyOnly {
			if config.explicit[name] {
				return fmt.Errorf("--%s is only available in accuracy mode", name)
			}
		}
	} else {
		for _, name := range benchmarkOnly {
			if config.explicit[name] {
				return fmt.Errorf("--%s is only available in benchmark mode", name)
			}
		}
		if config.outputRaw != "" {
			return errors.New("--output-raw is only available in benchmark mode")
		}
		if config.casesMin < 1 || config.casesMax < config.casesMin {
			return fmt.Errorf(
				"accuracy case bounds must satisfy 1 <= min <= max, got %d..%d",
				config.casesMin,
				config.casesMax,
			)
		}
	}
	if (config.explicit["telemetry-timeout"] ||
		config.telemetryCommand != "" ||
		config.telemetryOutput != "") &&
		config.telemetryTimeout <= 0 {
		return fmt.Errorf("--telemetry-timeout must be positive")
	}
	if config.telemetryCommand == "" {
		if config.telemetryOutput != "" {
			return errors.New("--telemetry-output requires --telemetry-command-json")
		}
		if config.explicit["telemetry-timeout"] {
			return errors.New("--telemetry-timeout requires --telemetry-command-json")
		}
	}
	if config.telemetryCommand != "" {
		if config.telemetryOutput == "" {
			return errors.New("--telemetry-command-json requires --telemetry-output")
		}
		if _, err := parseCommandJSON(
			config.telemetryCommand,
			"--telemetry-command-json",
		); err != nil {
			return err
		}
	}
	if config.runCommandJSON != "" {
		if _, err := parseCommandJSON(config.runCommandJSON, "--run-command-json"); err != nil {
			return err
		}
		if config.stopCommandJSON != "" {
			if _, err := parseCommandJSON(config.stopCommandJSON, "--stop-command-json"); err != nil {
				return err
			}
		}
		if config.cleanupCommandJSON != "" {
			if _, err := parseCommandJSON(config.cleanupCommandJSON, "--cleanup-command-json"); err != nil {
				return err
			}
		}
		if config.mode == "accuracy" && config.stateEnv == "" {
			return errors.New("--state-env must not be empty")
		}
		return nil
	}
	if config.stateDir != "" {
		return errors.New("--state-dir requires --run-command-json")
	}
	if config.stopCommandJSON != "" {
		return errors.New("--stop-command-json requires --run-command-json")
	}
	if config.cleanupCommandJSON != "" {
		return errors.New("--cleanup-command-json requires --run-command-json")
	}
	for _, name := range []string{"candidate-dir", "state-env"} {
		if config.explicit[name] {
			return fmt.Errorf("--%s requires --run-command-json", name)
		}
	}
	return nil
}

func runAccuracy(
	ctx context.Context,
	registered *registry.Registry,
	workload api.Workload,
	engineVersion string,
	outputJSON string,
	casesMin int,
	casesMax int,
	startupTimeoutSeconds float64,
	runCommandJSON string,
	stopCommandJSON string,
	cleanupCommandJSON string,
	candidateDir string,
	stateDir string,
	stateEnv string,
) error {
	var candidate accuracy.CandidateLifecycle
	var temporaryState string
	if runCommandJSON != "" {
		command, err := parseCommandJSON(runCommandJSON, "--run-command-json")
		if err != nil {
			return err
		}
		var stopCommand []string
		if stopCommandJSON != "" {
			stopCommand, err = parseCommandJSON(stopCommandJSON, "--stop-command-json")
			if err != nil {
				return err
			}
		}
		var cleanupCommand []string
		if cleanupCommandJSON != "" {
			cleanupCommand, err = parseCommandJSON(cleanupCommandJSON, "--cleanup-command-json")
			if err != nil {
				return err
			}
		}
		if stateDir == "" {
			temporaryState, err = os.MkdirTemp("", "microservice-state-*")
			if err != nil {
				return fmt.Errorf("create temporary candidate state directory: %w", err)
			}
			defer os.RemoveAll(temporaryState)
			stateDir = temporaryState
		}
		resolvedState, err := filepath.Abs(stateDir)
		if err != nil {
			return fmt.Errorf("resolve candidate state directory: %w", err)
		}
		if err := os.MkdirAll(resolvedState, 0o755); err != nil {
			return fmt.Errorf("create candidate state directory: %w", err)
		}
		stateDir = resolvedState
		if stateEnv == "" {
			return errors.New("--state-env must not be empty")
		}
		managed, err := lifecycle.NewManagedCandidate(
			command,
			stopCommand,
			cleanupCommand,
			candidateDir,
			map[string]string{stateEnv: stateDir},
		)
		if err != nil {
			return err
		}
		candidate = managed
	} else if stateDir != "" {
		return errors.New("--state-dir requires --run-command-json")
	}
	runner, err := accuracy.NewRunner(registered, accuracy.Options{
		EngineVersion:  engineVersion,
		Seed:           workload.Load.Seed,
		CasesMin:       casesMin,
		CasesMax:       casesMax,
		StartupTimeout: time.Duration(startupTimeoutSeconds * float64(time.Second)),
		ProbeTimeout:   time.Duration(workload.Load.TimeoutSeconds * float64(time.Second)),
		ProbeInterval:  100 * time.Millisecond,
		Lifecycle:      candidate,
	})
	if err != nil {
		return err
	}
	result := runner.Run(ctx, workload)
	encoded, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		return fmt.Errorf("encode accuracy result: %w", err)
	}
	if outputJSON != "" {
		if err := fsutil.WriteFileAtomic(outputJSON, append(encoded, '\n'), 0o600); err != nil {
			return err
		}
	}
	fmt.Println(string(encoded))
	if !result.Valid {
		return errors.New("accuracy result is invalid")
	}
	return nil
}

func parseCommandJSON(raw, flagName string) ([]string, error) {
	var command []string
	if err := json.Unmarshal([]byte(raw), &command); err != nil {
		return nil, fmt.Errorf("%s must be a JSON string array: %w", flagName, err)
	}
	if len(command) == 0 {
		return nil, fmt.Errorf("%s must be a non-empty JSON string array", flagName)
	}
	for index, argument := range command {
		if argument == "" {
			return nil, fmt.Errorf("%s argument %d is empty", flagName, index)
		}
	}
	return command, nil
}

func parseSeed(value, flagName string) (int64, error) {
	if value == "random" {
		var raw [8]byte
		if _, err := cryptorand.Read(raw[:]); err != nil {
			return 0, fmt.Errorf("generate random %s: %w", flagName, err)
		}
		return int64(binary.LittleEndian.Uint64(raw[:]) & uint64(^uint64(0)>>1)), nil
	}
	parsed, err := strconv.ParseInt(value, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("invalid %s: %w", flagName, err)
	}
	return parsed, nil
}

func writeNDJSON(path string, observations []api.Observation) error {
	file, err := os.Create(path)
	if err != nil {
		return fmt.Errorf("create raw output %s: %w", path, err)
	}
	writer := bufio.NewWriter(file)
	encoder := json.NewEncoder(writer)
	for _, observation := range observations {
		if err := encoder.Encode(observation); err != nil {
			file.Close()
			return fmt.Errorf("write raw output %s: %w", path, err)
		}
	}
	if err := writer.Flush(); err != nil {
		file.Close()
		return fmt.Errorf("flush raw output %s: %w", path, err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("close raw output %s: %w", path, err)
	}
	return nil
}
