package hotel

import (
	"context"
	"fmt"
	"time"

	"vibesys/microservice-evaluator/api"
	hotelsupport "vibesys/microservice-evaluator/appsupport/hotel"
)

// Application is the Hotel Reservation accuracy adapter. Seeded response
// semantics deliberately live here instead of in the mode-neutral appsupport
// package shared with the benchmark.
type Application struct {
	timeout time.Duration
	seed    int64
	catalog map[string]profile
}

func New(workload api.Workload) (api.AccuracyApplication, error) {
	config, err := hotelsupport.ValidateTopology(workload)
	if err != nil {
		return nil, err
	}
	catalog, err := seedProfiles()
	if err != nil {
		return nil, err
	}
	return &Application{timeout: config.Timeout, seed: workload.Load.Seed, catalog: catalog}, nil
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
		{Name: "reservation_optional_number", Required: true},
		{Name: "reservation_capacity", Required: true},
		{Name: "read_your_write", Required: true},
		{Name: "reservation_isolation", Required: true},
		{Name: "crash_recovery", Required: false},
	}
}

func (a *Application) ReadinessProbes() []api.ReadinessProbe {
	return hotelsupport.ReadinessProbes(a.seed)
}

func (a *Application) PreflightProbes() []api.ReadinessProbe {
	return hotelsupport.PreflightProbes()
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
