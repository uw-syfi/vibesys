package main

import (
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

const maxQueueCapacity = 1 << 16

func candidateFlags(flags *flag.FlagSet) (*string, *string, *bool) {
	workspace := defaultWorkspace()
	workspaceFlag := flags.String("workspace", workspace, "Candidate workspace")
	candidate := flags.String("candidate", "queue-candidate", "Candidate launcher relative to workspace")
	useReference := flags.Bool("use-reference", false, "Use the bundled reference candidate")
	return workspaceFlag, candidate, useReference
}

func defaultWorkspace() string {
	cwd, _ := os.Getwd()
	for current := filepath.Clean(cwd); ; current = filepath.Dir(current) {
		if filepath.Base(current) == "_input_libs" {
			return filepath.Dir(current)
		}
		parent := filepath.Dir(current)
		if parent == current {
			break
		}
	}
	if workspace := os.Getenv("PWD"); workspace != "" {
		return workspace
	}
	return cwd
}

func selectedScenarios(value string) ([]scenario, error) {
	if value == "all" {
		return []scenario{scenarioSPSC, scenarioMPSC, scenarioMPMC}, nil
	}
	selected, err := parseScenario(value)
	if err != nil {
		return nil, err
	}
	return []scenario{selected}, nil
}

func parseCandidateConfig(
	workspace string,
	candidate string,
	useReference bool,
	scenarioName string,
	capacity uint64,
) (candidateConfig, error) {
	s, err := parseScenario(scenarioName)
	if err != nil {
		return candidateConfig{}, err
	}
	absWorkspace, err := filepath.Abs(workspace)
	if err != nil {
		return candidateConfig{}, fmt.Errorf("resolve workspace: %w", err)
	}
	stat, err := os.Stat(absWorkspace)
	if err != nil {
		return candidateConfig{}, fmt.Errorf("workspace %q: %w", absWorkspace, err)
	}
	if !stat.IsDir() {
		return candidateConfig{}, fmt.Errorf("workspace %q is not a directory", absWorkspace)
	}
	if capacity == 0 {
		return candidateConfig{}, errors.New("capacity must be greater than zero")
	}
	if capacity > maxQueueCapacity {
		return candidateConfig{}, fmt.Errorf(
			"capacity must not exceed %d because the correctness gate fills the queue",
			maxQueueCapacity,
		)
	}
	return candidateConfig{
		workspace:    absWorkspace,
		candidate:    candidate,
		useReference: useReference,
		scenario:     s,
		capacity:     capacity,
	}, nil
}

func runCheckCommand(args []string) error {
	flags := flag.NewFlagSet("check", flag.ContinueOnError)
	workspace, candidate, useReference := candidateFlags(flags)
	scenarioName := flags.String("scenario", "spsc", "Queue scenario: spsc, mpsc, mpmc, or all")
	capacity := flags.Uint64("capacity", 1024, "Bounded queue capacity")
	operations := flags.Int("operations", 24, "Approximate operations per concurrent trial")
	trials := flags.Int("trials", 20, "Independent concurrent histories")
	producers := flags.Int("producers", 4, "Producer count for configurable scenarios")
	consumers := flags.Int("consumers", 4, "Consumer count for MPMC")
	seed := flags.Int64("seed", 42, "Deterministic workload seed")
	failureHistory := flags.String("failure-history", "", "Write the first rejected history as JSON")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if flags.NArg() != 0 {
		return fmt.Errorf("unexpected positional arguments: %v", flags.Args())
	}
	scenarios, err := selectedScenarios(*scenarioName)
	if err != nil {
		return err
	}
	for _, selected := range scenarios {
		base, err := parseCandidateConfig(
			*workspace,
			*candidate,
			*useReference,
			selected.String(),
			*capacity,
		)
		if err != nil {
			return err
		}
		config := accuracyConfig{
			candidateConfig: base,
			operations:      *operations,
			trials:          *trials,
			producers:       *producers,
			consumers:       *consumers,
			seed:            *seed,
			failureHistory:  failureHistoryForScenario(*failureHistory, selected, len(scenarios)),
		}
		if err := runAccuracy(config); err != nil {
			return fmt.Errorf("%s: %w", selected, err)
		}
		actualProducers, actualConsumers, _ := workerCounts(selected, *producers, *consumers)
		fmt.Printf(
			"PASS - %s linearizable (%d trials, approximately %d ops/trial, %dP/%dC, capacity=%d)\n",
			selected,
			*trials,
			*operations,
			actualProducers,
			actualConsumers,
			*capacity,
		)
	}
	return nil
}

func failureHistoryForScenario(path string, selected scenario, scenarioCount int) string {
	if path == "" || scenarioCount == 1 {
		return path
	}
	extension := filepath.Ext(path)
	base := path[:len(path)-len(extension)]
	return fmt.Sprintf("%s-%s%s", base, selected, extension)
}

func runBenchmarkCommand(args []string) error {
	flags := flag.NewFlagSet("benchmark", flag.ContinueOnError)
	workspace, candidate, useReference := candidateFlags(flags)
	scenarioName := flags.String("scenario", "spsc", "Queue scenario: spsc, mpsc, mpmc, or all")
	capacity := flags.Uint64("capacity", 1024, "Bounded queue capacity")
	producers := flags.Int("producers", 4, "Producer count for configurable scenarios")
	consumers := flags.Int("consumers", 4, "Consumer count for MPMC")
	duration := flags.Duration("duration", 10*time.Second, "Measured benchmark duration")
	warmup := flags.Duration("warmup", 2*time.Second, "Warmup duration")
	seed := flags.Int64("seed", 42, "Correctness-gate seed")
	output := flags.String("output-json", "", "Write trusted benchmark metrics as JSON")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if flags.NArg() != 0 {
		return fmt.Errorf("unexpected positional arguments: %v", flags.Args())
	}
	scenarios, err := selectedScenarios(*scenarioName)
	if err != nil {
		return err
	}
	results := make([]benchmarkResult, 0, len(scenarios))
	for _, selected := range scenarios {
		base, err := parseCandidateConfig(
			*workspace,
			*candidate,
			*useReference,
			selected.String(),
			*capacity,
		)
		if err != nil {
			return err
		}
		result, err := runBenchmark(benchmarkConfig{
			candidateConfig: base,
			producers:       *producers,
			consumers:       *consumers,
			duration:        *duration,
			warmup:          *warmup,
			seed:            *seed,
		})
		if err != nil {
			return fmt.Errorf("%s: %w", selected, err)
		}
		printBenchmarkResult(result)
		results = append(results, result)
	}
	return writeBenchmarkResults(*output, results)
}

func printBenchmarkResult(result benchmarkResult) {
	fmt.Printf(
		"Scenario: %s  Duration: %.3fs  Prod: %d  Cons: %d\n",
		result.Scenario,
		result.Duration,
		result.Producers,
		result.Consumers,
	)
	fmt.Printf(
		"  Enqueued: %d  Full: %d  Dequeued: %d  Empty: %d\n",
		result.Enqueued,
		result.Dropped,
		result.Dequeued,
		result.Empty,
	)
	fmt.Printf(
		"  Successful: %d  Attempts: %d (%.0f successful ops/s)\n",
		result.Enqueued+result.Dequeued,
		result.Attempts,
		result.TotalOpsPerSec,
	)
}

func runReferenceCommand(args []string) error {
	flags := flag.NewFlagSet("serve-reference", flag.ContinueOnError)
	sharedMemory := flags.String("shared-memory", "", "Shared-memory protocol file")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *sharedMemory == "" {
		return errors.New("--shared-memory is required")
	}
	return serveReference(*sharedMemory)
}

func run(args []string) error {
	if len(args) == 0 {
		return errors.New("expected one of: check, benchmark, serve-reference")
	}
	switch args[0] {
	case "check":
		return runCheckCommand(args[1:])
	case "benchmark":
		return runBenchmarkCommand(args[1:])
	case "serve-reference":
		return runReferenceCommand(args[1:])
	default:
		return fmt.Errorf("unknown command %q", args[0])
	}
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "FAIL - %v\n", err)
		os.Exit(1)
	}
}
