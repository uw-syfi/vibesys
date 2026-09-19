package hotel

import (
	"context"
	"fmt"
	"time"

	"vibesys/microservice-evaluator/api"
)

// Application is the example-owned Hotel Reservation accuracy adapter.
type Application struct {
	timeout time.Duration
	seed    int64
	catalog map[string]profile
	strict  Strictness
}

func New(workload api.Workload) (api.AccuracyApplication, error) {
	config, err := ValidateTopology(workload)
	if err != nil {
		return nil, err
	}
	catalog, err := seedProfiles()
	if err != nil {
		return nil, err
	}
	return &Application{
		timeout: config.Timeout,
		seed:    workload.Load.Seed,
		catalog: catalog,
		strict:  config.Strict,
	}, nil
}

func (a *Application) Name() string { return "hotel" }

func (a *Application) Properties() []api.AccuracyProperty {
	return []api.AccuracyProperty{
		{Name: "protocol_contract", Required: true},
		{Name: "persistent_http", Required: true},
		{Name: "strict_geojson_schema", Required: true},
		{Name: "seeded_profile_semantics", Required: true},
		{Name: "search_semantics", Required: true},
		{Name: "search_availability", Required: true},
		{Name: "recommendation_semantics", Required: true},
		{Name: "authentication_semantics", Required: true},
		{Name: "sequential_differential", Required: true},
		{Name: "reservation_optional_number", Required: true},
		{Name: "reservation_capacity", Required: true},
		{Name: "read_your_write", Required: true},
		{Name: "reservation_isolation", Required: true},
		{Name: "degenerate_date_ranges", Required: true},
		{Name: "multi_night_atomicity", Required: true},
		{Name: "concurrent_isolation", Required: true},
		{Name: "crash_recovery", Required: false},
		{Name: "durable_capacity", Required: false},
		// The remaining properties are violated by the pinned upstream
		// implementation, so they are reported only when a workload opts in
		// through application_config. See README.md for the counterexamples.
		{Name: "linearizable_capacity", Required: false},
		{Name: "durable_availability", Required: false},
		{Name: "endpoint_liveness", Required: false},
	}
}

func (a *Application) ReadinessProbes() []api.ReadinessProbe {
	return ReadinessProbes(a.seed)
}

func (a *Application) PreflightProbes() []api.ReadinessProbe {
	return PreflightProbes()
}

func (*Application) PreflightProperties() []string {
	return []string{"protocol_contract", "persistent_http"}
}

func (*Application) CasePolicy() api.AccuracyCasePolicy {
	return api.AccuracyCasePolicy{MinimumCases: 4, RandomExtraCases: 3}
}

func checkContext(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("Hotel accuracy check interrupted: %w", err)
	}
	return nil
}
