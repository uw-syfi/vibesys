package trainticket

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"vibesys/microservice-evaluator/api"
	trainticketsupport "vibesys/microservice-evaluator/appsupport/trainticket"
	"vibesys/microservice-evaluator/wire/httpjson"
)

var services = trainticketsupport.Services()

var operationTargets = map[string]string{
	"list_config": "config", "list_station": "station", "list_train": "train",
	"list_travel": "travel", "list_route": "route", "list_price": "price",
	"read_config": "config", "read_station": "station", "read_train": "train",
	"read_travel": "travel", "read_route": "route", "read_price": "price",
	"update_read_config": "config", "update_read_station": "station", "update_read_train": "train",
	"update_read_travel": "travel", "update_read_route": "route", "update_read_price": "price",
	"create_read_delete_config": "config",
}

type Config = trainticketsupport.Config

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
	config, err := trainticketsupport.ParseConfig(workload.ApplicationConfig)
	if err != nil {
		return nil, err
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
	namespaceDigest := sha256.Sum256([]byte(fmt.Sprintf("%d/%d", trial.Seed, trial.Index)))
	namespace := fmt.Sprintf("%x", namespaceDigest[:12])
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
	}
	return data, nil
}

func (a *Application) Reset(ctx context.Context, runtime api.Runtime, _ api.TrialContext) error {
	data := a.active
	if data == nil {
		return nil
	}
	var cleanupErrors []error
	for index := len(data.records) - 1; index >= 0; index-- {
		if err := a.deleteRecord(ctx, runtime, &data.records[index]); err != nil {
			cleanupErrors = append(cleanupErrors, fmt.Errorf("cleanup record %d: %w", index, err))
		}
	}
	if len(cleanupErrors) > 0 {
		return errors.Join(cleanupErrors...)
	}
	a.active = nil
	return nil
}

func (a *Application) createRecord(ctx context.Context, runtime api.Runtime, item *record) error {
	steps := lifecycleSteps(item)
	for index, step := range steps {
		if err := a.runStep(ctx, runtime, step.create); err != nil {
			cleanupErr := a.deleteRecord(ctx, runtime, item)
			if cleanupErr != nil {
				return fmt.Errorf("create step %d: %w (partial cleanup: %v)", index, err, cleanupErr)
			}
			return fmt.Errorf("create step %d: %w", index, err)
		}
		item.created[index] = true
	}
	return nil
}

func (a *Application) deleteRecord(ctx context.Context, runtime api.Runtime, item *record) error {
	steps := lifecycleSteps(item)
	var cleanupErrors []error
	for index := len(steps) - 1; index >= 0; index-- {
		if !item.created[index] {
			continue
		}
		if err := a.runStep(ctx, runtime, steps[index].delete); err != nil {
			cleanupErrors = append(cleanupErrors, fmt.Errorf("delete step %d: %w", index, err))
			continue
		}
		item.created[index] = false
	}
	return errors.Join(cleanupErrors...)
}

type lifecycleStep struct {
	create setupStep
	delete setupStep
}

func lifecycleSteps(item *record) []lifecycleStep {
	return []lifecycleStep{
		{
			create: setupStep{"config", http.MethodPost, servicePath("config", "/configs"), item.config, http.StatusCreated},
			delete: setupStep{"config", http.MethodDelete, servicePath("config", "/configs/"+url.PathEscape(item.config.Name)), nil, http.StatusOK},
		},
		{
			create: setupStep{"station", http.MethodPost, servicePath("station", "/stations"), item.stationA, http.StatusCreated},
			delete: setupStep{"station", http.MethodDelete, servicePath("station", "/stations"), item.stationA, http.StatusOK},
		},
		{
			create: setupStep{"station", http.MethodPost, servicePath("station", "/stations"), item.stationB, http.StatusCreated},
			delete: setupStep{"station", http.MethodDelete, servicePath("station", "/stations"), item.stationB, http.StatusOK},
		},
		{
			create: setupStep{"train", http.MethodPost, servicePath("train", "/trains"), item.train, http.StatusOK},
			delete: setupStep{"train", http.MethodDelete, servicePath("train", "/trains/"+item.train.ID), nil, http.StatusOK},
		},
		{
			create: setupStep{"route", http.MethodPost, servicePath("route", "/routes"), item.routeIn, http.StatusOK},
			delete: setupStep{"route", http.MethodDelete, servicePath("route", "/routes/"+item.route.ID), nil, http.StatusOK},
		},
		{
			create: setupStep{"price", http.MethodPost, servicePath("price", "/prices"), item.price, http.StatusCreated},
			delete: setupStep{"price", http.MethodDelete, servicePath("price", "/prices"), item.price, http.StatusOK},
		},
		{
			create: setupStep{"travel", http.MethodPost, servicePath("travel", "/trips"), item.tripIn, http.StatusCreated},
			delete: setupStep{"travel", http.MethodDelete, servicePath("travel", "/trips/"+item.tripIn.TripID), nil, http.StatusOK},
		},
	}
}

func servicePath(service, suffix string) string {
	return trainticketsupport.MustPath(service, suffix)
}

type setupStep struct {
	target string
	method string
	path   string
	body   any
	status int
}

func (a *Application) runStep(ctx context.Context, runtime api.Runtime, step setupStep) error {
	requestCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	result := runtime.Invoke(requestCtx, a.invocation(step.target, "fixture", step.method, step.path, step.body))
	if validation := validateEnvelopeResult(result, step.status, 1); !validation.Success {
		return fmt.Errorf("%s %s: %s", step.method, step.path, validation.ErrorMessage)
	}
	return nil
}

func (a *Application) invocation(target, operation, method, path string, body any) api.Invocation {
	spec := httpjson.MustRequest(method, path, body, "Bearer "+a.token)
	return api.Invocation{Target: target, Operation: operation, Payload: spec}
}

func serviceFromOperation(name string) string {
	for _, service := range services {
		if strings.HasSuffix(name, "_"+service) {
			return service
		}
	}
	return "config"
}
