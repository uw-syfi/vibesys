package hotel

import (
	"fmt"
	"time"

	"vibesys/microservice-evaluator/api"
)

const GatewayTarget = "gateway"

type Strictness struct {
	LinearizableCapacity bool
	DurableAvailability  bool
	EndpointLiveness     bool
}

type Config struct {
	Timeout time.Duration
	Strict  Strictness
}

func ValidateTopology(workload api.Workload) (Config, error) {
	strict, err := parseStrictness(workload.ApplicationConfig)
	if err != nil {
		return Config{}, err
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
	return Config{
		Timeout: time.Duration(workload.Load.TimeoutSeconds * float64(time.Second)),
		Strict:  strict,
	}, nil
}

func parseStrictness(config map[string]any) (Strictness, error) {
	strict := Strictness{}
	fields := map[string]*bool{
		"strict_linearizable_capacity": &strict.LinearizableCapacity,
		"strict_durable_availability":  &strict.DurableAvailability,
		"strict_endpoint_liveness":     &strict.EndpointLiveness,
	}
	for key, value := range config {
		field, known := fields[key]
		if !known {
			return Strictness{}, fmt.Errorf("unknown Hotel application_config field %q", key)
		}
		enabled, ok := value.(bool)
		if !ok {
			return Strictness{}, fmt.Errorf(
				"Hotel application_config field %q must be a boolean, got %T", key, value,
			)
		}
		*field = enabled
	}
	return strict, nil
}
