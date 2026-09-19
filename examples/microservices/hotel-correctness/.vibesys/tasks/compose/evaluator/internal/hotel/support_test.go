package hotel

import (
	"reflect"
	"testing"

	"vibesys/microservice-evaluator/api"
)

func TestExampleOwnedSeedInputs(t *testing.T) {
	username, password, err := User(12)
	if err != nil {
		t.Fatal(err)
	}
	if username != "Cornell_3132" || password != "12121212121212121212" {
		t.Fatalf("seed user = (%q, %q)", username, password)
	}
	for id, want := range map[int]int{1: 200, 7: 300, 8: 250, 9: 200, 80: 250} {
		got, err := Capacity(id)
		if err != nil || got != want {
			t.Fatalf("Capacity(%d) = %d, %v; want %d", id, got, err, want)
		}
	}
}

func TestExampleOwnedTopologyAndStrictness(t *testing.T) {
	workload := api.Workload{
		Load:              api.Load{TimeoutSeconds: 2},
		Targets:           []api.Target{{Name: GatewayTarget, Protocol: "http", SessionPolicy: "reuse"}},
		ApplicationConfig: map[string]any{"strict_endpoint_liveness": true},
	}
	config, err := ValidateTopology(workload)
	if err != nil {
		t.Fatal(err)
	}
	if !config.Strict.EndpointLiveness {
		t.Fatal("strict endpoint liveness was not enabled")
	}
	workload.ApplicationConfig["strict_endpoint_liveness"] = "true"
	if _, err := ValidateTopology(workload); err == nil {
		t.Fatal("non-boolean strictness was accepted")
	}
}

func TestExampleOwnedPreflightIsDeterministic(t *testing.T) {
	left := PreflightProbes()
	right := PreflightProbes()
	for index := range left {
		if left[index].Name != right[index].Name ||
			!reflect.DeepEqual(left[index].Invocation, right[index].Invocation) {
			t.Fatalf("probe %d differs", index)
		}
	}
}
