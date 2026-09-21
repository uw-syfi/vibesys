package main

import (
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

const (
	minMapPayloadSize = 8
	maxMapKeySize     = 1 << 20
	maxMapValueSize   = 1 << 20
)

func candidateFlags(flags *flag.FlagSet) (*string, *string, *bool) {
	workspaceFlag := flags.String("workspace", ".", "Candidate workspace")
	candidate := flags.String(
		"candidate",
		"unordered-map-candidate.so",
		"Candidate shared library relative to workspace",
	)
	useReference := flags.Bool("use-reference", false, "Use the bundled reference candidate")
	return workspaceFlag, candidate, useReference
}

func selectedScenarios(value string) ([]scenario, error) {
	if value == "all" {
		return []scenario{scenarioSWMR, scenarioMW}, nil
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
	keySize uint64,
	valueSize uint64,
	clients int,
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
	if keySize < minMapPayloadSize || keySize > maxMapKeySize {
		return candidateConfig{}, fmt.Errorf(
			"key size must be in [%d, %d] bytes",
			minMapPayloadSize,
			maxMapKeySize,
		)
	}
	if valueSize < minMapPayloadSize || valueSize > maxMapValueSize {
		return candidateConfig{}, fmt.Errorf(
			"value size must be in [%d, %d] bytes",
			minMapPayloadSize,
			maxMapValueSize,
		)
	}
	if _, err := clientCountForScenario(s, clients); err != nil {
		return candidateConfig{}, err
	}
	return candidateConfig{
		workspace:    absWorkspace,
		candidate:    candidate,
		useReference: useReference,
		scenario:     s,
		keySize:      int(keySize),
		valueSize:    int(valueSize),
		clientCount:  clients,
	}, nil
}

func runCheckCommand(args []string) error {
	flags := flag.NewFlagSet("check", flag.ContinueOnError)
	workspace, candidate, useReference := candidateFlags(flags)
	scenarioName := flags.String("scenario", "swmr", "Map scenario: swmr, mw, or all")
	keySize := flags.Uint64("key-size", 8, "Copied map key size in bytes")
	valueSize := flags.Uint64("value-size", 8, "Copied map value size in bytes")
	operations := flags.Int("operations", 24, "Approximate operations per concurrent trial")
	trials := flags.Int("trials", 20, "Independent concurrent histories")
	clients := flags.Int("clients", 4, "Client count used by ABI probes and concurrent histories")
	seed := flags.Int64("seed", 42, "Deterministic workload seed")
	checkBudget := flags.Duration(
		"check-budget",
		defaultCheckBudget,
		"Time budget for deciding one history; an undecided history fails the gate",
	)
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
			*keySize,
			*valueSize,
			*clients,
		)
		if err != nil {
			return err
		}
		config := accuracyConfig{
			candidateConfig: base,
			operations:      *operations,
			trials:          *trials,
			seed:            *seed,
			checkBudget:     *checkBudget,
			failureHistory:  failureHistoryForScenario(*failureHistory, selected, len(scenarios)),
		}
		if err := runAccuracy(config); err != nil {
			return fmt.Errorf("%s: %w", selected, err)
		}
		fmt.Printf(
			"PASS - %s %s (%d trials, approximately %d ops/trial, %d clients, key_size=%d, value_size=%d)\n",
			selected,
			correctnessContract(),
			*trials,
			*operations,
			*clients,
			*keySize,
			*valueSize,
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
	scenarioName := flags.String("scenario", "swmr", "Map scenario: swmr, mw, or all")
	keySize := flags.Uint64("key-size", 8, "Copied map key size in bytes")
	valueSize := flags.Uint64("value-size", 8, "Copied map value size in bytes")
	clients := flags.Int("clients", 4, "Client count for the selected scenario")
	keySpace := flags.Uint64("key-space", 256, "Distinct encoded keys in the benchmark")
	duration := flags.Duration("duration", 10*time.Second, "Measured benchmark duration")
	warmup := flags.Duration("warmup", 2*time.Second, "Warmup duration")
	repetitions := flags.Int(
		"repetitions",
		1,
		"Odd number of measured runs; total_ops_per_sec reports their median",
	)
	seed := flags.Int64("seed", 42, "Correctness-gate seed")
	checkBudget := flags.Duration(
		"check-budget",
		defaultCheckBudget,
		"Time budget for deciding one correctness-gate history",
	)
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
			*keySize,
			*valueSize,
			*clients,
		)
		if err != nil {
			return err
		}
		result, err := runBenchmark(benchmarkConfig{
			candidateConfig: base,
			duration:        *duration,
			warmup:          *warmup,
			repetitions:     *repetitions,
			keySpace:        *keySpace,
			seed:            *seed,
			checkBudget:     *checkBudget,
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
	if len(result.TotalOpsPerSecSamples) > 1 {
		fmt.Printf(
			"Scenario: %s  Repetitions: %d  Median ops/s: %.0f\n",
			result.Scenario,
			result.Repetitions,
			result.TotalOpsPerSec,
		)
		fmt.Printf("  Samples: %v\n", result.TotalOpsPerSecSamples)
	}
	fmt.Printf(
		"Scenario: %s  Duration: %.3fs  Clients: %d\n",
		result.Scenario,
		result.Duration,
		result.Clients,
	)
	fmt.Printf(
		"  Puts: %d  Gets: %d  Removes: %d  Hits: %d  Misses: %d\n",
		result.Puts,
		result.Gets,
		result.Removes,
		result.Hits,
		result.Misses,
	)
	fmt.Printf(
		"  Completed: %d (%.0f ops/s)\n",
		result.Puts+result.Gets+result.Removes,
		result.TotalOpsPerSec,
	)
}

func run(args []string) error {
	if len(args) == 0 {
		return errors.New("expected one of: check, benchmark")
	}
	switch args[0] {
	case "check":
		return runCheckCommand(args[1:])
	case "benchmark":
		return runBenchmarkCommand(args[1:])
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
