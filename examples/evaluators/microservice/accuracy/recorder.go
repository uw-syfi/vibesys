package accuracy

import (
	"fmt"
	"sort"
	"sync"

	"vibesys/microservice-evaluator/api"
)

type Recorder struct {
	mu         sync.Mutex
	checks     int
	claimed    int
	properties map[string]bool
	required   map[string]struct{}
}

func newRecorder(properties []api.AccuracyProperty) (*Recorder, error) {
	recorder := &Recorder{
		properties: make(map[string]bool, len(properties)),
		required:   make(map[string]struct{}),
	}
	for index, property := range properties {
		if property.Name == "" {
			return nil, fmt.Errorf("accuracy property %d has an empty name", index)
		}
		if _, duplicate := recorder.properties[property.Name]; duplicate {
			return nil, fmt.Errorf("accuracy property %q is duplicated", property.Name)
		}
		recorder.properties[property.Name] = false
		if property.Required {
			recorder.required[property.Name] = struct{}{}
		}
	}
	if len(properties) == 0 {
		return nil, fmt.Errorf("accuracy application declares no properties")
	}
	if len(recorder.required) == 0 {
		return nil, fmt.Errorf("accuracy application declares no required properties")
	}
	return recorder, nil
}

func (r *Recorder) AddChecks(count int) {
	if count < 0 {
		panic("accuracy check count cannot be negative")
	}
	r.mu.Lock()
	r.checks += count
	r.mu.Unlock()
}

func (r *Recorder) Pass(names ...string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(names) == 0 {
		return fmt.Errorf("accuracy application passed an empty property group")
	}
	if r.checks <= r.claimed {
		return fmt.Errorf("accuracy property group %v has no newly recorded checks", names)
	}
	seen := make(map[string]struct{}, len(names))
	for _, name := range names {
		passed, exists := r.properties[name]
		if !exists {
			return fmt.Errorf("accuracy application passed undeclared property %q", name)
		}
		if passed {
			return fmt.Errorf("accuracy property %q was passed more than once", name)
		}
		if _, duplicate := seen[name]; duplicate {
			return fmt.Errorf("accuracy property %q is duplicated in a pass group", name)
		}
		seen[name] = struct{}{}
	}
	for _, name := range names {
		r.properties[name] = true
	}
	r.claimed = r.checks
	return nil
}

func (r *Recorder) snapshot() (int, map[string]bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	properties := make(map[string]bool, len(r.properties))
	for name, passed := range r.properties {
		properties[name] = passed
	}
	return r.checks, properties
}

func (r *Recorder) validateRequired() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	missing := make([]string, 0)
	for name := range r.required {
		if !r.properties[name] {
			missing = append(missing, name)
		}
	}
	if len(missing) == 0 {
		return nil
	}
	sort.Strings(missing)
	return fmt.Errorf("required accuracy properties were not passed: %v", missing)
}
