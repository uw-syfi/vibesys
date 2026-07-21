package trainticket

import (
	"context"
	"fmt"
	"net/http"
	"time"

	"vibesys/microservice-evaluator/accuracy/httpcheck"
	"vibesys/microservice-evaluator/api"
)

var services = []string{"config", "station", "train", "travel", "route", "price"}

var welcomePaths = map[string]struct {
	path string
	text string
}{
	"config":  {"/welcome", "Welcome to [ Config Service ] !"},
	"station": {"/welcome", "Welcome to [ Station Service ] !"},
	"train":   {"/trains/welcome", "Welcome to [ Train Service ] !"},
	"travel":  {"/welcome", "Welcome to [ Travel Service ] !"},
	"route":   {"/welcome", "Welcome to [ Route Service ] !"},
	"price":   {"/prices/welcome", "Welcome to [ Price Service ] !"},
}

type Application struct {
	timeout time.Duration
	catalog map[string][]map[string]any
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
	for key := range workload.ApplicationConfig {
		if key != "records" {
			return nil, fmt.Errorf("unknown Train Ticket application_config field %q", key)
		}
	}
	catalog, err := loadSeedCatalog()
	if err != nil {
		return nil, err
	}
	return &Application{
		timeout: time.Duration(workload.Load.TimeoutSeconds * float64(time.Second)),
		catalog: catalog,
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
	probes := make([]api.ReadinessProbe, 0, len(services))
	for _, service := range services {
		service := service
		welcome := welcomePaths[service]
		probes = append(probes, api.ReadinessProbe{
			Name: service,
			Invocation: api.Invocation{
				Target:    service,
				Operation: "accuracy-readiness",
				Payload: api.HTTPRequestSpec{
					Method: http.MethodGet,
					Path:   servicePaths[service] + welcome.path,
					Headers: map[string]string{
						"Accept": "text/plain,*/*",
					},
				},
			},
			Validate: func(result api.ProtocolResult) error {
				return httpcheck.ExactText(result, http.StatusOK, welcome.text)
			},
		})
	}
	return probes
}

func pass(recorder api.AccuracyRecorder, properties ...string) error {
	for _, property := range properties {
		if err := recorder.Pass(property); err != nil {
			return err
		}
	}
	return nil
}

func checkContext(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("accuracy check interrupted: %w", err)
	}
	return nil
}
