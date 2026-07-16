package main

import (
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

type benchmarkConfig struct {
	candidateConfig
	producers   int
	consumers   int
	duration    time.Duration
	warmup      time.Duration
	repetitions int
	seed        int64
}

type benchmarkResult struct {
	Scenario              string    `json:"scenario"`
	Enqueued              uint64    `json:"enqueued"`
	Dropped               uint64    `json:"dropped"`
	Dequeued              uint64    `json:"dequeued"`
	Empty                 uint64    `json:"empty"`
	Attempts              uint64    `json:"attempts"`
	Duration              float64   `json:"duration"`
	TotalOpsPerSec        float64   `json:"total_ops_per_sec"`
	Producers             int       `json:"producers"`
	Consumers             int       `json:"consumers"`
	Repetitions           int       `json:"repetitions,omitempty"`
	TotalOpsPerSecSamples []float64 `json:"total_ops_per_sec_samples,omitempty"`
}

func runNativeBenchmark(config benchmarkConfig) (benchmarkResult, error) {
	producers, consumers, err := workerCounts(config.scenario, config.producers, config.consumers)
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
	output, err := os.CreateTemp("", "vibesys-queue-benchmark-*.json")
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
		"--capacity", strconv.FormatUint(config.capacity, 10),
		"--value-size", strconv.Itoa(config.valueSize),
		"--producers", strconv.Itoa(producers),
		"--consumers", strconv.Itoa(consumers),
		"--warmup-ns", strconv.FormatInt(config.warmup.Nanoseconds(), 10),
		"--duration-ns", strconv.FormatInt(config.duration.Nanoseconds(), 10),
		"--output", outputPath,
	)
	command := exec.Command(runner, args...)
	command.Dir = config.workspace
	log := newBoundedLog(64 * 1024)
	command.Stdout = io.Writer(log)
	command.Stderr = io.Writer(log)
	if err := command.Run(); err != nil {
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
	if result.Producers != producers || result.Consumers != consumers {
		return benchmarkResult{}, errors.New("native benchmark reported incorrect worker counts")
	}
	if result.Duration <= 0 || result.Attempts != result.Enqueued+result.Dropped+result.Dequeued+result.Empty {
		return benchmarkResult{}, errors.New("native benchmark reported inconsistent metrics")
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
	if _, _, err := workerCounts(config.scenario, config.producers, config.consumers); err != nil {
		return benchmarkResult{}, err
	}

	gate := accuracyConfig{
		candidateConfig: config.candidateConfig,
		operations:      24,
		trials:          1,
		producers:       config.producers,
		consumers:       config.consumers,
		seed:            config.seed,
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
