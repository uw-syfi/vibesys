package composition

import (
	"context"
	"errors"
	"strings"
	"testing"

	"vibesys/microservice-evaluator/api"
)

func TestNewRegistryAddsCommandOwnedAccuracyApplication(t *testing.T) {
	want := errors.New("custom factory called")
	registered, err := NewRegistry(AccuracyApplication(
		"custom",
		func(api.Workload) (api.AccuracyApplication, error) { return nil, want },
	))
	if err != nil {
		t.Fatal(err)
	}
	_, err = registered.AccuracyApplication(api.Workload{Application: "custom"})
	if !errors.Is(err, want) {
		t.Fatalf("custom accuracy lookup error=%v, want %v", err, want)
	}
}

func TestNewRegistryRejectsInvalidRegistrations(t *testing.T) {
	if _, err := NewRegistry(nil); err == nil || !strings.Contains(err.Error(), "is nil") {
		t.Fatalf("nil registration error=%v", err)
	}
	registration := AccuracyApplication(
		"duplicate",
		func(api.Workload) (api.AccuracyApplication, error) { return nil, nil },
	)
	_, err := NewRegistry(registration, registration)
	if err == nil || !strings.Contains(err.Error(), "already registered") {
		t.Fatalf("duplicate registration error=%v", err)
	}
}

type compositionDriver struct{}

func (compositionDriver) Protocol() string { return "custom" }
func (compositionDriver) Open(context.Context, api.Target) (api.Client, error) {
	return nil, errors.New("not used")
}

func TestNewRegistryAddsCommandOwnedDriver(t *testing.T) {
	registered, err := NewRegistry(Driver(compositionDriver{}))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := registered.Driver("custom"); err != nil {
		t.Fatal(err)
	}
}
