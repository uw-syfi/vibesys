package trainticket

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"vibesys/microservice-evaluator/api"
)

var services = []string{"config", "station", "train", "travel", "route", "price"}

var operationTargets = map[string]string{
	"list_config": "config", "list_station": "station", "list_train": "train",
	"list_travel": "travel", "list_route": "route", "list_price": "price",
	"read_config": "config", "read_station": "station", "read_train": "train",
	"read_travel": "travel", "read_route": "route", "read_price": "price",
	"update_read_config": "config", "update_read_station": "station", "update_read_train": "train",
	"update_read_travel": "travel", "update_read_route": "route", "update_read_price": "price",
	"create_read_delete_config": "config",
}

type Config struct {
	Records int
}

type Application struct {
	config Config
	token  string
	active *dataset
}

func New(workload api.Workload) (api.Application, error) {
	targets := make(map[string]api.Target, len(workload.Targets))
	for _, target := range workload.Targets {
		targets[target.Name] = target
	}
	for _, service := range services {
		target, ok := targets[service]
		if !ok {
			return nil, fmt.Errorf("Train Ticket requires a target named %q", service)
		}
		if target.Protocol != "http" {
			return nil, fmt.Errorf("Train Ticket target %q must use HTTP, got %q", service, target.Protocol)
		}
	}
	config := Config{Records: 32}
	for key := range workload.ApplicationConfig {
		if key != "records" {
			return nil, fmt.Errorf("unknown application_config field %q", key)
		}
	}
	if raw, ok := workload.ApplicationConfig["records"]; ok {
		value, ok := integer(raw)
		if !ok || value < 2 {
			return nil, fmt.Errorf("application_config.records must be an integer greater than one")
		}
		config.Records = value
	}
	for _, operation := range workload.Operations {
		target, ok := operationTargets[operation.Name]
		if !ok {
			return nil, fmt.Errorf("unknown Train Ticket operation %q", operation.Name)
		}
		if operation.Target != target {
			return nil, fmt.Errorf("Train Ticket operation %q must target %q", operation.Name, target)
		}
		if operation.HTTP != nil {
			return nil, fmt.Errorf("Train Ticket operation %q must not declare operations.http", operation.Name)
		}
	}
	return &Application{config: config, token: makeAdminToken(time.Now())}, nil
}

func (a *Application) Name() string { return "train-ticket" }

func (a *Application) Prepare(ctx context.Context, runtime api.Runtime, trial api.TrialContext) (any, error) {
	if a.active != nil {
		return nil, fmt.Errorf("previous Train Ticket fixture was not reset")
	}
	namespace := fmt.Sprintf("vb%016x%02x", uint64(trial.Seed), trial.Index)
	data := &dataset{namespace: namespace, records: makeRecords(namespace, trial.Seed, a.config.Records)}
	a.active = data
	for index := range data.records {
		if err := a.createRecord(ctx, runtime, &data.records[index]); err != nil {
			cleanupErr := a.Reset(ctx, runtime, trial)
			if cleanupErr != nil {
				return nil, fmt.Errorf("prepare record %d: %w (cleanup: %v)", index, err, cleanupErr)
			}
			return nil, fmt.Errorf("prepare record %d: %w", index, err)
		}
		data.prepared++
	}
	return data, nil
}

func (a *Application) Reset(ctx context.Context, runtime api.Runtime, _ api.TrialContext) error {
	data := a.active
	a.active = nil
	if data == nil {
		return nil
	}
	var firstErr error
	for index := data.prepared - 1; index >= 0; index-- {
		if err := a.deleteRecord(ctx, runtime, &data.records[index]); err != nil && firstErr == nil {
			firstErr = fmt.Errorf("cleanup record %d: %w", index, err)
		}
	}
	return firstErr
}

func (a *Application) createRecord(ctx context.Context, runtime api.Runtime, item *record) error {
	steps := []setupStep{
		{"config", http.MethodPost, "/api/v1/configservice/configs", item.config, http.StatusCreated},
		{"station", http.MethodPost, "/api/v1/stationservice/stations", item.stationA, http.StatusCreated},
		{"station", http.MethodPost, "/api/v1/stationservice/stations", item.stationB, http.StatusCreated},
		{"train", http.MethodPost, "/api/v1/trainservice/trains", item.train, http.StatusOK},
		{"route", http.MethodPost, "/api/v1/routeservice/routes", item.routeIn, http.StatusOK},
		{"price", http.MethodPost, "/api/v1/priceservice/prices", item.price, http.StatusCreated},
		{"travel", http.MethodPost, "/api/v1/travelservice/trips", item.tripIn, http.StatusCreated},
	}
	return a.runSetup(ctx, runtime, steps)
}

func (a *Application) deleteRecord(ctx context.Context, runtime api.Runtime, item *record) error {
	steps := []setupStep{
		{"travel", http.MethodDelete, "/api/v1/travelservice/trips/" + item.tripIn.TripID, nil, http.StatusOK},
		{"price", http.MethodDelete, "/api/v1/priceservice/prices", item.price, http.StatusOK},
		{"route", http.MethodDelete, "/api/v1/routeservice/routes/" + item.route.ID, nil, http.StatusOK},
		{"train", http.MethodDelete, "/api/v1/trainservice/trains/" + item.train.ID, nil, http.StatusOK},
		{"station", http.MethodDelete, "/api/v1/stationservice/stations", item.stationA, http.StatusOK},
		{"station", http.MethodDelete, "/api/v1/stationservice/stations", item.stationB, http.StatusOK},
		{"config", http.MethodDelete, "/api/v1/configservice/configs/" + url.PathEscape(item.config.Name), nil, http.StatusOK},
	}
	return a.runSetup(ctx, runtime, steps)
}

type setupStep struct {
	target string
	method string
	path   string
	body   any
	status int
}

func (a *Application) runSetup(ctx context.Context, runtime api.Runtime, steps []setupStep) error {
	for _, step := range steps {
		requestCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
		result := runtime.Invoke(requestCtx, a.invocation(step.target, "fixture", step.method, step.path, step.body))
		cancel()
		if validation := validateEnvelopeResult(result, step.status, 1); !validation.Success {
			return fmt.Errorf("%s %s: %s", step.method, step.path, validation.ErrorMessage)
		}
	}
	return nil
}

func (a *Application) invocation(target, operation, method, path string, body any) api.Invocation {
	spec := api.HTTPRequestSpec{
		Method: method, Path: path,
		Headers: map[string]string{"Accept": "application/json", "Authorization": "Bearer " + a.token},
	}
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			panic(fmt.Sprintf("marshal trusted Train Ticket entity: %v", err))
		}
		spec.Body = string(encoded)
		spec.Headers["Content-Type"] = "application/json"
	}
	return api.Invocation{Target: target, Operation: operation, Payload: spec}
}

func integer(value any) (int, bool) {
	switch number := value.(type) {
	case int64:
		return int(number), int64(int(number)) == number
	case int:
		return number, true
	case float64:
		return int(number), number == float64(int(number))
	default:
		return 0, false
	}
}

func serviceFromOperation(name string) string {
	for _, service := range services {
		if strings.HasSuffix(name, "_"+service) {
			return service
		}
	}
	return "config"
}
