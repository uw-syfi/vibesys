package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"sort"
	"strconv"
	"time"
)

var nativeBenchmarkShutdownGrace = 15 * time.Second

type benchmarkConfig struct {
	candidateConfig
	duration    time.Duration
	warmup      time.Duration
	repetitions int
	keySpace    uint64
	seed        int64
	checkBudget time.Duration
}

type benchmarkResult struct {
	Scenario              string    `json:"scenario"`
	Duration              float64   `json:"duration"`
	TotalOpsPerSec        float64   `json:"total_ops_per_sec"`
	Clients               int       `json:"clients"`
	Puts                  uint64    `json:"puts"`
	Gets                  uint64    `json:"gets"`
	Removes               uint64    `json:"removes"`
	Hits                  uint64    `json:"hits"`
	Misses                uint64    `json:"misses"`
	Repetitions           int       `json:"repetitions,omitempty"`
	TotalOpsPerSecSamples []float64 `json:"total_ops_per_sec_samples,omitempty"`
}

func runNativeBenchmark(config benchmarkConfig) (benchmarkResult, error) {
	clients, err := clientCountForScenario(config.scenario, config.clientCount)
	if err != nil {
		return benchmarkResult{}, err
	}
	runner, err := nativeRunnerPath()
	if err != nil {
		return benchmarkResult{}, err
	}
	sourceArgs, err := candidateSourceArgs(config.candidateConfig)
	if err != nil {
		return benchmarkResult{}, err
	}
	output, err := os.CreateTemp("", "vibesys-unordered-map-benchmark-*.json")
	if err != nil {
		return benchmarkResult{}, fmt.Errorf("create native benchmark result file: %w", err)
	}
	outputPath := output.Name()
	if err := output.Close(); err != nil {
		_ = os.Remove(outputPath)
		return benchmarkResult{}, fmt.Errorf("close native benchmark result file: %w", err)
	}
	defer os.Remove(outputPath)

	args := append([]string{"benchmark"}, sourceArgs...)
	args = append(args,
		"--scenario", config.scenario.String(),
		"--key-size", strconv.Itoa(config.keySize),
		"--value-size", strconv.Itoa(config.valueSize),
		"--clients", strconv.Itoa(clients),
		"--key-space", strconv.FormatUint(config.keySpace, 10),
		"--warmup-ns", strconv.FormatInt(config.warmup.Nanoseconds(), 10),
		"--duration-ns", strconv.FormatInt(config.duration.Nanoseconds(), 10),
		"--output", outputPath,
	)
	timeout := config.warmup + config.duration + nativeBenchmarkShutdownGrace
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	command := exec.CommandContext(ctx, runner, args...)
	command.Dir = config.workspace
	log := newBoundedLog(64 * 1024)
	command.Stdout = io.Writer(log)
	command.Stderr = io.Writer(log)
	if err := command.Run(); err != nil {
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return benchmarkResult{}, fmt.Errorf(
				"native benchmark timed out after %s; candidate operation or shutdown did not complete",
				timeout,
			)
		}
		return benchmarkResult{}, fmt.Errorf(
			"native benchmark failed: %w\nnative runner output:\n%s",
			err,
			log.String(),
		)
	}

	data, err := os.Open(outputPath)
	if err != nil {
		return benchmarkResult{}, fmt.Errorf("open native benchmark result: %w", err)
	}
	defer data.Close()
	decoder := json.NewDecoder(data)
	decoder.DisallowUnknownFields()
	var result benchmarkResult
	if err := decoder.Decode(&result); err != nil {
		return benchmarkResult{}, fmt.Errorf("decode native benchmark result: %w", err)
	}
	if result.Scenario != config.scenario.String() {
		return benchmarkResult{}, fmt.Errorf(
			"native benchmark reported scenario %q, expected %q",
			result.Scenario,
			config.scenario,
		)
	}
	if result.Clients != clients {
		return benchmarkResult{}, errors.New("native benchmark reported incorrect client count")
	}
	if result.Duration <= 0 || result.Hits+result.Misses != result.Gets+result.Removes {
		return benchmarkResult{}, errors.New("native benchmark reported inconsistent metrics")
	}
	if result.Puts+result.Gets+result.Removes == 0 {
		return benchmarkResult{}, errors.New("native benchmark reported no completed operations")
	}
	return result, nil
}

func runBenchmark(config benchmarkConfig) (benchmarkResult, error) {
	if config.duration <= 0 {
		return benchmarkResult{}, errors.New("duration must be greater than zero")
	}
	if config.warmup < 0 {
		return benchmarkResult{}, errors.New("warmup must not be negative")
	}
	if config.repetitions <= 0 || config.repetitions%2 == 0 {
		return benchmarkResult{}, errors.New("repetitions must be a positive odd number")
	}
	if config.keySpace == 0 {
		return benchmarkResult{}, errors.New("key space must be greater than zero")
	}
	if _, err := clientCountForScenario(config.scenario, config.clientCount); err != nil {
		return benchmarkResult{}, err
	}

	gate := accuracyConfig{
		candidateConfig: config.candidateConfig,
		operations:      24,
		trials:          1,
		seed:            config.seed,
		checkBudget:     config.checkBudget,
	}
	if err := runAccuracy(gate); err != nil {
		return benchmarkResult{}, fmt.Errorf("correctness gate: %w", err)
	}

	results := make([]benchmarkResult, 0, config.repetitions)
	for repetition := 0; repetition < config.repetitions; repetition++ {
		result, err := runNativeBenchmark(config)
		if err != nil {
			return benchmarkResult{}, fmt.Errorf(
				"benchmark repetition %d/%d: %w",
				repetition+1,
				config.repetitions,
				err,
			)
		}
		results = append(results, result)
	}
	return medianBenchmarkResult(results), nil
}

func medianBenchmarkResult(results []benchmarkResult) benchmarkResult {
	rates := make([]float64, len(results))
	for index, result := range results {
		rates[index] = result.TotalOpsPerSec
	}
	sortedRates := append([]float64(nil), rates...)
	sort.Float64s(sortedRates)
	medianRate := sortedRates[len(sortedRates)/2]

	median := results[0]
	for _, result := range results {
		if result.TotalOpsPerSec == medianRate {
			median = result
			break
		}
	}
	median.Repetitions = len(results)
	median.TotalOpsPerSecSamples = rates
	return median
}

func writeBenchmarkResults(path string, results []benchmarkResult) error {
	if path == "" {
		return nil
	}
	data, err := json.MarshalIndent(results, "", "  ")
	if err != nil {
		return fmt.Errorf("encode benchmark result: %w", err)
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return fmt.Errorf("write benchmark result %q: %w", path, err)
	}
	return nil
}
