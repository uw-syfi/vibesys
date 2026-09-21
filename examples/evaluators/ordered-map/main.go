package main

import (
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

func candidateFlags(flags *flag.FlagSet) (*string, *string, *bool) {
	workspaceFlag := flags.String("workspace", ".", "Candidate workspace")
	candidate := flags.String(
		"candidate",
		"ordered-map-candidate.so",
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
	maxKeySize uint64,
	maxValueSize uint64,
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
	if maxKeySize == 0 || maxKeySize > maxMapSize {
		return candidateConfig{}, fmt.Errorf("max key size must be in [1, %d] bytes", maxMapSize)
	}
	if maxValueSize == 0 || maxValueSize > maxMapSize {
		return candidateConfig{}, fmt.Errorf("max value size must be in [1, %d] bytes", maxMapSize)
	}
	return candidateConfig{
		workspace:    absWorkspace,
		candidate:    candidate,
		useReference: useReference,
		scenario:     s,
		maxKeySize:   int(maxKeySize),
		maxValueSize: int(maxValueSize),
	}, nil
}

func runCheckCommand(args []string) error {
	flags := flag.NewFlagSet("check", flag.ContinueOnError)
	workspace, candidate, useReference := candidateFlags(flags)
	scenarioName := flags.String("scenario", "swmr", "Map scenario: swmr, mw, or all")
	maxKeySize := flags.Uint64("max-key-size", 8, "Maximum copied key size in bytes")
	maxValueSize := flags.Uint64("max-value-size", 8, "Maximum copied value size in bytes")
	operations := flags.Int("operations", 24, "Approximate operations per concurrent trial")
	trials := flags.Int("trials", 20, "Independent concurrent histories")
	clients := flags.Int("clients", 4, "Client count for the selected scenario")
	seed := flags.Int64("seed", 42, "Deterministic workload seed")
	checkBudget := flags.Duration(
		"check-budget",
		defaultCheckBudget,
		"Time budget for deciding one history; an undecided history fails the gate",
	)
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
			*maxKeySize,
			*maxValueSize,
		)
		if err != nil {
			return err
		}
		config := accuracyConfig{
			candidateConfig: base,
			operations:      *operations,
			trials:          *trials,
			clients:         *clients,
			seed:            *seed,
			checkBudget:     *checkBudget,
		}
		if err := runAccuracy(config); err != nil {
			return fmt.Errorf("%s: %w", selected, err)
		}
		actualClients, _ := clientCount(selected, *clients)
		fmt.Printf(
			"PASS - %s %s (%d trials, approximately %d ops/trial, %d clients, max_key_size=%d, max_value_size=%d)\n",
			selected,
			correctnessContract(),
			*trials,
			*operations,
			actualClients,
			*maxKeySize,
			*maxValueSize,
		)
	}
	return nil
}

func runBenchmarkCommand(args []string) error {
	flags := flag.NewFlagSet("benchmark", flag.ContinueOnError)
	workspace, candidate, useReference := candidateFlags(flags)
	scenarioName := flags.String("scenario", "swmr", "Map scenario: swmr, mw, or all")
	maxKeySize := flags.Uint64("max-key-size", 8, "Maximum copied key size in bytes")
	maxValueSize := flags.Uint64("max-value-size", 8, "Maximum copied value size in bytes")
	clients := flags.Int("clients", 4, "Client count for the selected scenario")
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
			*maxKeySize,
			*maxValueSize,
		)
		if err != nil {
			return err
		}
		result, err := runBenchmark(benchmarkConfig{
			candidateConfig: base,
			clients:         *clients,
			duration:        *duration,
			warmup:          *warmup,
			repetitions:     *repetitions,
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
		"Scenario: %s  Duration: %.3fs  Clients: %d  Writers: %d  Readers: %d\n",
		result.Scenario,
		result.Duration,
		result.Clients,
		result.Writers,
		result.Readers,
	)
	fmt.Printf(
		"  Puts: %d  Gets: %d  Removes: %d  Successors: %d  Ranges: %d  Missing: %d\n",
		result.Puts,
		result.Gets,
		result.Removes,
		result.Successors,
		result.Ranges,
		result.Missing,
	)
	fmt.Printf(
		"  Completed: %d  Attempts: %d (%.0f ops/s)\n",
		result.Puts+result.Gets+result.Removes+result.Successors+result.Ranges+result.Missing,
		result.Attempts,
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
