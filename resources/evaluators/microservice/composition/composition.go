// Package composition builds servicebench registries from command-owned
// components.
package composition

import (
	"fmt"

	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/registry"
)

// Registration adds one command-owned component to a servicebench registry.
type Registration func(*registry.Registry) error

// AccuracyApplication registers an accuracy adapter owned by the composing
// command. Keeping this registration outside the generic servicebench command
// lets examples supply application-specific correctness without adding imports
// to servicebench itself.
func AccuracyApplication(
	name string,
	factory registry.AccuracyApplicationFactory,
) Registration {
	return func(registered *registry.Registry) error {
		return registered.RegisterAccuracyApplication(name, factory)
	}
}

// Application registers a benchmark adapter owned by the composing command.
func Application(name string, factory registry.ApplicationFactory) Registration {
	return func(registered *registry.Registry) error {
		return registered.RegisterApplication(name, factory)
	}
}

// Driver registers a transport driver owned by the composing command.
func Driver(driver api.Driver) Registration {
	return func(registered *registry.Registry) error {
		return registered.RegisterDriver(driver)
	}
}

// NewRegistry returns a registry populated only by the composing command's
// registrations.
func NewRegistry(registrations ...Registration) (*registry.Registry, error) {
	registered := registry.New()
	for index, registration := range registrations {
		if registration == nil {
			return nil, fmt.Errorf("servicebench registration %d is nil", index)
		}
		if err := registration(registered); err != nil {
			return nil, fmt.Errorf("servicebench registration %d: %w", index, err)
		}
	}
	return registered, nil
}
