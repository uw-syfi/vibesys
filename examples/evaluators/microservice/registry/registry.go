package registry

import (
	"fmt"
	"reflect"
	"sort"

	"vibesys/microservice-evaluator/api"
)

type ApplicationFactory func(api.Workload) (api.Application, error)
type AccuracyApplicationFactory func(api.Workload) (api.AccuracyApplication, error)

type Registry struct {
	drivers      map[string]api.Driver
	applications map[string]ApplicationFactory
	accuracy     map[string]AccuracyApplicationFactory
}

func New() *Registry {
	return &Registry{
		drivers:      make(map[string]api.Driver),
		applications: make(map[string]ApplicationFactory),
		accuracy:     make(map[string]AccuracyApplicationFactory),
	}
}

func (r *Registry) RegisterAccuracyApplication(
	name string,
	factory AccuracyApplicationFactory,
) error {
	if name == "" {
		return fmt.Errorf("accuracy application name must not be empty")
	}
	if factory == nil {
		return fmt.Errorf("accuracy application %q factory must not be nil", name)
	}
	if _, exists := r.accuracy[name]; exists {
		return fmt.Errorf("accuracy application %q is already registered", name)
	}
	r.accuracy[name] = factory
	return nil
}

func (r *Registry) RegisterDriver(driver api.Driver) error {
	if isNil(driver) {
		return fmt.Errorf("driver must not be nil")
	}
	name := driver.Protocol()
	if name == "" {
		return fmt.Errorf("driver protocol must not be empty")
	}
	if _, exists := r.drivers[name]; exists {
		return fmt.Errorf("driver protocol %q is already registered", name)
	}
	r.drivers[name] = driver
	return nil
}

func (r *Registry) RegisterApplication(name string, factory ApplicationFactory) error {
	if name == "" {
		return fmt.Errorf("application name must not be empty")
	}
	if factory == nil {
		return fmt.Errorf("application %q factory must not be nil", name)
	}
	if _, exists := r.applications[name]; exists {
		return fmt.Errorf("application %q is already registered", name)
	}
	r.applications[name] = factory
	return nil
}

func (r *Registry) Driver(protocol string) (api.Driver, error) {
	driver, ok := r.drivers[protocol]
	if !ok {
		return nil, fmt.Errorf("unsupported target protocol %q (registered: %v)", protocol, sortedKeys(r.drivers))
	}
	return driver, nil
}

func (r *Registry) Application(workload api.Workload) (api.Application, error) {
	factory, ok := r.applications[workload.Application]
	if !ok {
		return nil, fmt.Errorf("unsupported application %q (registered: %v)", workload.Application, sortedKeys(r.applications))
	}
	application, err := factory(workload)
	if err != nil {
		return nil, fmt.Errorf("configure application %q: %w", workload.Application, err)
	}
	if isNil(application) {
		return nil, fmt.Errorf("configure application %q: factory returned nil", workload.Application)
	}
	if actual := application.Name(); actual != workload.Application {
		return nil, fmt.Errorf(
			"configure application %q: factory returned application named %q",
			workload.Application,
			actual,
		)
	}
	return application, nil
}

func (r *Registry) AccuracyApplication(workload api.Workload) (api.AccuracyApplication, error) {
	factory, ok := r.accuracy[workload.Application]
	if !ok {
		return nil, fmt.Errorf(
			"unsupported accuracy application %q (registered: %v)",
			workload.Application,
			sortedKeys(r.accuracy),
		)
	}
	application, err := factory(workload)
	if err != nil {
		return nil, fmt.Errorf("configure accuracy application %q: %w", workload.Application, err)
	}
	if isNil(application) {
		return nil, fmt.Errorf("configure accuracy application %q: factory returned nil", workload.Application)
	}
	if actual := application.Name(); actual != workload.Application {
		return nil, fmt.Errorf(
			"configure accuracy application %q: factory returned application named %q",
			workload.Application,
			actual,
		)
	}
	return application, nil
}

func isNil(value any) bool {
	if value == nil {
		return true
	}
	reflected := reflect.ValueOf(value)
	switch reflected.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return reflected.IsNil()
	default:
		return false
	}
}

func sortedKeys[T any](values map[string]T) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}
