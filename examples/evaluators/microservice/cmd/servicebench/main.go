package main

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

	"vibesys/microservice-evaluator/accuracy"
	accuracytrainticket "vibesys/microservice-evaluator/accuracyapps/trainticket"
	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/apps/declarative"
	"vibesys/microservice-evaluator/apps/socialnetwork"
	benchmarktrainticket "vibesys/microservice-evaluator/apps/trainticket"
	"vibesys/microservice-evaluator/config"
	"vibesys/microservice-evaluator/drivers/httpdriver"
	"vibesys/microservice-evaluator/engine"
	"vibesys/microservice-evaluator/lifecycle"
	"vibesys/microservice-evaluator/registry"
)

var version = "dev"

type targetOverrides map[string]string

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

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "servicebench:", err)
		os.Exit(1)
	}
}

func run() error {
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
	var casesMin int
	var casesMax int
	var startupTimeout float64
	var runCommandJSON string
	var candidateDir string
	var stateDir string
	var stateEnv string

	flag.StringVar(&mode, "mode", "benchmark", "execution mode: benchmark or accuracy")
	flag.StringVar(&workloadPath, "workload", "", "path to the workload TOML file")
	flag.StringVar(&profile, "profile", "", "optional named workload profile")
	flag.StringVar(&outputJSON, "output-json", "", "optional structured result path")
	flag.StringVar(&outputRaw, "output-raw", "", "optional per-operation NDJSON path")
	flag.StringVar(&baseURL, "base-url", "", "override every HTTP target address")
	flag.BoolVar(&skipPrepare, "skip-prepare", false, "skip application fixture preparation")
	flag.BoolVar(&validateOnly, "validate-only", false, "validate configuration and registered extensions without connecting")
	flag.Var(&overrides, "target", "override a target address as NAME=ADDRESS (repeatable)")
	flag.Float64Var(&rate, "rate", 0, "override open-loop target logical operations per second")
	flag.Float64Var(&duration, "duration", 0, "override measured duration in seconds")
	flag.Float64Var(&warmup, "warmup", -1, "override warmup duration in seconds")
	flag.IntVar(&concurrency, "concurrency", 0, "override maximum in-flight logical operations")
	flag.IntVar(&repetitions, "repetitions", 0, "override independent trial count")
	flag.StringVar(&seed, "seed", "", "override deterministic random seed, or use 'random'")
	flag.IntVar(&casesMin, "cases-min", 2, "minimum randomized cases in accuracy mode")
	flag.IntVar(&casesMax, "cases-max", 5, "maximum randomized cases in accuracy mode")
	flag.Float64Var(&startupTimeout, "startup-timeout", 15, "candidate readiness timeout in seconds for accuracy mode")
	flag.StringVar(&runCommandJSON, "run-command-json", "", "managed candidate command as a JSON string array in accuracy mode")
	flag.StringVar(&candidateDir, "candidate-dir", ".", "managed candidate working directory in accuracy mode")
	flag.StringVar(&stateDir, "state-dir", "", "managed candidate persistent state directory in accuracy mode")
	flag.StringVar(&stateEnv, "state-env", "VIBESYS_STATE_DIR", "environment variable receiving --state-dir in accuracy mode")
	flag.Parse()
	if mode != "benchmark" && mode != "accuracy" {
		return fmt.Errorf("--mode must be benchmark or accuracy, got %q", mode)
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
		var parsed int64
		if seed == "random" {
			var raw [8]byte
			if _, randomErr := cryptorand.Read(raw[:]); randomErr != nil {
				return fmt.Errorf("generate random --seed: %w", randomErr)
			}
			parsed = int64(binary.LittleEndian.Uint64(raw[:]) & uint64(^uint64(0)>>1))
		} else {
			var parseErr error
			parsed, parseErr = strconv.ParseInt(seed, 10, 64)
			if parseErr != nil {
				return fmt.Errorf("invalid --seed: %w", parseErr)
			}
		}
		workload.Load.Seed = parsed
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
	canonical, err := config.CanonicalJSON(workload)
	if err != nil {
		return fmt.Errorf("serialize resolved workload: %w", err)
	}
	hash := sha256.Sum256(canonical)

	registry := registry.New()
	if err := registry.RegisterDriver(httpdriver.New()); err != nil {
		return err
	}
	if err := registry.RegisterApplication("declarative", declarative.New); err != nil {
		return err
	}
	if err := registry.RegisterApplication("social-network", socialnetwork.New); err != nil {
		return err
	}
	if err := registry.RegisterApplication("train-ticket", benchmarktrainticket.New); err != nil {
		return err
	}
	if err := registry.RegisterAccuracyApplication("train-ticket", accuracytrainticket.New); err != nil {
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
	if mode == "accuracy" {
		seedDigest := sha256.Sum256([]byte(seedDisplay))
		seedDisplay = fmt.Sprintf("sha256:%x", seedDigest[:8])
	}
	fmt.Fprintf(
		os.Stderr,
		"workload=%s application=%s model=%s rate=%.2f duration=%.2fs warmup=%.2fs concurrency=%d repetitions=%d seed=%s\n",
		workload.Name,
		workload.Application,
		workload.Load.Model,
		workload.Load.Rate,
		workload.Load.DurationSeconds,
		workload.Load.WarmupSeconds,
		workload.Load.Concurrency,
		workload.Load.Repetitions,
		seedDisplay,
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
		if outputRaw != "" {
			return errors.New("--output-raw is only available in benchmark mode")
		}
		return runAccuracy(
			ctx,
			registry,
			workload,
			outputJSON,
			casesMin,
			casesMax,
			startupTimeout,
			runCommandJSON,
			candidateDir,
			stateDir,
			stateEnv,
		)
	}
	runner := engine.New(registry, engine.Options{
		EngineVersion: version,
		WorkloadHash:  hex.EncodeToString(hash[:]),
		SkipPrepare:   skipPrepare,
	})
	runResult, err := runner.Run(ctx, workload)
	if err != nil {
		return err
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
		if err := writeAtomic(outputJSON, append(encoded, '\n')); err != nil {
			return err
		}
	}
	fmt.Println(string(encoded))
	if !runResult.Summary.Valid {
		return errors.New("benchmark result is invalid; inspect constraints and trial invalid_reasons")
	}
	return nil
}

func runAccuracy(
	ctx context.Context,
	registered *registry.Registry,
	workload api.Workload,
	outputJSON string,
	casesMin int,
	casesMax int,
	startupTimeoutSeconds float64,
	runCommandJSON string,
	candidateDir string,
	stateDir string,
	stateEnv string,
) error {
	var candidate accuracy.CandidateLifecycle
	var temporaryState string
	if runCommandJSON != "" {
		command, err := parseCommandJSON(runCommandJSON)
		if err != nil {
			return err
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
		EngineVersion:  version,
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
		if err := writeAtomic(outputJSON, append(encoded, '\n')); err != nil {
			return err
		}
	}
	fmt.Println(string(encoded))
	if !result.Valid {
		return errors.New("accuracy result is invalid")
	}
	return nil
}

func parseCommandJSON(raw string) ([]string, error) {
	var command []string
	if err := json.Unmarshal([]byte(raw), &command); err != nil {
		return nil, fmt.Errorf("--run-command-json must be a JSON string array: %w", err)
	}
	if len(command) == 0 {
		return nil, errors.New("--run-command-json must be a non-empty JSON string array")
	}
	for index, argument := range command {
		if argument == "" {
			return nil, fmt.Errorf("--run-command-json argument %d is empty", index)
		}
	}
	return command, nil
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

func writeAtomic(path string, data []byte) error {
	directory := filepath.Dir(path)
	temporary, err := os.CreateTemp(directory, ".servicebench-result-*")
	if err != nil {
		return fmt.Errorf("create temporary result in %s: %w", directory, err)
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if _, err := temporary.Write(data); err != nil {
		temporary.Close()
		return fmt.Errorf("write temporary result: %w", err)
	}
	if err := temporary.Close(); err != nil {
		return fmt.Errorf("close temporary result: %w", err)
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return fmt.Errorf("replace result %s: %w", path, err)
	}
	return nil
}
