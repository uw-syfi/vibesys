// Package hotel owns topology, seed-input, and preflight contracts shared by
// the independent Hotel benchmark and accuracy adapters.
package hotel

import (
	"fmt"
	"time"

	"vibesys/microservice-evaluator/api"
)

const GatewayTarget = "gateway"

type Config struct {
	Timeout time.Duration
}

// ValidateTopology keeps mode-neutral workload requirements identical in
// benchmark and accuracy mode. Semantic response oracles intentionally live in
// the mode-specific packages.
func ValidateTopology(workload api.Workload) (Config, error) {
	for key := range workload.ApplicationConfig {
		return Config{}, fmt.Errorf("unknown Hotel application_config field %q", key)
	}
	targetFound := false
	for _, target := range workload.Targets {
		if target.Name != GatewayTarget {
			continue
		}
		targetFound = true
		if target.Protocol != "http" {
			return Config{}, fmt.Errorf("Hotel gateway target must use HTTP, got %q", target.Protocol)
		}
		if target.SessionPolicy != "reuse" {
			return Config{}, fmt.Errorf("Hotel gateway target must use session_policy reuse")
		}
	}
	if !targetFound {
		return Config{}, fmt.Errorf("Hotel requires a target named %q", GatewayTarget)
	}
	if workload.Load.TimeoutSeconds <= 0 {
		return Config{}, fmt.Errorf("Hotel timeout must be positive")
	}
	return Config{Timeout: time.Duration(workload.Load.TimeoutSeconds * float64(time.Second))}, nil
}
