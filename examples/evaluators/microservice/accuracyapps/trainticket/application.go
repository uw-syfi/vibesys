package trainticket

import (
	"context"
	"fmt"
	"time"

	"vibesys/microservice-evaluator/api"
	trainticketsupport "vibesys/microservice-evaluator/appsupport/trainticket"
)

var services = trainticketsupport.Services()

type Application struct {
	timeout time.Duration
	catalog map[string][]map[string]any
	config  trainticketsupport.Config
	seed    int64
	token   string
}

func New(workload api.Workload) (api.AccuracyApplication, error) {
	targets := make(map[string]api.Target, len(workload.Targets))
	for _, target := range workload.Targets {
		targets[target.Name] = target
	}
	for _, service := range services {
		target, ok := targets[service]
		if !ok {
			return nil, fmt.Errorf("Train Ticket accuracy requires a target named %q", service)
		}
		if target.Protocol != "http" {
			return nil, fmt.Errorf(
				"Train Ticket accuracy target %q must use HTTP, got %q",
				service,
				target.Protocol,
			)
		}
		if service == "station" && target.SessionPolicy != "reuse" {
			return nil, fmt.Errorf(
				"Train Ticket accuracy target %q must use session_policy reuse to verify persistent HTTP",
				service,
			)
		}
	}
	config, err := trainticketsupport.ParseConfig(workload.ApplicationConfig)
	if err != nil {
		return nil, err
	}
	catalog, err := loadSeedCatalog()
	if err != nil {
		return nil, err
	}
	token, err := trainticketsupport.AdminToken(time.Now())
	if err != nil {
		return nil, err
	}
	return &Application{
		timeout: time.Duration(workload.Load.TimeoutSeconds * float64(time.Second)),
		catalog: catalog,
		config:  config,
		seed:    workload.Load.Seed,
		token:   token,
	}, nil
}

func (a *Application) Name() string { return "train-ticket" }

func (a *Application) Properties() []api.AccuracyProperty {
	return []api.AccuracyProperty{
		{Name: "protocol_contract", Required: true},
		{Name: "persistent_http", Required: true},
		{Name: "exact_seed_catalog", Required: true},
		{Name: "strict_entity_schemas", Required: true},
		{Name: "randomized_crud_graph", Required: true},
		{Name: "cross_entity_graph", Required: true},
		{Name: "read_your_write", Required: true},
		{Name: "updates_visible", Required: true},
		{Name: "stale_secondary_indexes", Required: true},
		{Name: "deletes_visible", Required: true},
		{Name: "delete_isolation", Required: true},
		{Name: "crash_recovery", Required: false},
	}
}

func (a *Application) ReadinessProbes() []api.ReadinessProbe {
	return trainticketsupport.ReadinessProbes(a.seed)
}

func (a *Application) PreflightProbes() []api.ReadinessProbe {
	return trainticketsupport.PreflightProbes(a.token)
}

func (a *Application) PreflightProperties() []string {
	return []string{"protocol_contract", "persistent_http"}
}

func (a *Application) CasePolicy() api.AccuracyCasePolicy {
	return api.AccuracyCasePolicy{MinimumCases: a.config.Records, RandomExtraCases: 3}
}

func pass(recorder api.AccuracyRecorder, properties ...string) error {
	return recorder.Pass(properties...)
}

func checkContext(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("accuracy check interrupted: %w", err)
	}
	return nil
}
