package main

import (
	"bufio"
	"context"
	"crypto/sha256"
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

	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/apps/declarative"
	"vibesys/microservice-evaluator/apps/socialnetwork"
	"vibesys/microservice-evaluator/config"
	"vibesys/microservice-evaluator/drivers/httpdriver"
	"vibesys/microservice-evaluator/engine"
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
		fmt.Fprintln(os.Stderr, "microbench:", err)
		os.Exit(1)
	}
}

func run() error {
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

	flag.StringVar(&workloadPath, "workload", "", "path to the workload TOML file")
	flag.StringVar(&profile, "profile", "", "optional named workload profile")
	flag.StringVar(&outputJSON, "output-json", "", "optional structured result path")
	flag.StringVar(&outputRaw, "output-raw", "", "optional per-request NDJSON path")
	flag.StringVar(&baseURL, "base-url", "", "override every HTTP target address")
	flag.BoolVar(&skipPrepare, "skip-prepare", false, "skip application fixture preparation")
	flag.BoolVar(&validateOnly, "validate-only", false, "validate configuration and registered extensions without connecting")
	flag.Var(&overrides, "target", "override a target address as NAME=ADDRESS (repeatable)")
	flag.Float64Var(&rate, "rate", 0, "override target requests per second")
	flag.Float64Var(&duration, "duration", 0, "override measured duration in seconds")
	flag.Float64Var(&warmup, "warmup", -1, "override warmup duration in seconds")
	flag.IntVar(&concurrency, "concurrency", 0, "override maximum in-flight requests")
	flag.IntVar(&repetitions, "repetitions", 0, "override independent trial count")
	flag.StringVar(&seed, "seed", "", "override deterministic random seed")
	flag.Parse()
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
		parsed, parseErr := strconv.ParseInt(seed, 10, 64)
		if parseErr != nil {
			return fmt.Errorf("invalid --seed: %w", parseErr)
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
	if _, err := registry.Application(workload); err != nil {
		return err
	}
	for _, target := range workload.Targets {
		if _, err := registry.Driver(target.Protocol); err != nil {
			return fmt.Errorf("target %q: %w", target.Name, err)
		}
	}

	fmt.Fprintf(
		os.Stderr,
		"workload=%s application=%s model=%s rate=%.2f duration=%.2fs warmup=%.2fs concurrency=%d repetitions=%d seed=%d\n",
		workload.Name,
		workload.Application,
		workload.Load.Model,
		workload.Load.Rate,
		workload.Load.DurationSeconds,
		workload.Load.WarmupSeconds,
		workload.Load.Concurrency,
		workload.Load.Repetitions,
		workload.Load.Seed,
	)
	for _, target := range workload.Targets {
		fmt.Fprintf(os.Stderr, "target=%s protocol=%s address=%s session_policy=%s\n", target.Name, target.Protocol, target.Address, target.SessionPolicy)
	}
	if validateOnly {
		fmt.Println("workload is valid")
		return nil
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
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
	temporary, err := os.CreateTemp(directory, ".microbench-result-*")
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
