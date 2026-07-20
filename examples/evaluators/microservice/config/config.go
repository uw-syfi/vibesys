package config

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/BurntSushi/toml"

	"vibesys/microservice-evaluator/api"
)

func Load(path string, profile string) (api.Workload, error) {
	var workload api.Workload
	metadata, err := toml.DecodeFile(path, &workload)
	if err != nil {
		return api.Workload{}, fmt.Errorf("parse workload %s: %w", path, err)
	}
	if undecoded := metadata.Undecoded(); len(undecoded) > 0 {
		keys := make([]string, 0, len(undecoded))
		for _, key := range undecoded {
			keys = append(keys, key.String())
		}
		sort.Strings(keys)
		return api.Workload{}, fmt.Errorf("workload contains unknown fields: %s", strings.Join(keys, ", "))
	}
	applyDefaults(&workload)
	if profile != "" {
		override, ok := workload.Profiles[profile]
		if !ok {
			return api.Workload{}, fmt.Errorf("unknown workload profile %q", profile)
		}
		override.Apply(&workload.Load)
		if len(override.ApplicationConfig) > 0 {
			if workload.ApplicationConfig == nil {
				workload.ApplicationConfig = make(map[string]any, len(override.ApplicationConfig))
			}
			for key, value := range override.ApplicationConfig {
				workload.ApplicationConfig[key] = value
			}
		}
	}
	if err := Validate(workload); err != nil {
		return api.Workload{}, fmt.Errorf("invalid workload: %w", err)
	}
	return workload, nil
}

func applyDefaults(workload *api.Workload) {
	if workload.Load.Model == "" {
		workload.Load.Model = "open_loop"
	}
	if workload.Load.Concurrency == 0 {
		workload.Load.Concurrency = 32
	}
	if workload.Load.TimeoutSeconds == 0 {
		workload.Load.TimeoutSeconds = 10
	}
	if workload.Load.Repetitions == 0 {
		workload.Load.Repetitions = 1
	}
	if workload.Load.MinOfferedRateRatio == 0 {
		workload.Load.MinOfferedRateRatio = 0.95
	}
	for index := range workload.Targets {
		if workload.Targets[index].SessionPolicy == "" {
			workload.Targets[index].SessionPolicy = "reuse"
		}
	}
	for index := range workload.Operations {
		if len(workload.Operations[index].Expect.Statuses) == 0 {
			workload.Operations[index].Expect.Statuses = []int{200}
		}
	}
}

func Validate(workload api.Workload) error {
	if workload.Version != api.WorkloadVersion {
		return fmt.Errorf("version must be %d, got %d", api.WorkloadVersion, workload.Version)
	}
	if strings.TrimSpace(workload.Name) == "" {
		return fmt.Errorf("name must not be empty")
	}
	if strings.TrimSpace(workload.Application) == "" {
		return fmt.Errorf("application must not be empty")
	}
	if workload.Load.Model != "open_loop" {
		return fmt.Errorf("load.model must be open_loop, got %q", workload.Load.Model)
	}
	if workload.Load.Rate <= 0 {
		return fmt.Errorf("load.rate must be greater than zero")
	}
	if workload.Load.DurationSeconds <= 0 {
		return fmt.Errorf("load.duration_seconds must be greater than zero")
	}
	if workload.Load.WarmupSeconds < 0 {
		return fmt.Errorf("load.warmup_seconds must not be negative")
	}
	if workload.Load.Concurrency <= 0 {
		return fmt.Errorf("load.concurrency must be greater than zero")
	}
	if workload.Load.TimeoutSeconds <= 0 {
		return fmt.Errorf("load.timeout_seconds must be greater than zero")
	}
	if workload.Load.Repetitions <= 0 {
		return fmt.Errorf("load.repetitions must be greater than zero")
	}
	if workload.Load.MinOfferedRateRatio <= 0 || workload.Load.MinOfferedRateRatio > 1 {
		return fmt.Errorf("load.min_offered_rate_ratio must be in (0, 1]")
	}

	targets := make(map[string]struct{}, len(workload.Targets))
	for index, target := range workload.Targets {
		if target.Name == "" {
			return fmt.Errorf("targets[%d].name must not be empty", index)
		}
		if _, exists := targets[target.Name]; exists {
			return fmt.Errorf("target name %q is duplicated", target.Name)
		}
		if target.Protocol == "" {
			return fmt.Errorf("target %q protocol must not be empty", target.Name)
		}
		if target.Address == "" {
			return fmt.Errorf("target %q address must not be empty", target.Name)
		}
		targets[target.Name] = struct{}{}
	}
	if len(targets) == 0 {
		return fmt.Errorf("at least one target is required")
	}

	operations := make(map[string]struct{}, len(workload.Operations))
	for index, operation := range workload.Operations {
		if operation.Name == "" {
			return fmt.Errorf("operations[%d].name must not be empty", index)
		}
		if _, exists := operations[operation.Name]; exists {
			return fmt.Errorf("operation name %q is duplicated", operation.Name)
		}
		if _, exists := targets[operation.Target]; !exists {
			return fmt.Errorf("operation %q references unknown target %q", operation.Name, operation.Target)
		}
		if operation.Weight <= 0 {
			return fmt.Errorf("operation %q weight must be greater than zero", operation.Name)
		}
		for captureIndex, capture := range operation.CaptureHeaders {
			if capture.Name == "" || capture.Header == "" {
				return fmt.Errorf("operation %q capture_headers[%d] requires name and header", operation.Name, captureIndex)
			}
			if capture.Unit != "" && capture.Unit != "ms" {
				return fmt.Errorf("operation %q capture %q has unsupported unit %q", operation.Name, capture.Name, capture.Unit)
			}
		}
		operations[operation.Name] = struct{}{}
	}
	if len(operations) == 0 {
		return fmt.Errorf("at least one operation is required")
	}

	if workload.Objective.Name == "" {
		return fmt.Errorf("objective.name must not be empty")
	}
	if workload.Objective.Metric != "latency_ms.p50" && workload.Objective.Metric != "requests_per_second" {
		return fmt.Errorf("objective.metric must be latency_ms.p50 or requests_per_second")
	}
	if workload.Objective.Direction != "minimize" && workload.Objective.Direction != "maximize" {
		return fmt.Errorf("objective.direction must be minimize or maximize")
	}
	if workload.Constraints.MinSuccessRate != nil {
		if *workload.Constraints.MinSuccessRate < 0 || *workload.Constraints.MinSuccessRate > 1 {
			return fmt.Errorf("constraints.min_success_rate must be in [0, 1]")
		}
	}
	if workload.Constraints.MaxErrorRate != nil {
		if *workload.Constraints.MaxErrorRate < 0 || *workload.Constraints.MaxErrorRate > 1 {
			return fmt.Errorf("constraints.max_error_rate must be in [0, 1]")
		}
	}
	return nil
}

func CanonicalJSON(workload api.Workload) ([]byte, error) {
	return json.Marshal(workload)
}
